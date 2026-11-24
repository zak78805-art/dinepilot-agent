"""Externalized state persistence backends (Redis with graceful fallback)."""
from app.storage.redis_store import RedisStateStore

__all__ = ['RedisStateStore']
