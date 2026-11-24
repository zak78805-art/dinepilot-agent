"""Single-event-loop bounded session ownership with optional Redis AgentState persistence."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import sys
import time
from typing import Any


class SessionCapacityError(RuntimeError):
    pass


class SessionBusyError(RuntimeError):
    pass


@dataclass
class _Entry:
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    agent: object = None


def _unpack_session_key(key: Any) -> tuple[str, int, str] | None:
    if (
        isinstance(key, tuple)
        and len(key) == 3
        and isinstance(key[0], str)
        and type(key[1]) is int
        and isinstance(key[2], str)
    ):
        return key[0], key[1], key[2]
    return None


class SessionStore:
    def __init__(self, max_entries=500, idle_seconds=1800, *, clock=time.monotonic,
                 disposer=None, close_timeout=2.0, state_store=None):
        if (type(max_entries) is not int or max_entries <= 0
                or type(idle_seconds) is not int or idle_seconds <= 0):
            raise ValueError('Session limits must be positive integers')
        self.max_entries, self.idle_seconds = max_entries, idle_seconds
        self._clock, self._disposer = clock, disposer
        self._close_timeout = close_timeout
        self._state_store = state_store
        self._entries = {}

    def __len__(self):
        return len(self._entries)

    def _expire(self):
        now = self._clock()
        retired = []
        for key, entry in list(self._entries.items()):
            if entry.users == 0 and now - entry.last_used >= self.idle_seconds:
                del self._entries[key]
                if entry.agent is not None:
                    retired.append(entry.agent)
        return retired

    async def _dispose(self, agents):
        if not agents or self._disposer is None:
            return
        async def close(agent):
            try:
                await asyncio.wait_for(self._disposer(agent), self._close_timeout)
            except Exception:
                try:
                    sys.stderr.write('Session resource cleanup not confirmed\n')
                except Exception:
                    pass
        await asyncio.gather(*(close(agent) for agent in agents))

    @asynccontextmanager
    async def use(self, key, factory):
        retired = self._expire()
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self.max_entries:
                await self._dispose(retired)
                raise SessionCapacityError('会话容量已满，请清理会话或稍后重试')
            entry = _Entry(self._clock())
            self._entries[key] = entry
        # No await between locating the entry and pinning it, including waiters.
        entry.users += 1
        try:
            # Reclaim before waiting or running a potentially long reply; otherwise
            # queued requests can each retain an unbounded batch of retired agents.
            try:
                await self._dispose(retired)
            finally:
                retired.clear()
            async with entry.lock:
                if entry.agent is None:
                    constructed = factory()
                    unpacked = _unpack_session_key(key)
                    if (
                        self._state_store is not None
                        and unpacked is not None
                        and getattr(constructed, 'state', None) is not None
                    ):
                        restored = await self._state_store.load_agent_state(*unpacked)
                        if restored is not None:
                            constructed.state = restored
                    entry.agent = constructed
                try:
                    yield entry.agent
                finally:
                    unpacked = _unpack_session_key(key)
                    if (
                        self._state_store is not None
                        and unpacked is not None
                        and getattr(entry.agent, 'state', None) is not None
                    ):
                        await self._state_store.save_agent_state(
                            *unpacked,
                            entry.agent.state,
                            ttl_seconds=self.idle_seconds,
                        )
        finally:
            entry.users -= 1
            entry.last_used = self._clock()
            if entry.agent is None and entry.users == 0:
                self._entries.pop(key, None)

    async def clear(self, key):
        retired = self._expire()
        entry = self._entries.get(key)
        local_cleared = False
        try:
            if entry is not None:
                if entry.users:
                    raise SessionBusyError('会话正在使用，请稍后再试')
                del self._entries[key]
                if entry.agent is not None:
                    retired.append(entry.agent)
                local_cleared = True
            remote_cleared = False
            unpacked = _unpack_session_key(key)
            if self._state_store is not None and unpacked is not None:
                remote_cleared = await self._state_store.delete_agent_state(*unpacked)
            return local_cleared or remote_cleared
        finally:
            await self._dispose(retired)
