"""Single-process bounded in-memory async report review task manager."""
import asyncio
from collections.abc import Awaitable, Callable
import contextvars
from dataclasses import dataclass
import math
from pathlib import Path
import secrets
import time
from typing import Literal

from app.models.response import (
    ReportReviewFailure,
    ReportReviewIssue,
    ReportReviewRef,
    ReportReviewStatus,
    ReportReviewStatusResponse,
)
from app.observability.context import get_current_trace_id
from app.observability.telemetry import record_review_telemetry
from app.review.numeric_rules import ReportMetrics, check_numeric_rules
from app.review.text_reviewer import Fact, Generate, Issue, review_text


_TERMINAL_STATUSES = frozenset({'pass', 'revise', 'unavailable'})


def _validate_positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _validate_positive_float(name: str, value: object, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a positive number')
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f'{name} must be a positive finite number')
    if maximum is not None and result > maximum:
        raise ValueError(f'{name} must be <= {maximum}')
    return result


@dataclass
class _ReviewRecord:
    review_id: str
    agent_role: Literal['merchant']
    operator_id: int
    session_id: str
    status: ReportReviewStatus
    evidence_ids: tuple[str, ...]
    created_at: float
    updated_at: float
    issues: tuple[Issue, ...] = ()
    failure: ReportReviewFailure | None = None
    trace_id: str | None = None
    task: asyncio.Task | None = None


class ReportReviewManager:
    """Bounded async review pool; background tasks never inherit request context or tokens."""

    def __init__(
        self,
        generate: Generate | None = None,
        *,
        max_entries: int = 200,
        max_concurrency: int = 4,
        max_pending: int = 16,
        ttl_seconds: float = 600.0,
        timeout: float = 15.0,
        queue_timeout: float | None = None,
        poll_after_ms: int = 1000,
        closer: Callable[[], Awaitable[None]] | None = None,
        state_store=None,
        telemetry_log_path: Path | None = None,
        clock=time.monotonic,
    ) -> None:
        self.max_entries = _validate_positive_int('max_entries', max_entries)
        self.max_concurrency = _validate_positive_int('max_concurrency', max_concurrency)
        self.max_pending = _validate_positive_int('max_pending', max_pending)
        if self.max_concurrency > self.max_pending:
            raise ValueError('max_concurrency must be <= max_pending')
        if self.max_pending > self.max_entries:
            raise ValueError('max_pending must be <= max_entries')
        self.ttl_seconds = _validate_positive_float('ttl_seconds', ttl_seconds)
        self.timeout = _validate_positive_float('timeout', timeout, maximum=120.0)
        self.queue_timeout = _validate_positive_float(
            'queue_timeout',
            self.timeout if queue_timeout is None else queue_timeout,
            maximum=120.0,
        )
        self.poll_after_ms = _validate_positive_int('poll_after_ms', poll_after_ms)
        self._closer = closer
        self._state_store = state_store
        self._telemetry_log_path = telemetry_log_path
        self._clock = clock
        self._generate = generate
        self._entries: dict[str, _ReviewRecord] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return self._generate is not None

    def configure_generator(self, generate: Generate | None) -> None:
        self._generate = generate

    def __len__(self) -> int:
        return len(self._entries)

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
            self._semaphore_loop = loop
        return self._semaphore

    def _expire(self) -> None:
        now = self._clock()
        expired = [
            review_id
            for review_id, record in self._entries.items()
            if record.status in _TERMINAL_STATUSES and (now - record.updated_at) >= self.ttl_seconds
        ]
        for review_id in expired:
            self._entries.pop(review_id, None)

    def _evict_finished_if_needed(self) -> bool:
        if len(self._entries) < self.max_entries:
            return True
        finished = sorted(
            (r for r in self._entries.values() if r.status in _TERMINAL_STATUSES),
            key=lambda item: item.updated_at,
        )
        for record in finished:
            self._entries.pop(record.review_id, None)
            if len(self._entries) < self.max_entries:
                return True
        return len(self._entries) < self.max_entries

    def _active_count(self) -> int:
        return sum(1 for record in self._entries.values() if record.status not in _TERMINAL_STATUSES)

    def submit(
        self,
        *,
        operator_id: int,
        session_id: str,
        draft: str,
        evidence: tuple[Fact, ...],
        metrics: ReportMetrics | None = None,
        trace_id: str | None = None,
    ) -> ReportReviewRef | None:
        """Schedule an isolated async review without waiting or retaining admin credentials."""
        self._expire()
        generate = self._generate
        if generate is None:
            return None
        if not isinstance(draft, str) or not draft.strip() or not evidence:
            return None
        if len(draft) > 6000:
            return ReportReviewRef(review_id=None, status='unavailable', failure='bad_input')

        if self._active_count() >= self.max_pending or not self._evict_finished_if_needed():
            return ReportReviewRef(review_id=None, status='unavailable', failure='queue_full')

        review_id = secrets.token_urlsafe(18)
        now = self._clock()
        record = _ReviewRecord(
            review_id=review_id,
            agent_role='merchant',
            operator_id=operator_id,
            session_id=session_id,
            status='pending',
            evidence_ids=tuple(fact.id for fact in evidence),
            created_at=now,
            updated_at=now,
            trace_id=trace_id or get_current_trace_id(),
        )
        self._entries[review_id] = record

        # Spawn with a fresh empty Context so request-scoped tokens/identity never leak.
        isolated_ctx = contextvars.Context()
        record.task = asyncio.create_task(
            self._run_review(record, draft, evidence, metrics, generate),
            context=isolated_ctx,
        )
        return ReportReviewRef(
            review_id=review_id,
            status='pending',
            poll_after_ms=self.poll_after_ms,
        )

    @staticmethod
    def _record_to_response(record: _ReviewRecord) -> ReportReviewStatusResponse:
        return ReportReviewStatusResponse(
            agent_role='merchant',
            session_id=record.session_id,
            review_id=record.review_id,
            status=record.status,
            issues=[
                ReportReviewIssue(
                    code=issue.code,
                    quote=issue.quote,
                    reason=issue.reason,
                    suggestion=issue.suggestion,
                    evidence_ids=list(issue.evidence_ids),
                )
                for issue in record.issues
            ],
            failure=record.failure,
            evidence_ids=list(record.evidence_ids),
        )

    async def _persist_record(self, record: _ReviewRecord) -> None:
        if self._state_store is None or self._entries.get(record.review_id) is not record:
            return
        await self._state_store.save_review_status(
            operator_id=record.operator_id,
            session_id=record.session_id,
            status=self._record_to_response(record),
            ttl_seconds=self.ttl_seconds,
        )

    def _emit_telemetry(self, record: _ReviewRecord, *, precheck_hit: bool, started_mono: float) -> None:
        record_review_telemetry(
            operator_id=record.operator_id,
            session_id=record.session_id,
            review_id=record.review_id,
            verdict=record.status,
            precheck_hit=precheck_hit,
            issue_codes=[i.code for i in record.issues],
            failure=record.failure,
            elapsed_ms=(time.monotonic() - started_mono) * 1000,
            trace_id=record.trace_id,
            log_path=self._telemetry_log_path,
        )

    async def _run_review(
        self,
        record: _ReviewRecord,
        draft: str,
        evidence: tuple[Fact, ...],
        metrics: ReportMetrics | None,
        generate: Generate,
    ) -> None:
        started_mono = time.monotonic()
        await self._persist_record(record)
        # Step 1: fast deterministic numeric/scope pre-check (0 model tokens).
        precheck_issues = check_numeric_rules(draft, metrics, set(record.evidence_ids))
        if precheck_issues:
            record.status = 'revise'
            record.issues = precheck_issues
            record.failure = None
            record.updated_at = self._clock()
            record.task = None
            await self._persist_record(record)
            self._emit_telemetry(record, precheck_hit=True, started_mono=started_mono)
            return

        semaphore = self._get_semaphore()
        acquired = False
        try:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=self.queue_timeout)
                acquired = True
            except TimeoutError:
                record.status = 'unavailable'
                record.failure = 'timeout'
                record.issues = ()
                record.updated_at = self._clock()
                await self._persist_record(record)
                self._emit_telemetry(record, precheck_hit=False, started_mono=started_mono)
                return

            if self._entries.get(record.review_id) is not record:
                return

            record.status = 'running'
            record.updated_at = self._clock()
            await self._persist_record(record)

            outcome = await review_text(draft, evidence, generate, timeout=self.timeout)
            record.status = outcome.verdict
            record.issues = outcome.issues
            record.failure = outcome.failure
            record.updated_at = self._clock()
            await self._persist_record(record)
            self._emit_telemetry(record, precheck_hit=False, started_mono=started_mono)
        except asyncio.CancelledError:
            if self._entries.get(record.review_id) is record:
                record.status = 'unavailable'
                record.failure = 'cancelled'
                record.issues = ()
                record.updated_at = self._clock()
                await self._persist_record(record)
                self._emit_telemetry(record, precheck_hit=False, started_mono=started_mono)
            raise
        except Exception:
            record.status = 'unavailable'
            record.failure = 'model_error'
            record.issues = ()
            record.updated_at = self._clock()
            await self._persist_record(record)
            self._emit_telemetry(record, precheck_hit=False, started_mono=started_mono)
        finally:
            if acquired:
                semaphore.release()
            record.task = None

    def get(
        self,
        *,
        review_id: str,
        operator_id: int,
        session_id: str,
    ) -> ReportReviewStatusResponse | None:
        """Read review status only if owned by the authenticated merchant operator and session."""
        self._expire()
        record = self._entries.get(review_id)
        if record is None:
            return None
        if (record.agent_role != 'merchant'
                or record.operator_id != operator_id
                or record.session_id != session_id):
            return None
        return self._record_to_response(record)

    async def aget(
        self,
        *,
        review_id: str,
        operator_id: int,
        session_id: str,
    ) -> ReportReviewStatusResponse | None:
        local = self.get(review_id=review_id, operator_id=operator_id, session_id=session_id)
        if local is not None:
            return local
        if self._state_store is not None:
            return await self._state_store.load_review_status(
                operator_id=operator_id,
                session_id=session_id,
                review_id=review_id,
            )
        return None

    def cancel_session(self, *, operator_id: int, session_id: str) -> int:
        """Cancel and drop all review tasks belonging to a cleared merchant session."""
        removed = 0
        for review_id, record in list(self._entries.items()):
            if record.operator_id == operator_id and record.session_id == session_id:
                self._entries.pop(review_id, None)
                if record.task is not None and not record.task.done():
                    record.task.cancel()
                removed += 1
        return removed

    async def acancel_session(self, *, operator_id: int, session_id: str) -> int:
        removed = self.cancel_session(operator_id=operator_id, session_id=session_id)
        if self._state_store is not None:
            remote = await self._state_store.delete_session_reviews(
                operator_id=operator_id,
                session_id=session_id,
            )
            return max(removed, remote)
        return removed

    async def shutdown(self) -> None:
        """Cancel all active background tasks and release optional reviewer client."""
        tasks = [
            record.task
            for record in self._entries.values()
            if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()
        if self._closer is not None:
            try:
                await self._closer()
            except Exception:
                pass
