from dataclasses import dataclass
import httpx
from fastapi import Header, HTTPException
from app.clients.sky_backend_client import SkyBackendClient


@dataclass(frozen=True)
class AdminIdentity:
    operator_id: int
    token: str


def _clean_credential(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned or '\r' in cleaned or '\n' in cleaned:
        return None
    return cleaned


async def require_admin(authorization: str | None = Header(default=None),
                        token: str | None = Header(default=None)) -> AdminIdentity:
    bearer = None
    if authorization:
        scheme, _, value = authorization.partition(' ')
        cleaned = _clean_credential(value)
        if scheme.lower() != 'bearer' or not cleaned:
            raise HTTPException(401, '请使用 Authorization: Bearer <token>')
        bearer = cleaned
    credential = bearer or _clean_credential(token)
    if not credential:
        raise HTTPException(401, '缺少管理员令牌')
    try:
        payload = await SkyBackendClient(token=credential).get('/admin/agent/identity')
        operator_id = payload['data']['operator_id']
        if isinstance(operator_id, bool) or not isinstance(operator_id, int) or operator_id <= 0:
            raise ValueError('invalid operator')
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            raise HTTPException(error.response.status_code, '管理员令牌无效或账号已禁用') from None
        raise HTTPException(502, '后端身份校验失败') from None
    except httpx.RequestError:
        raise HTTPException(503, '苍穹外卖后端未启动或无法连接') from None
    except (RuntimeError, ValueError, KeyError, TypeError):
        raise HTTPException(502, '后端未返回有效管理员身份') from None
    return AdminIdentity(operator_id, credential)
