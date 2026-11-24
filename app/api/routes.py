import asyncio
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
import httpx
from openai import OpenAIError

from app.agent.customer_agent import CustomerAgent
from app.agent.merchant_agent import MerchantAgent
from app.approval.manager import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
)
from app.clients.customer_backend_client import current_customer_id, current_customer_token
from app.clients.sky_backend_client import current_backend_token
from app.config import get_settings
from app.memory.session_store import SessionBusyError, SessionCapacityError
from app.models.request import AgentChatRequest, ApprovalDecisionRequest, CustomerChatRequest
from app.models.response import (
    AgentChatResponse,
    ApprovalTicketResponse,
    ReportReviewStatusResponse,
    SessionClearResponse,
)
from app.security.auth import AdminIdentity, require_admin
from app.security.customer_auth import CustomerIdentity, require_customer
from app.security.rate_limit import ActorRateLimiter, RateLimitExceededError


router = APIRouter(prefix="/api/agent", tags=["agent"])
_settings = get_settings()
merchant_agent = MerchantAgent()
customer_agent = CustomerAgent()
chat_rate_limiter = ActorRateLimiter(
    max_requests_per_minute=getattr(_settings, 'chat_rate_limit_per_minute', 20),
    daily_token_budget=getattr(_settings, 'daily_token_budget_per_user', 200_000),
    state_store=merchant_agent.state_store,
)
merchant_agent._token_recorder = chat_rate_limiter.record_tokens
customer_agent._token_recorder = chat_rate_limiter.record_tokens


async def _enforce_chat_rate_limit(role: str, operator_id: int) -> None:
    try:
        await chat_rate_limiter.check_and_consume_request(role=role, operator_id=operator_id)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=exc.message,
            headers={'Retry-After': str(exc.retry_after)},
        ) from None


def _format_sse(event: str, payload: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'


def _build_merchant_sse_response(
    request: AgentChatRequest,
    admin: AdminIdentity,
    http_request: Request | None = None,
) -> StreamingResponse:
    async def event_stream():
        token = current_backend_token.set(admin.token)
        stream_gen = merchant_agent.chat_stream(request, operator_id=admin.operator_id)
        try:
            async for event, payload in stream_gen:
                if http_request is not None and await http_request.is_disconnected():
                    break
                yield _format_sse(event, payload)
        except asyncio.CancelledError:
            raise
        except SessionCapacityError:
            yield _format_sse('error', {'code': 'SESSION_CAPACITY', 'message': '会话容量已满，请清理会话或稍后重试'})
        except (httpx.RequestError, httpx.HTTPStatusError):
            yield _format_sse('error', {'code': 'BACKEND_ERROR', 'message': '业务数据服务调用失败，请检查后端状态'})
        except (RuntimeError, OpenAIError):
            yield _format_sse('error', {'code': 'AGENT_ERROR', 'message': 'Agent 调用失败，请检查模型配置或后端数据'})
        finally:
            try:
                await stream_gen.aclose()
            except Exception:
                pass
            current_backend_token.reset(token)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream; charset=utf-8',
        headers={
            'Cache-Control': 'no-store, no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@router.post("/chat", response_model=AgentChatResponse)
@router.post("/merchant/chat", response_model=AgentChatResponse)
async def chat(
    request: AgentChatRequest,
    http_request: Request,
    admin: AdminIdentity = Depends(require_admin),
):
    await _enforce_chat_rate_limit('merchant', admin.operator_id)
    accept = (http_request.headers.get('accept') or '').lower()
    if 'text/event-stream' in accept:
        return _build_merchant_sse_response(request, admin, http_request)
    token = current_backend_token.set(admin.token)
    try:
        return await merchant_agent.chat(request, operator_id=admin.operator_id)
    except SessionCapacityError:
        raise HTTPException(503, '会话容量已满，请清理会话或稍后重试') from None
    except (httpx.RequestError, httpx.HTTPStatusError):
        raise HTTPException(502, '业务数据服务调用失败，请检查后端状态') from None
    except (RuntimeError, OpenAIError):
        raise HTTPException(502, 'Agent 调用失败，请检查模型配置或后端数据') from None
    finally:
        current_backend_token.reset(token)


@router.post('/merchant/chat/stream')
async def merchant_chat_stream(
    request: AgentChatRequest,
    http_request: Request,
    admin: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    await _enforce_chat_rate_limit('merchant', admin.operator_id)
    return _build_merchant_sse_response(request, admin, http_request)


@router.post('/customer/chat', response_model=AgentChatResponse)
async def customer_chat(request: CustomerChatRequest,
                        customer: CustomerIdentity = Depends(require_customer)) -> AgentChatResponse:
    await _enforce_chat_rate_limit('customer', customer.user_id)
    credential = current_customer_token.set(customer.token)
    identity = current_customer_id.set(customer.user_id)
    try:
        return await customer_agent.chat(request, operator_id=customer.user_id)
    except SessionCapacityError:
        raise HTTPException(503, '会话容量已满，请清理会话或稍后重试') from None
    except (httpx.RequestError, httpx.HTTPStatusError):
        raise HTTPException(502, '用户业务服务调用失败') from None
    except (RuntimeError, OpenAIError):
        raise HTTPException(502, '用户助手调用失败，请检查模型或用户接口配置') from None
    finally:
        current_customer_id.reset(identity)
        current_customer_token.reset(credential)


@router.delete('/merchant/sessions/{session_id}', response_model=SessionClearResponse)
async def clear_merchant_session(
    session_id: str = Path(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$'),
    admin: AdminIdentity = Depends(require_admin),
):
    try:
        cleared = await merchant_agent.clear_session(session_id, admin.operator_id)
    except SessionBusyError:
        raise HTTPException(409, '会话正在使用，请稍后再试') from None
    return SessionClearResponse(agent_role='merchant', session_id=session_id, cleared=cleared)


@router.delete('/customer/sessions/{session_id}', response_model=SessionClearResponse)
async def clear_customer_session(
    session_id: str = Path(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$'),
    customer: CustomerIdentity = Depends(require_customer),
):
    try:
        cleared = await customer_agent.clear_session(session_id, customer.user_id)
    except SessionBusyError:
        raise HTTPException(409, '会话正在使用，请稍后再试') from None
    return SessionClearResponse(agent_role='customer', session_id=session_id, cleared=cleared)


@router.get(
    '/merchant/sessions/{session_id}/report-reviews/{review_id}',
    response_model=ReportReviewStatusResponse,
)
async def get_merchant_report_review(
    session_id: str = Path(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$'),
    review_id: str = Path(min_length=8, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$'),
    admin: AdminIdentity = Depends(require_admin),
) -> ReportReviewStatusResponse:
    if hasattr(merchant_agent, 'aget_report_review'):
        status = await merchant_agent.aget_report_review(
            review_id=review_id,
            operator_id=admin.operator_id,
            session_id=session_id,
        )
    else:
        status = merchant_agent.get_report_review(
            review_id=review_id,
            operator_id=admin.operator_id,
            session_id=session_id,
        )
    if status is None:
        raise HTTPException(404, '复核任务不存在、已过期或无权访问')
    return status


@router.get(
    '/merchant/approvals/{approval_id}',
    response_model=ApprovalTicketResponse,
)
async def get_merchant_approval(
    approval_id: str = Path(min_length=8, max_length=64, pattern=r'^apr_[a-zA-Z0-9_-]+$'),
    admin: AdminIdentity = Depends(require_admin),
) -> ApprovalTicketResponse:
    if hasattr(merchant_agent, 'aget_approval'):
        ticket = await merchant_agent.aget_approval(approval_id=approval_id, operator_id=admin.operator_id)
    else:
        ticket = merchant_agent.get_approval(approval_id=approval_id, operator_id=admin.operator_id)
    if ticket is None:
        raise HTTPException(404, '审批单不存在或无权访问')
    return ticket


@router.post(
    '/merchant/approvals/{approval_id}/confirm',
    response_model=ApprovalTicketResponse,
)
async def confirm_merchant_approval(
    approval_id: str = Path(min_length=8, max_length=64, pattern=r'^apr_[a-zA-Z0-9_-]+$'),
    decision: ApprovalDecisionRequest | None = Body(default=None),
    admin: AdminIdentity = Depends(require_admin),
) -> ApprovalTicketResponse:
    token = current_backend_token.set(admin.token)
    try:
        return await merchant_agent.confirm_approval(
            approval_id=approval_id,
            operator_id=admin.operator_id,
            reason=decision.reason if decision is not None else None,
        )
    except ApprovalNotFoundError:
        raise HTTPException(404, '审批单不存在或无权操作') from None
    except ApprovalExpiredError:
        raise HTTPException(410, '审批单已过期，请重新发起修改请求') from None
    except ApprovalConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError, ValueError):
        raise HTTPException(502, '执行菜品状态更新失败，请检查后端状态后重新发起') from None
    finally:
        current_backend_token.reset(token)


@router.post(
    '/merchant/approvals/{approval_id}/reject',
    response_model=ApprovalTicketResponse,
)
async def reject_merchant_approval(
    approval_id: str = Path(min_length=8, max_length=64, pattern=r'^apr_[a-zA-Z0-9_-]+$'),
    decision: ApprovalDecisionRequest | None = Body(default=None),
    admin: AdminIdentity = Depends(require_admin),
) -> ApprovalTicketResponse:
    try:
        return await merchant_agent.reject_approval(
            approval_id=approval_id,
            operator_id=admin.operator_id,
            reason=decision.reason if decision is not None else None,
        )
    except ApprovalNotFoundError:
        raise HTTPException(404, '审批单不存在或无权操作') from None
    except ApprovalExpiredError:
        raise HTTPException(410, '审批单已过期') from None
    except ApprovalConflictError as exc:
        raise HTTPException(409, str(exc)) from None
