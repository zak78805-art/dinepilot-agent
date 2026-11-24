"""Per-actor sliding-window RPM rate limiter and daily token budget guard (HTTP 429).

Supports in-memory bounded operation by default (zero network for offline tests)
and optional Redis counters when multi-worker Redis persistence is enabled.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import Literal

logger = logging.getLogger(__name__)
_BEIJING_TZ = timezone(timedelta(hours=8))


class RateLimitExceededError(RuntimeError):
    def __init__(
        self,
        *,
        retry_after: int,
        reason: Literal['rpm', 'daily_token_budget'],
        message: str,
    ) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))
        self.reason = reason
        self.message = message


def _beijing_date_and_seconds_to_midnight() -> tuple[str, int]:
    now = datetime.now(_BEIJING_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remaining = max(1, int(math.ceil((tomorrow - now).total_seconds())))
    return now.date().isoformat(), remaining


class ActorRateLimiter:
    """Bounded per-(role, actor_id) sliding-window RPM limiter + daily token budget."""

    def __init__(
        self,
        *,
        max_requests_per_minute: int = 20,
        daily_token_budget: int = 200_000,
        window_seconds: float = 60.0,
        max_tracked_actors: int = 2000,
        clock=time.monotonic,
        state_store=None,
    ) -> None:
        if type(max_requests_per_minute) is not int or max_requests_per_minute <= 0:
            raise ValueError('max_requests_per_minute must be a positive integer')
        if type(daily_token_budget) is not int or daily_token_budget <= 0:
            raise ValueError('daily_token_budget must be a positive integer')
        self.max_requests_per_minute = max_requests_per_minute
        self.daily_token_budget = daily_token_budget
        self.window_seconds = float(window_seconds)
        self.max_tracked_actors = max_tracked_actors
        self._clock = clock
        self._state_store = state_store
        self._windows: dict[tuple[str, int], deque[float]] = {}
        self._daily_tokens: dict[tuple[str, int, str], int] = {}

    def reset(self) -> None:
        self._windows.clear()
        self._daily_tokens.clear()

    def _prune_stale_days(self, today_str: str) -> None:
        stale = [key for key in self._daily_tokens if key[2] != today_str]
        for key in stale:
            self._daily_tokens.pop(key, None)

    def _evict_windows_if_full(self, now: float) -> None:
        if len(self._windows) < self.max_tracked_actors:
            return
        cutoff = now - self.window_seconds
        empty_keys = []
        for actor, stamps in self._windows.items():
            while stamps and stamps[0] <= cutoff:
                stamps.popleft()
            if not stamps:
                empty_keys.append(actor)
        for actor in empty_keys:
            self._windows.pop(actor, None)
        if len(self._windows) >= self.max_tracked_actors:
            oldest = min(self._windows.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)[0]
            self._windows.pop(oldest, None)

    async def _redis_get_daily_tokens(self, role: str, operator_id: int, today_str: str) -> int | None:
        if self._state_store is None:
            return None
        try:
            key = f'{self._state_store.key_prefix}:tok:{today_str}:{role}:{operator_id}'
            raw = await self._state_store._get_client().get(key)
            return int(raw) if raw is not None else 0
        except Exception:
            logger.warning('Redis daily token check unavailable; using local counter')
            return None

    async def _redis_incr_daily_tokens(
        self,
        role: str,
        operator_id: int,
        today_str: str,
        tokens: int,
        ttl_seconds: int,
    ) -> None:
        if self._state_store is None:
            return
        try:
            client = self._state_store._get_client()
            key = f'{self._state_store.key_prefix}:tok:{today_str}:{role}:{operator_id}'
            await client.incrby(key, tokens)
            await client.expire(key, max(60, ttl_seconds))
        except Exception:
            logger.warning('Redis daily token increment unavailable; tracked locally')

    async def check_and_consume_request(self, *, role: str, operator_id: int) -> None:
        """Enforce daily token budget and per-minute RPM limit for `(role, operator_id)`."""
        today_str, seconds_to_midnight = _beijing_date_and_seconds_to_midnight()
        self._prune_stale_days(today_str)

        # 1. Daily token budget check
        local_used = self._daily_tokens.get((role, operator_id, today_str), 0)
        remote_used = await self._redis_get_daily_tokens(role, operator_id, today_str)
        effective_used = max(local_used, remote_used or 0)
        if effective_used >= self.daily_token_budget:
            raise RateLimitExceededError(
                retry_after=seconds_to_midnight,
                reason='daily_token_budget',
                message='今日 AI 对话 Token 配额已达上限，请明日再试或联系管理员。',
            )

        # 2. Sliding-window RPM check
        now = self._clock()
        actor = (role, operator_id)
        stamps = self._windows.get(actor)
        if stamps is None:
            self._evict_windows_if_full(now)
            stamps = deque()
            self._windows[actor] = stamps

        cutoff = now - self.window_seconds
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()

        if len(stamps) >= self.max_requests_per_minute:
            retry_after = max(1, int(math.ceil(self.window_seconds - (now - stamps[0]))))
            raise RateLimitExceededError(
                retry_after=retry_after,
                reason='rpm',
                message=f'请求过于频繁，请在 {retry_after} 秒后再试。',
            )

        stamps.append(now)

    async def record_tokens(self, *, role: str, operator_id: int, total_tokens: int | None) -> None:
        """Accumulate consumed tokens for `(role, operator_id)` toward the daily budget."""
        if type(total_tokens) is not int or total_tokens <= 0:
            return
        today_str, seconds_to_midnight = _beijing_date_and_seconds_to_midnight()
        self._prune_stale_days(today_str)
        key = (role, operator_id, today_str)
        self._daily_tokens[key] = self._daily_tokens.get(key, 0) + total_tokens
        await self._redis_incr_daily_tokens(role, operator_id, today_str, total_tokens, seconds_to_midnight)
