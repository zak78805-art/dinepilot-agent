from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import httpx

from app.clients.http_pool import get_shared_backend_client
from app.config import get_settings

current_backend_token: ContextVar[str | None] = ContextVar('backend_token', default=None)
_ORIG_HTTPX = httpx
_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _is_httpx_patched() -> bool:
    return httpx is not _ORIG_HTTPX or getattr(httpx, 'AsyncClient', None) is not _ORIG_ASYNC_CLIENT


class SkyBackendClient:
    """统一访问苍穹外卖 Java 后端，Agent 工具不直接访问数据库。"""

    def __init__(self, token: str | None = None) -> None:
        settings = get_settings()
        self.base_url = settings.sky_backend_base_url.rstrip("/")
        self.timeout_seconds = getattr(settings, 'backend_http_timeout_seconds', 10.0)
        self.token = token if token is not None else current_backend_token.get()

    @asynccontextmanager
    async def _borrow_client(self):
        if not _is_httpx_patched():
            pooled = get_shared_backend_client(self.base_url)
            if pooled is not None:
                yield pooled
                return
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            yield client

    def _headers(self) -> dict[str, str]:
        if not isinstance(self.token, str) or not self.token.strip():
            raise RuntimeError('缺少当前管理员凭证')
        # 苍穹外卖 JwtTokenAdminInterceptor 从名为 token 的请求头取令牌。
        return {"token": self.token}

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("苍穹外卖后端返回了非 JSON 响应") from None
        if not isinstance(payload, dict):
            raise RuntimeError("苍穹外卖后端返回格式异常")
        return payload

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        async with self._borrow_client() as client:
            response = await client.get(path, params=params, headers=self._headers())
            response.raise_for_status()
            payload = self._parse_json(response)
            self._raise_for_business_error(payload)
            return payload

    async def post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict:
        async with self._borrow_client() as client:
            response = await client.post(path, params=params, json=json, headers=self._headers())
            response.raise_for_status()
            payload = self._parse_json(response)
            self._raise_for_business_error(payload)
            return payload

    async def put(self, path: str, json: dict[str, Any] | None = None) -> dict:
        async with self._borrow_client() as client:
            response = await client.put(path, json=json, headers=self._headers())
            response.raise_for_status()
            payload = self._parse_json(response)
            self._raise_for_business_error(payload)
            return payload

    @staticmethod
    def _raise_for_business_error(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError("苍穹外卖后端返回格式异常")
        if payload.get("code") != 1:
            message = payload.get("msg") or "苍穹外卖后端返回业务错误"
            raise RuntimeError(message)
