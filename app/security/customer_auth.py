from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException

from app.clients.customer_backend_client import CustomerBackendClient


@dataclass(frozen=True)
class CustomerIdentity:
    user_id: int
    token: str


def _clean_credential(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned or '\r' in cleaned or '\n' in cleaned:
        return None
    return cleaned


async def require_customer(authorization: str | None = Header(default=None),
                           authentication: str | None = Header(default=None)) -> CustomerIdentity:
    credential = _clean_credential(authentication)
    if authorization:
        scheme, _, value = authorization.partition(' ')
        cleaned = _clean_credential(value)
        if scheme.lower() != 'bearer' or not cleaned:
            raise HTTPException(401, '请使用 Authorization: Bearer <用户token>')
        credential = cleaned
    if not credential:
        raise HTTPException(401, '缺少用户令牌')
    try:
        payload = await CustomerBackendClient(token=credential).get('/user/agent/identity')
        user_id = payload['data']['user_id']
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError('invalid identity')
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            raise HTTPException(error.response.status_code, '用户令牌无效或用户不存在') from None
        raise HTTPException(502, '后端用户身份校验失败') from None
    except httpx.RequestError:
        raise HTTPException(503, '用户业务后端不可连接') from None
    except (RuntimeError, KeyError, TypeError, ValueError):
        raise HTTPException(502, '后端未返回有效用户身份') from None
    return CustomerIdentity(user_id, credential)
