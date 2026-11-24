"""Request-scoped trace_id ContextVar and zero-buffering pure ASGI middleware."""
from contextvars import ContextVar
import re
import secrets

current_trace_id: ContextVar[str | None] = ContextVar('trace_id', default=None)

_SAFE_TRACE_RE = re.compile(r'^[a-zA-Z0-9_-]{8,64}$')


def sanitize_or_generate_trace_id(raw: str | None = None) -> str:
    """Accept safe upstream X-Trace-Id / X-Request-Id or generate a fresh `trc_...` id."""
    if isinstance(raw, str):
        candidate = raw.strip()
        if _SAFE_TRACE_RE.fullmatch(candidate):
            return candidate
    return f'trc_{secrets.token_hex(12)}'


def get_current_trace_id() -> str | None:
    return current_trace_id.get()


class TraceIdMiddleware:
    """Pure ASGI middleware keeping `current_trace_id` active across JSON and SSE streams."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        incoming: str | None = None
        for raw_key, raw_val in scope.get('headers') or ():
            key_lower = raw_key.lower()
            if key_lower == b'x-trace-id':
                incoming = raw_val.decode('latin1', errors='ignore')
                break
            if key_lower == b'x-request-id' and incoming is None:
                incoming = raw_val.decode('latin1', errors='ignore')

        trace_id = sanitize_or_generate_trace_id(incoming)
        token = current_trace_id.set(trace_id)

        async def send_with_trace_id(message):
            if message.get('type') == 'http.response.start':
                headers = list(message.get('headers') or [])
                existing = {k.lower() for k, _ in headers}
                if b'x-trace-id' not in existing:
                    headers.append((b'x-trace-id', trace_id.encode('ascii')))
                message = {**message, 'headers': headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            current_trace_id.reset(token)
