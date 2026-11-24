"""Bounded, operator-owned, idempotent Human-in-the-Loop approval manager."""
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import secrets
import time
from typing import Literal

from app.approval.audit_log import record_approval_event
from app.models.response import ApprovalStateSnapshot, ApprovalTargetInfo, ApprovalTicketResponse


_BEIJING_TZ = timezone(timedelta(hours=8))
ApprovalStatus = Literal['pending', 'executing', 'executed', 'rejected', 'expired', 'failed']
_TERMINAL_STATUSES = frozenset({'executed', 'rejected', 'expired', 'failed'})


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalExpiredError(RuntimeError):
    pass


class ApprovalConflictError(RuntimeError):
    pass


@dataclass
class ApprovalTicket:
    approval_id: str
    agent_role: Literal['merchant']
    operator_id: int
    session_id: str
    action: Literal['change_dish_status']
    summary: str
    dish_id: int
    dish_name: str
    category: str | None
    price: float | None
    before_status: int
    before_status_name: str
    after_status: int
    after_status_name: str
    reason: str | None
    status: ApprovalStatus
    created_at_mono: float
    expires_at_mono: float
    created_at_iso: str
    expires_at_iso: str
    expires_at_epoch: float = 0.0
    decided_at_iso: str | None = None
    decision_reason: str | None = None
    execution_message: str | None = None
    lock: asyncio.Lock | None = field(default=None, repr=False)
    lock_loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    def get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self.lock is None or self.lock_loop is not loop:
            self.lock = asyncio.Lock()
            self.lock_loop = loop
        return self.lock

    def to_storage_dict(self) -> dict:
        return {
            'approval_id': self.approval_id,
            'agent_role': self.agent_role,
            'operator_id': self.operator_id,
            'session_id': self.session_id,
            'action': self.action,
            'summary': self.summary,
            'dish_id': self.dish_id,
            'dish_name': self.dish_name,
            'category': self.category,
            'price': self.price,
            'before_status': self.before_status,
            'before_status_name': self.before_status_name,
            'after_status': self.after_status,
            'after_status_name': self.after_status_name,
            'reason': self.reason,
            'status': self.status,
            'created_at_iso': self.created_at_iso,
            'expires_at_iso': self.expires_at_iso,
            'expires_at_epoch': self.expires_at_epoch,
            'decided_at_iso': self.decided_at_iso,
            'decision_reason': self.decision_reason,
            'execution_message': self.execution_message,
        }

    @classmethod
    def from_storage_dict(cls, raw: dict, *, now_mono: float, now_epoch: float) -> 'ApprovalTicket':
        exp_epoch = float(raw.get('expires_at_epoch') or 0.0)
        remaining = max(0.0, exp_epoch - now_epoch) if exp_epoch > 0 else 0.0
        return cls(
            approval_id=str(raw['approval_id']),
            agent_role='merchant',
            operator_id=int(raw['operator_id']),
            session_id=str(raw['session_id']),
            action='change_dish_status',
            summary=str(raw['summary']),
            dish_id=int(raw['dish_id']),
            dish_name=str(raw['dish_name']),
            category=raw.get('category'),
            price=float(raw['price']) if raw.get('price') is not None else None,
            before_status=int(raw['before_status']),
            before_status_name=str(raw['before_status_name']),
            after_status=int(raw['after_status']),
            after_status_name=str(raw['after_status_name']),
            reason=raw.get('reason'),
            status=raw['status'],
            created_at_mono=now_mono,
            expires_at_mono=now_mono + remaining,
            created_at_iso=str(raw['created_at_iso']),
            expires_at_iso=str(raw['expires_at_iso']),
            expires_at_epoch=exp_epoch,
            decided_at_iso=raw.get('decided_at_iso'),
            decision_reason=raw.get('decision_reason'),
            execution_message=raw.get('execution_message'),
        )

    def to_response(self) -> ApprovalTicketResponse:
        return ApprovalTicketResponse(
            agent_role=self.agent_role,
            approval_id=self.approval_id,
            session_id=self.session_id,
            action=self.action,
            status=self.status,
            summary=self.summary,
            target=ApprovalTargetInfo(
                dish_id=self.dish_id,
                dish_name=self.dish_name,
                category=self.category,
                price=self.price,
            ),
            before_state=ApprovalStateSnapshot(
                status=self.before_status,
                status_name=self.before_status_name,
            ),
            after_state=ApprovalStateSnapshot(
                status=self.after_status,
                status_name=self.after_status_name,
            ),
            reason=self.reason,
            created_at=self.created_at_iso,
            expires_at=self.expires_at_iso,
            decided_at=self.decided_at_iso,
            decision_reason=self.decision_reason,
            execution_message=self.execution_message,
        )


def _now_iso(offset_seconds: float = 0.0) -> str:
    return (datetime.now(_BEIJING_TZ) + timedelta(seconds=offset_seconds)).isoformat(timespec='seconds')


@asynccontextmanager
async def _noop_dist_lock():
    yield True


class ApprovalManager:
    """Bounded approval store with optional Redis persistence & distributed lock; never retains tokens."""

    def __init__(
        self,
        *,
        max_entries: int = 200,
        ttl_seconds: float = 300.0,
        retention_seconds: float = 1800.0,
        clock=time.monotonic,
        epoch_clock=time.time,
        audit_log_path: Path | None = None,
        state_store=None,
    ) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError('max_entries must be a positive integer')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError('ttl_seconds must be a positive finite number')
        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, (int, float)) or not math.isfinite(retention_seconds) or retention_seconds <= 0:
            raise ValueError('retention_seconds must be a positive finite number')
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self.retention_seconds = float(retention_seconds)
        self._clock = clock
        self._epoch_clock = epoch_clock
        self._audit_log_path = audit_log_path
        self._state_store = state_store
        self._tickets: dict[str, ApprovalTicket] = {}

    @property
    def _storage_ttl(self) -> float:
        return max(self.ttl_seconds, self.retention_seconds)

    def __len__(self) -> int:
        return len(self._tickets)

    def _sweep(self) -> None:
        now = self._clock()
        to_remove: list[str] = []
        for approval_id, ticket in self._tickets.items():
            if ticket.status == 'pending' and now >= ticket.expires_at_mono:
                ticket.status = 'expired'
                ticket.decided_at_iso = _now_iso()
                ticket.execution_message = '审批单已超过有效期，未执行任何修改。'
                record_approval_event(
                    event='approval_expired',
                    approval_id=ticket.approval_id,
                    operator_id=ticket.operator_id,
                    session_id=ticket.session_id,
                    action=ticket.action,
                    dish_id=ticket.dish_id,
                    before_status=ticket.before_status,
                    after_status=ticket.after_status,
                    outcome='expired',
                    log_path=self._audit_log_path,
                )
            if ticket.status in _TERMINAL_STATUSES and (now - ticket.created_at_mono) >= self.retention_seconds:
                to_remove.append(approval_id)
        for approval_id in to_remove:
            self._tickets.pop(approval_id, None)

    def _evict_if_full(self) -> None:
        if len(self._tickets) < self.max_entries:
            return
        finished = sorted(
            (t for t in self._tickets.values() if t.status in _TERMINAL_STATUSES),
            key=lambda item: item.created_at_mono,
        )
        for ticket in finished:
            self._tickets.pop(ticket.approval_id, None)
            if len(self._tickets) < self.max_entries:
                return
        if len(self._tickets) >= self.max_entries:
            oldest = min(self._tickets.values(), key=lambda item: item.created_at_mono)
            self._tickets.pop(oldest.approval_id, None)

    async def _persist_ticket(self, ticket: ApprovalTicket) -> None:
        if self._state_store is not None:
            await self._state_store.save_approval_record(
                ticket.to_storage_dict(),
                ttl_seconds=self._storage_ttl,
            )

    async def _load_or_sync_ticket(self, approval_id: str) -> ApprovalTicket | None:
        if self._state_store is not None:
            raw = await self._state_store.load_approval_record(approval_id)
            if raw is not None:
                existing = self._tickets.get(approval_id)
                hydrated = ApprovalTicket.from_storage_dict(
                    raw,
                    now_mono=self._clock(),
                    now_epoch=self._epoch_clock(),
                )
                if existing is not None:
                    # Preserve the local asyncio.Lock instance while syncing remote state.
                    existing.status = hydrated.status
                    existing.decided_at_iso = hydrated.decided_at_iso
                    existing.decision_reason = hydrated.decision_reason
                    existing.execution_message = hydrated.execution_message
                    existing.expires_at_epoch = hydrated.expires_at_epoch
                    existing.expires_at_mono = min(existing.expires_at_mono, hydrated.expires_at_mono)
                    return existing
                self._evict_if_full()
                self._tickets[approval_id] = hydrated
                return hydrated
        return self._tickets.get(approval_id)

    def create_dish_status_approval(
        self,
        *,
        operator_id: int,
        session_id: str,
        dish_id: int,
        dish_name: str,
        category: str | None,
        price: float | None,
        before_status: int,
        before_status_name: str,
        after_status: int,
        after_status_name: str,
        reason: str | None = None,
    ) -> ApprovalTicket:
        self._sweep()
        self._evict_if_full()
        now = self._clock()
        approval_id = f'apr_{secrets.token_urlsafe(18)}'
        clean_reason = ' '.join(reason.split())[:200] if isinstance(reason, str) and reason.strip() else None
        summary = f'将菜品「{dish_name}」状态由「{before_status_name}」改为「{after_status_name}」'
        ticket = ApprovalTicket(
            approval_id=approval_id,
            agent_role='merchant',
            operator_id=operator_id,
            session_id=session_id,
            action='change_dish_status',
            summary=summary,
            dish_id=dish_id,
            dish_name=dish_name,
            category=category,
            price=price,
            before_status=before_status,
            before_status_name=before_status_name,
            after_status=after_status,
            after_status_name=after_status_name,
            reason=clean_reason,
            status='pending',
            created_at_mono=now,
            expires_at_mono=now + self.ttl_seconds,
            created_at_iso=_now_iso(0.0),
            expires_at_iso=_now_iso(self.ttl_seconds),
            expires_at_epoch=self._epoch_clock() + self.ttl_seconds,
        )
        self._tickets[approval_id] = ticket
        record_approval_event(
            event='approval_created',
            approval_id=approval_id,
            operator_id=operator_id,
            session_id=session_id,
            action='change_dish_status',
            dish_id=dish_id,
            before_status=before_status,
            after_status=after_status,
            outcome='pending',
            reason=clean_reason,
            log_path=self._audit_log_path,
        )
        return ticket

    async def acreate_dish_status_approval(self, **kwargs) -> ApprovalTicket:
        ticket = self.create_dish_status_approval(**kwargs)
        await self._persist_ticket(ticket)
        return ticket

    def get(self, *, approval_id: str, operator_id: int) -> ApprovalTicketResponse | None:
        self._sweep()
        ticket = self._tickets.get(approval_id)
        if ticket is None or ticket.operator_id != operator_id or ticket.agent_role != 'merchant':
            return None
        return ticket.to_response()

    async def aget(self, *, approval_id: str, operator_id: int) -> ApprovalTicketResponse | None:
        self._sweep()
        ticket = await self._load_or_sync_ticket(approval_id)
        if ticket is None or ticket.operator_id != operator_id or ticket.agent_role != 'merchant':
            return None
        if ticket.status == 'pending' and self._clock() >= ticket.expires_at_mono:
            ticket.status = 'expired'
            ticket.decided_at_iso = _now_iso()
            ticket.execution_message = '审批单已超过有效期，未执行任何修改。'
            await self._persist_ticket(ticket)
        return ticket.to_response()

    async def confirm(
        self,
        *,
        approval_id: str,
        operator_id: int,
        executor: Callable[[ApprovalTicket], Awaitable[str]],
        decision_reason: str | None = None,
    ) -> ApprovalTicketResponse:
        self._sweep()
        dist_lock = (
            self._state_store.approval_lock(approval_id)
            if self._state_store is not None
            else _noop_dist_lock()
        )
        async with dist_lock as dist_acquired:
            if not dist_acquired:
                raise ApprovalConflictError('审批单正在由其他节点处理中，请勿重复提交')
            ticket = await self._load_or_sync_ticket(approval_id)
            if ticket is None or ticket.operator_id != operator_id or ticket.agent_role != 'merchant':
                raise ApprovalNotFoundError('审批单不存在或无权操作')

            async with ticket.get_lock():
                now = self._clock()
                if ticket.status == 'pending' and now >= ticket.expires_at_mono:
                    ticket.status = 'expired'
                    ticket.decided_at_iso = _now_iso()
                    ticket.execution_message = '审批单已过期，请重新发起。'
                    await self._persist_ticket(ticket)
                if ticket.status == 'expired':
                    raise ApprovalExpiredError('审批单已过期，请重新发起修改请求')
                if ticket.status != 'pending':
                    raise ApprovalConflictError(f'审批单当前状态为 {ticket.status}，不可重复执行')

                ticket.status = 'executing'
                ticket.decision_reason = (
                    ' '.join(decision_reason.split())[:200]
                    if isinstance(decision_reason, str) and decision_reason.strip()
                    else None
                )
                await self._persist_ticket(ticket)
                started = time.monotonic()
                try:
                    message = await executor(ticket)
                except ApprovalConflictError:
                    ticket.status = 'failed'
                    ticket.decided_at_iso = _now_iso()
                    ticket.execution_message = '目标菜品状态已发生变化，原审批单失效，未执行修改。'
                    await self._persist_ticket(ticket)
                    record_approval_event(
                        event='approval_confirm',
                        approval_id=ticket.approval_id,
                        operator_id=operator_id,
                        session_id=ticket.session_id,
                        action=ticket.action,
                        dish_id=ticket.dish_id,
                        before_status=ticket.before_status,
                        after_status=ticket.after_status,
                        outcome='state_conflict',
                        reason=ticket.decision_reason,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        log_path=self._audit_log_path,
                    )
                    raise
                except Exception:
                    ticket.status = 'failed'
                    ticket.decided_at_iso = _now_iso()
                    ticket.execution_message = '执行菜品状态更新失败，请检查业务后端后重新发起。'
                    await self._persist_ticket(ticket)
                    record_approval_event(
                        event='approval_confirm',
                        approval_id=ticket.approval_id,
                        operator_id=operator_id,
                        session_id=ticket.session_id,
                        action=ticket.action,
                        dish_id=ticket.dish_id,
                        before_status=ticket.before_status,
                        after_status=ticket.after_status,
                        outcome='execution_failed',
                        reason=ticket.decision_reason,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        log_path=self._audit_log_path,
                    )
                    raise

                ticket.status = 'executed'
                ticket.decided_at_iso = _now_iso()
                ticket.execution_message = message
                await self._persist_ticket(ticket)
                record_approval_event(
                    event='approval_confirm',
                    approval_id=ticket.approval_id,
                    operator_id=operator_id,
                    session_id=ticket.session_id,
                    action=ticket.action,
                    dish_id=ticket.dish_id,
                    before_status=ticket.before_status,
                    after_status=ticket.after_status,
                    outcome='executed',
                    reason=ticket.decision_reason,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    log_path=self._audit_log_path,
                )
                return ticket.to_response()

    async def reject(
        self,
        *,
        approval_id: str,
        operator_id: int,
        decision_reason: str | None = None,
    ) -> ApprovalTicketResponse:
        self._sweep()
        dist_lock = (
            self._state_store.approval_lock(approval_id)
            if self._state_store is not None
            else _noop_dist_lock()
        )
        async with dist_lock as dist_acquired:
            if not dist_acquired:
                raise ApprovalConflictError('审批单正在由其他节点处理中，请勿重复提交')
            ticket = await self._load_or_sync_ticket(approval_id)
            if ticket is None or ticket.operator_id != operator_id or ticket.agent_role != 'merchant':
                raise ApprovalNotFoundError('审批单不存在或无权操作')

            async with ticket.get_lock():
                now = self._clock()
                if ticket.status == 'pending' and now >= ticket.expires_at_mono:
                    ticket.status = 'expired'
                    ticket.decided_at_iso = _now_iso()
                    ticket.execution_message = '审批单已过期，未执行任何修改。'
                    await self._persist_ticket(ticket)
                if ticket.status == 'expired':
                    raise ApprovalExpiredError('审批单已过期')
                if ticket.status != 'pending':
                    raise ApprovalConflictError(f'审批单当前状态为 {ticket.status}，不可重复操作')

                ticket.status = 'rejected'
                ticket.decided_at_iso = _now_iso()
                ticket.decision_reason = (
                    ' '.join(decision_reason.split())[:200]
                    if isinstance(decision_reason, str) and decision_reason.strip()
                    else None
                )
                ticket.execution_message = '管理员已拒绝本次修改，未变更菜品状态。'
                await self._persist_ticket(ticket)
                record_approval_event(
                    event='approval_reject',
                    approval_id=ticket.approval_id,
                    operator_id=operator_id,
                    session_id=ticket.session_id,
                    action=ticket.action,
                    dish_id=ticket.dish_id,
                    before_status=ticket.before_status,
                    after_status=ticket.after_status,
                    outcome='rejected',
                    reason=ticket.decision_reason,
                    log_path=self._audit_log_path,
                )
                return ticket.to_response()

    def cancel_session(self, *, operator_id: int, session_id: str) -> int:
        """Expire all pending approval tickets belonging to a cleared merchant session."""
        cancelled = 0
        for ticket in self._tickets.values():
            if ticket.operator_id == operator_id and ticket.session_id == session_id and ticket.status == 'pending':
                ticket.status = 'expired'
                ticket.decided_at_iso = _now_iso()
                ticket.execution_message = '关联会话已清空，审批单已自动失效。'
                record_approval_event(
                    event='approval_session_cleared',
                    approval_id=ticket.approval_id,
                    operator_id=operator_id,
                    session_id=session_id,
                    action=ticket.action,
                    dish_id=ticket.dish_id,
                    before_status=ticket.before_status,
                    after_status=ticket.after_status,
                    outcome='expired',
                    log_path=self._audit_log_path,
                )
                cancelled += 1
        return cancelled

    async def acancel_session(self, *, operator_id: int, session_id: str) -> int:
        cancelled = self.cancel_session(operator_id=operator_id, session_id=session_id)
        if self._state_store is not None:
            for ticket in self._tickets.values():
                if ticket.operator_id == operator_id and ticket.session_id == session_id:
                    await self._persist_ticket(ticket)
            remote_ids = await self._state_store.list_session_approval_ids(
                operator_id=operator_id,
                session_id=session_id,
            )
            for aid in remote_ids:
                if aid in self._tickets:
                    continue
                ticket = await self._load_or_sync_ticket(aid)
                if (
                    ticket is not None
                    and ticket.operator_id == operator_id
                    and ticket.session_id == session_id
                    and ticket.status == 'pending'
                ):
                    ticket.status = 'expired'
                    ticket.decided_at_iso = _now_iso()
                    ticket.execution_message = '关联会话已清空，审批单已自动失效。'
                    await self._persist_ticket(ticket)
                    cancelled += 1
        return cancelled
