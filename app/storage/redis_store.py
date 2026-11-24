"""Async Redis persistence for AgentState sessions, report reviews, and HITL approvals.

Security & reliability invariants:
- Never store or log admin/customer JWT credentials or Redis connection passwords.
- Enforce strict `(role, operator_id, session_id)` key namespacing on every read/write.
- Use atomic `SET NX PX` + compare-and-delete unlock for multi-worker approval confirmation.
- Degrade gracefully on transient Redis I/O failures so chat availability is preserved.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import secrets
from typing import Any

from agentscope.state import AgentState
import redis.asyncio as aioredis

from app.models.response import ReportReviewStatusResponse

logger = logging.getLogger(__name__)

_UNLOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RedisStateStore:
    """Async Redis store backing sessions, async report reviews, and HITL approvals."""

    def __init__(
        self,
        *,
        redis_url: str = 'redis://localhost:6379/1',
        key_prefix: str = 'sky_agent',
        socket_timeout: float = 1.5,
        client: Any | None = None,
    ) -> None:
        self._redis_url = redis_url
        self.key_prefix = (key_prefix or 'sky_agent').strip(':')
        self.socket_timeout = float(socket_timeout)
        self._injected_client = client
        self._client: Any | None = client
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _get_client(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=self.socket_timeout,
                socket_connect_timeout=self.socket_timeout,
            )
            self._client_loop = loop
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self._get_client().ping())
        except Exception:
            logger.warning('Redis state store ping failed; operating with fallback')
            return False

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None and self._injected_client is None:
            try:
                if hasattr(client, 'aclose'):
                    await client.aclose()
                elif hasattr(client, 'close'):
                    res = client.close()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass

    # ---------------- Session (AgentState) ----------------

    def _session_key(self, role: str, operator_id: int, session_id: str) -> str:
        return f'{self.key_prefix}:session:{role}:{operator_id}:{session_id}'

    async def load_agent_state(self, role: str, operator_id: int, session_id: str) -> AgentState | None:
        try:
            raw = await self._get_client().get(self._session_key(role, operator_id, session_id))
            if not raw:
                return None
            return AgentState.model_validate_json(raw)
        except Exception:
            logger.warning('Failed to load session state from Redis; starting fresh in-memory state')
            return None

    async def save_agent_state(
        self,
        role: str,
        operator_id: int,
        session_id: str,
        state: AgentState,
        *,
        ttl_seconds: int,
    ) -> bool:
        try:
            payload = state.model_dump_json()
            await self._get_client().set(
                self._session_key(role, operator_id, session_id),
                payload,
                ex=max(1, int(ttl_seconds)),
            )
            return True
        except Exception:
            logger.warning('Failed to persist session state to Redis')
            return False

    async def delete_agent_state(self, role: str, operator_id: int, session_id: str) -> bool:
        try:
            deleted = await self._get_client().delete(self._session_key(role, operator_id, session_id))
            return bool(deleted)
        except Exception:
            logger.warning('Failed to delete session state from Redis')
            return False

    # ---------------- Report Review ----------------

    def _review_key(self, operator_id: int, session_id: str, review_id: str) -> str:
        return f'{self.key_prefix}:review:{operator_id}:{session_id}:{review_id}'

    def _review_idx_key(self, operator_id: int, session_id: str) -> str:
        return f'{self.key_prefix}:review_idx:{operator_id}:{session_id}'

    async def save_review_status(
        self,
        *,
        operator_id: int,
        session_id: str,
        status: ReportReviewStatusResponse,
        ttl_seconds: float,
    ) -> bool:
        ttl = max(1, int(round(ttl_seconds)))
        rkey = self._review_key(operator_id, session_id, status.review_id)
        ikey = self._review_idx_key(operator_id, session_id)
        try:
            client = self._get_client()
            await client.set(rkey, status.model_dump_json(), ex=ttl)
            await client.sadd(ikey, status.review_id)
            await client.expire(ikey, ttl)
            return True
        except Exception:
            logger.warning('Failed to persist report review status to Redis')
            return False

    async def load_review_status(
        self,
        *,
        operator_id: int,
        session_id: str,
        review_id: str,
    ) -> ReportReviewStatusResponse | None:
        try:
            raw = await self._get_client().get(self._review_key(operator_id, session_id, review_id))
            if not raw:
                return None
            parsed = ReportReviewStatusResponse.model_validate_json(raw)
            if parsed.session_id != session_id:
                return None
            return parsed
        except Exception:
            logger.warning('Failed to load report review status from Redis')
            return None

    async def delete_session_reviews(self, *, operator_id: int, session_id: str) -> int:
        ikey = self._review_idx_key(operator_id, session_id)
        try:
            client = self._get_client()
            members = await client.smembers(ikey)
            keys = [self._review_key(operator_id, session_id, rid) for rid in (members or ())]
            keys.append(ikey)
            deleted = await client.delete(*keys)
            return max(0, int(deleted) - 1) if members else 0
        except Exception:
            logger.warning('Failed to delete session reviews from Redis')
            return 0

    # ---------------- HITL Approvals & Distributed Lock ----------------

    def _approval_key(self, approval_id: str) -> str:
        return f'{self.key_prefix}:approval:{approval_id}'

    def _approval_idx_key(self, operator_id: int, session_id: str) -> str:
        return f'{self.key_prefix}:approval_idx:{operator_id}:{session_id}'

    def _approval_lock_key(self, approval_id: str) -> str:
        return f'{self.key_prefix}:approval_lock:{approval_id}'

    async def save_approval_record(self, record: dict[str, Any], *, ttl_seconds: float) -> bool:
        ttl = max(1, int(round(ttl_seconds)))
        approval_id = str(record['approval_id'])
        operator_id = int(record['operator_id'])
        session_id = str(record['session_id'])
        akey = self._approval_key(approval_id)
        ikey = self._approval_idx_key(operator_id, session_id)
        try:
            client = self._get_client()
            await client.set(akey, json.dumps(record, ensure_ascii=False), ex=ttl)
            await client.sadd(ikey, approval_id)
            await client.expire(ikey, ttl)
            return True
        except Exception:
            logger.warning('Failed to persist approval ticket to Redis')
            return False

    async def load_approval_record(self, approval_id: str) -> dict[str, Any] | None:
        try:
            raw = await self._get_client().get(self._approval_key(approval_id))
            if not raw:
                return None
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            logger.warning('Failed to load approval ticket from Redis')
            return None

    async def list_session_approval_ids(self, *, operator_id: int, session_id: str) -> list[str]:
        try:
            members = await self._get_client().smembers(self._approval_idx_key(operator_id, session_id))
            return [str(m) for m in (members or ())]
        except Exception:
            logger.warning('Failed to list session approvals from Redis')
            return []

    @asynccontextmanager
    async def approval_lock(self, approval_id: str, *, lock_ms: int = 15000, wait_seconds: float = 3.0):
        """Acquire a per-ticket distributed Redis lock (`SET NX PX`) with safe release."""
        lkey = self._approval_lock_key(approval_id)
        token = secrets.token_hex(16)
        acquired = False
        client = None
        try:
            client = self._get_client()
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while True:
                ok = await client.set(lkey, token, nx=True, px=lock_ms)
                if ok:
                    acquired = True
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.025)
        except Exception:
            logger.warning('Redis distributed lock unavailable; falling back to in-process ticket lock')
            client = None

        try:
            yield acquired
        finally:
            if acquired and client is not None:
                try:
                    await client.eval(_UNLOCK_LUA, 1, lkey, token)
                except Exception:
                    try:
                        cur = await client.get(lkey)
                        if cur == token:
                            await client.delete(lkey)
                    except Exception:
                        pass
