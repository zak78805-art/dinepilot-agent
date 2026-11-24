"""Shared httpx.AsyncClient connection pool for Sky Take-Out Java backend calls.

Security invariant:
- Never attach default authentication headers (`token` or `authentication`) to the
  shared client. Every request MUST pass per-request headers derived from its own
  verified ContextVar / explicit credential so admin and customer tokens can never
  cross-contaminate over keep-alive connections.
"""
import asyncio
from dataclasses import dataclass

import httpx


@dataclass
class _PoolEntry:
    client: httpx.AsyncClient
    base_url: str
    loop: asyncio.AbstractEventLoop


_active_pool: _PoolEntry | None = None


def open_shared_backend_client(
    *,
    base_url: str,
    timeout_seconds: float = 10.0,
    max_connections: int = 50,
    max_keepalive_connections: int = 20,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Open (or replace) the process-wide pooled httpx.AsyncClient for the current event loop."""
    global _active_pool
    normalized = base_url.rstrip('/')
    loop = asyncio.get_running_loop()
    if (
        _active_pool is not None
        and not _active_pool.client.is_closed
        and _active_pool.base_url == normalized
        and _active_pool.loop is loop
        and transport is None
    ):
        return _active_pool.client

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=30.0,
    )
    timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
    client = httpx.AsyncClient(
        base_url=normalized,
        timeout=timeout,
        limits=limits,
        trust_env=False,
        transport=transport,
    )
    _active_pool = _PoolEntry(client=client, base_url=normalized, loop=loop)
    return client


def get_shared_backend_client(base_url: str) -> httpx.AsyncClient | None:
    """Return the active pooled client if open on the current loop and matching base_url."""
    if _active_pool is None or _active_pool.client.is_closed:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _active_pool.loop is not loop:
        return None
    if _active_pool.base_url != base_url.rstrip('/'):
        return None
    return _active_pool.client


async def close_shared_backend_client() -> None:
    """Close and clear the shared httpx.AsyncClient pool on lifespan shutdown."""
    global _active_pool
    entry = _active_pool
    _active_pool = None
    if entry is not None and not entry.client.is_closed:
        try:
            await entry.client.aclose()
        except Exception:
            pass
