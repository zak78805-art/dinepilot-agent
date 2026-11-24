"""Offline unit suite: fail immediately on real IP connections or DNS lookups.

ASGI TestClient and httpx.MockTransport stay in memory. This is a test guard,
not an OS sandbox; it doesn't restrict subprocesses or application production code.
"""
import socket
from threading import local

import pytest

from app.api import routes as _routes
from app.observability import telemetry as _telemetry


@pytest.fixture(autouse=True)
def isolate_telemetry_log(monkeypatch, tmp_path):
    monkeypatch.setattr(_telemetry, '_DEFAULT_TELEMETRY_PATH', tmp_path / 'telemetry.jsonl')
    _routes.chat_rate_limiter.reset()


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    socketpair = socket.socketpair
    internal = local()

    def internal_pair(*args, **kwargs):
        # On Windows stdlib socketpair is implemented via a loopback socket.
        # Permit only the synchronous stdlib construction, never a blanket localhost exemption.
        internal.making_pair = True
        try:
            return socketpair(*args, **kwargs)
        finally:
            internal.making_pair = False

    def denied(*args, **kwargs):
        raise RuntimeError('offline tests forbid real network access; use MockTransport')

    def guarded_connect(self, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not getattr(internal, 'making_pair', False):
            denied()
        return connect(self, *args, **kwargs)

    def guarded_connect_ex(self, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not getattr(internal, 'making_pair', False):
            denied()
        return connect_ex(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    monkeypatch.setattr(socket, 'gethostbyname', denied)
    monkeypatch.setattr(socket, 'gethostbyname_ex', denied)
    monkeypatch.setattr(socket, 'gethostbyaddr', denied)
    monkeypatch.setattr(socket, 'getnameinfo', denied)
    monkeypatch.setattr(socket, 'socketpair', internal_pair)
