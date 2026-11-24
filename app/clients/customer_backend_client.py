"""User-only GET client. Do not reuse the administrator credential context."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import re

import httpx

from app.clients.http_pool import get_shared_backend_client
from app.config import get_settings

current_customer_token: ContextVar[str | None] = ContextVar('customer_token', default=None)
current_customer_id: ContextVar[int | None] = ContextVar('customer_id', default=None)
_ORIG_HTTPX = httpx
_ORIG_ASYNC_CLIENT = httpx.AsyncClient

_READ_PATHS = {
    '/user/agent/identity', '/user/shop/status', '/user/category/list',
    '/user/dish/list', '/user/setmeal/list', '/user/order/historyOrders',
}


def _is_httpx_patched() -> bool:
    return httpx is not _ORIG_HTTPX or getattr(httpx, 'AsyncClient', None) is not _ORIG_ASYNC_CLIENT


class CustomerBackendClient:
    def __init__(self, token: str | None = None):
        settings = get_settings()
        self.token = token if token is not None else current_customer_token.get()
        self.base_url = settings.sky_backend_base_url.rstrip('/')
        self.timeout_seconds = getattr(settings, 'backend_http_timeout_seconds', 10.0)

    @asynccontextmanager
    async def _borrow_client(self):
        if not _is_httpx_patched():
            pooled = get_shared_backend_client(self.base_url)
            if pooled is not None:
                yield pooled
                return
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds, trust_env=False) as client:
            yield client

    def _headers(self):
        if not isinstance(self.token, str) or not self.token.strip():
            raise RuntimeError('缺少当前用户凭证')
        return {'authentication': self.token}

    async def get(self, path: str, params: dict | None = None) -> dict:
        if path not in _READ_PATHS and not re.fullmatch(r'/user/(?:setmeal/dish|order/orderDetail)/[1-9][0-9]*', path):
            raise ValueError('用户工具不允许访问该接口')
        # GET reminder is a write action in the legacy API and is intentionally NOT allowed.
        async with self._borrow_client() as client:
            response = await client.get(path, params=params, headers=self._headers())
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError('用户业务接口返回了非 JSON 响应') from None
        if not isinstance(payload, dict) or payload.get('code') != 1:
            raise RuntimeError('用户业务接口未返回成功结果')
        return payload
