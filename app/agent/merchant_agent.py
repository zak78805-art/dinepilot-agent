from contextvars import ContextVar
import asyncio
from collections.abc import AsyncIterator
import logging
import time
from datetime import timedelta

from agentscope.agent import Agent, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.event import (
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
)
from agentscope.message import Msg, TextBlock, UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.tool import FunctionTool, Toolkit

from app.agent.prompts import MERCHANT_SYSTEM_PROMPT
from app.approval.manager import ApprovalManager, ApprovalTicket
from app.config import get_settings
from app.models.request import AgentChatRequest
from app.models.response import (
    AgentChatResponse,
    ApprovalTicketResponse,
    ReportReviewStatusResponse,
)
from app.tools.sales_tools import get_today_sales, get_turnover_statistics
from app.tools.order_tools import (
    get_order_statistics,
    get_order_status_overview,
    get_order_list,
    get_order_detail,
)
from app.tools.dish_tools import (
    DISH_STATUS_NAMES,
    execute_dish_status_change,
    get_dish_catalog,
    get_top_dishes,
    lookup_dish_for_status_change,
)
from app.tools.user_tools import get_user_statistics
from app.tools.report_utils import today
from app.audit import record_tool_call
from app.memory.context_compactor import compact_agent_context_if_needed
from app.memory.session_store import SessionStore
from app.observability.context import get_current_trace_id
from app.observability.telemetry import extract_reply_token_usage, record_chat_turn_telemetry
from app.review.evidence import extract_merchant_report_bundle
from app.review.llm_reviewer import build_llm_review_generator
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.storage.redis_store import RedisStateStore
from app.workflow.period_diagnosis import get_period_comparison_report


_tool_calls: ContextVar[list[str] | None] = ContextVar("tool_calls", default=None)
_tool_results: ContextVar[list[tuple[str, dict]] | None] = ContextVar("tool_results", default=None)
_operator: ContextVar[int | None] = ContextVar('operator', default=None)
_actor_role: ContextVar[str] = ContextVar('actor_role', default='merchant')
_session_id: ContextVar[str | None] = ContextVar('session_id', default=None)
_approval_manager: ContextVar[ApprovalManager | None] = ContextVar('approval_manager', default=None)
_pending_approvals: ContextVar[list[ApprovalTicket] | None] = ContextVar('pending_approvals', default=None)


async def _run_tool(name, function, **arguments):
    calls = _tool_calls.get()
    if calls is not None:
        calls.append(name)
    start = time.monotonic()
    succeeded = False
    try:
        result = await function(**arguments)
        succeeded = True
        results = _tool_results.get()
        if results is not None and _actor_role.get() == 'merchant' and isinstance(result, dict):
            results.append((name, result))
        return result
    finally:
        record_tool_call(role=_actor_role.get(), operator_id=_operator.get(),
                         tool_name=name, arguments=arguments, succeeded=succeeded,
                         elapsed_ms=(time.monotonic()-start)*1000)


async def _tracked_get_today_sales() -> dict:
    """查询今天的真实营业额、订单、客单价和新增用户等经营数据。"""
    return await _run_tool('get_today_sales', get_today_sales)


async def _turnover(begin: str, end: str) -> dict:
    """查询每日营业额趋势，begin、end 均为 YYYY-MM-DD。"""
    return await _run_tool('get_turnover_statistics', get_turnover_statistics, begin=begin, end=end)


async def _orders(begin: str, end: str) -> dict:
    """查询日期范围内订单数量和完成率，日期格式 YYYY-MM-DD。"""
    return await _run_tool('get_order_statistics', get_order_statistics, begin=begin, end=end)


async def _top_dishes(begin: str, end: str, limit: int = 10) -> dict:
    """查询指定日期范围销量排名，最多前10名。"""
    return await _run_tool('get_top_dishes', get_top_dishes, begin=begin, end=end, limit=limit)


async def _order_status_overview() -> dict:
    """查询当前待接单、已接单待派送、派送中的订单数量。"""
    return await _run_tool('get_order_status_overview', get_order_status_overview)


async def _order_list(status: int | None = None, begin: str | None = None,
                      end: str | None = None, number: str | None = None,
                      phone: str | None = None, limit: int = 10) -> dict:
    """按状态、下单日期、订单号或手机号查询订单列表；status 取1到6，begin/end 为 YYYY-MM-DD 且需同时提供。"""
    return await _run_tool('get_order_list', get_order_list,
                           status=status, begin=begin, end=end,
                           number=number, phone=phone, limit=limit)


async def _order_detail(order_id: int | None = None, order_number: str | None = None) -> dict:
    """查询单个订单的详情，包含取消原因和商品明细。

    用户说「订单号」时传 order_number（如 1785838137567）；只有知道数据库主键时才传 order_id。
    """
    return await _run_tool('get_order_detail', get_order_detail,
                           order_id=order_id, order_number=order_number)


async def _dish_catalog(status: int | None = None, limit: int = 30) -> dict:
    """查询菜品目录与上下架状态；status 为1在售、0停售、不传为全部。"""
    return await _run_tool('get_dish_catalog', get_dish_catalog, status=status, limit=limit)


async def _user_statistics(begin: str, end: str) -> dict:
    """查询日期范围内的累计用户数与新增用户数，日期格式 YYYY-MM-DD。"""
    return await _run_tool('get_user_statistics', get_user_statistics, begin=begin, end=end)


async def _period_comparison(begin: str, end: str, top_limit: int = 10) -> dict:
    """并发拉取本期与等长上一周期的营业额、订单完成率和热销榜，并计算精确环比变化；begin/end 为 YYYY-MM-DD。"""
    return await _run_tool(
        'get_period_comparison_report',
        get_period_comparison_report,
        begin=begin,
        end=end,
        top_limit=top_limit,
    )


async def _propose_dish_status_change(
    target_status: int,
    dish_id: int | None = None,
    dish_name: str | None = None,
    reason: str | None = None,
) -> dict:
    if _actor_role.get() != 'merchant':
        raise RuntimeError('仅商家管理员可以发起菜品状态变更审批')
    operator_id = _operator.get()
    session_id = _session_id.get()
    manager = _approval_manager.get()
    if type(operator_id) is not int or not session_id or manager is None:
        raise RuntimeError('缺少审批上下文，无法生成审批单')
    if type(target_status) is not int or target_status not in DISH_STATUS_NAMES:
        raise ValueError('target_status 只能是 0（停售）或 1（起售）')

    dish = await lookup_dish_for_status_change(dish_id=dish_id, dish_name=dish_name)
    if dish['status'] == target_status:
        return {
            'approval_required': False,
            'already_in_target_status': True,
            'dish': dish,
            'message': f"菜品「{dish['name']}」当前已经是「{dish['status_name']}」状态，无需重复修改。",
        }

    ticket = await manager.acreate_dish_status_approval(
        operator_id=operator_id,
        session_id=session_id,
        dish_id=dish['id'],
        dish_name=dish['name'],
        category=dish.get('category'),
        price=dish.get('price'),
        before_status=dish['status'],
        before_status_name=dish['status_name'],
        after_status=target_status,
        after_status_name=DISH_STATUS_NAMES[target_status],
        reason=reason,
    )
    pending = _pending_approvals.get()
    if pending is not None:
        pending.append(ticket)
    return {
        'approval_required': True,
        'approval_id': ticket.approval_id,
        'status': ticket.status,
        'summary': ticket.summary,
        'dish': dish,
        'target_status': target_status,
        'target_status_name': DISH_STATUS_NAMES[target_status],
        'expires_at': ticket.expires_at_iso,
        'note': '当前仅生成待审批单，尚未执行数据库修改；必须等待管理员点击确认执行后才会生效。',
    }


async def _request_dish_status_change(
    target_status: int,
    dish_id: int | None = None,
    dish_name: str | None = None,
    reason: str | None = None,
) -> dict:
    """发起菜品起售或停售的人工审批单（不会直接修改数据库）；target_status 为 1 起售、0 停售，dish_id 与 dish_name 至少提供一个。"""
    return await _run_tool(
        'request_dish_status_change',
        _propose_dish_status_change,
        target_status=target_status,
        dish_id=dish_id,
        dish_name=dish_name,
        reason=reason,
    )


class MerchantAgent:
    """基于 AgentScope 的商家经营 Agent 入口。"""

    actor_role = 'merchant'

    def __init__(
        self,
        *,
        review_manager: ReportReviewManager | None = None,
        approval_manager: ApprovalManager | None = None,
        state_store: RedisStateStore | None = None,
        token_recorder=None,
    ) -> None:
        self.settings = get_settings()
        self._token_recorder = token_recorder
        if state_store is not None:
            self.state_store: RedisStateStore | None = state_store
        elif getattr(self.settings, 'state_store_mode', 'memory') == 'redis':
            self.state_store = RedisStateStore(
                redis_url=getattr(self.settings, 'redis_url', 'redis://localhost:6379/1'),
                key_prefix=getattr(self.settings, 'redis_key_prefix', 'sky_agent'),
                socket_timeout=getattr(self.settings, 'redis_socket_timeout_seconds', 1.5),
            )
        else:
            self.state_store = None

        self.sessions = SessionStore(
            self.settings.agent_session_max_entries,
            self.settings.agent_session_idle_seconds,
            disposer=self._dispose_agent,
            state_store=self.state_store,
        )
        if self.actor_role == 'merchant':
            if review_manager is not None:
                self.review_manager: ReportReviewManager | None = review_manager
            else:
                mode = getattr(self.settings, 'report_review_mode', 'off')
                generator = None
                closer = None
                if mode == 'mock':
                    generator = build_mock_review_generator()
                elif mode == 'llm':
                    review_model = getattr(self.settings, 'report_review_model', '') or self.settings.agent_model
                    generator, closer = build_llm_review_generator(
                        api_key=self.settings.dashscope_api_key,
                        base_url=self.settings.dashscope_base_url,
                        model=review_model,
                        max_tokens=getattr(self.settings, 'report_review_max_tokens', 1536),
                        http_timeout=getattr(self.settings, 'report_review_http_timeout_seconds', 12.5),
                    )
                self.review_manager = ReportReviewManager(
                    generate=generator,
                    closer=closer,
                    state_store=self.state_store,
                    max_entries=getattr(self.settings, 'report_review_max_entries', 200),
                    max_concurrency=getattr(self.settings, 'report_review_max_concurrency', 4),
                    max_pending=getattr(self.settings, 'report_review_max_pending', 16),
                    ttl_seconds=getattr(self.settings, 'report_review_ttl_seconds', 600.0),
                    timeout=getattr(self.settings, 'report_review_timeout_seconds', 15.0),
                    poll_after_ms=getattr(self.settings, 'report_review_poll_after_ms', 1000),
                )
            self.approval_manager: ApprovalManager | None = (
                approval_manager
                if approval_manager is not None
                else ApprovalManager(
                    max_entries=getattr(self.settings, 'approval_max_entries', 200),
                    ttl_seconds=getattr(self.settings, 'approval_ttl_seconds', 300.0),
                    state_store=self.state_store,
                )
            )
        else:
            self.review_manager = None
            self.approval_manager = None

    def _create_agent(self) -> Agent:
        return self._build_agent(self._create_toolkit(), MERCHANT_SYSTEM_PROMPT, 'merchant_assistant')

    @staticmethod
    def _create_toolkit() -> Toolkit:
        return Toolkit(
            tools=[
                FunctionTool(_tracked_get_today_sales, name="get_today_sales", is_read_only=True),
                FunctionTool(_turnover, name='get_turnover_statistics', is_read_only=True),
                FunctionTool(_orders, name='get_order_statistics', is_read_only=True),
                FunctionTool(_top_dishes, name='get_top_dishes', is_read_only=True),
                FunctionTool(_order_status_overview, name='get_order_status_overview', is_read_only=True),
                FunctionTool(_order_list, name='get_order_list', is_read_only=True),
                FunctionTool(_order_detail, name='get_order_detail', is_read_only=True),
                FunctionTool(_dish_catalog, name='get_dish_catalog', is_read_only=True),
                FunctionTool(_user_statistics, name='get_user_statistics', is_read_only=True),
                FunctionTool(_period_comparison, name='get_period_comparison_report', is_read_only=True),
                FunctionTool(_request_dish_status_change, name='request_dish_status_change', is_read_only=True),
            ],
        )

    def _build_agent(self, toolkit: Toolkit, system_prompt: str, name: str) -> Agent:
        if not self.settings.dashscope_api_key:
            raise RuntimeError("未读取到 DASHSCOPE_API_KEY，请先配置环境变量")

        credential = OpenAICredential(
            api_key=self.settings.dashscope_api_key,
            base_url=self.settings.dashscope_base_url,
        )
        model = OpenAIChatModel(
            credential=credential,
            model=self.settings.agent_model,
            stream=False,
        )
        return Agent(
            name=name,
            system_prompt=system_prompt,
            model=model,
            toolkit=toolkit,
            react_config=ReActConfig(max_iters=6),
        )

    @staticmethod
    async def _dispose_agent(agent):
        await agent.model.client.close()

    async def shutdown(self) -> None:
        if self.review_manager is not None:
            await self.review_manager.shutdown()
        if self.state_store is not None:
            await self.state_store.close()

    async def clear_session(self, session_id: str, operator_id: int) -> bool:
        cleared = await self.sessions.clear((self.actor_role, operator_id, session_id))
        if self.actor_role == 'merchant':
            if self.review_manager is not None:
                await self.review_manager.acancel_session(operator_id=operator_id, session_id=session_id)
            if self.approval_manager is not None:
                await self.approval_manager.acancel_session(operator_id=operator_id, session_id=session_id)
        return cleared

    def get_report_review(
        self,
        *,
        review_id: str,
        operator_id: int,
        session_id: str,
    ) -> ReportReviewStatusResponse | None:
        if self.actor_role != 'merchant' or self.review_manager is None:
            return None
        return self.review_manager.get(
            review_id=review_id,
            operator_id=operator_id,
            session_id=session_id,
        )

    async def aget_report_review(
        self,
        *,
        review_id: str,
        operator_id: int,
        session_id: str,
    ) -> ReportReviewStatusResponse | None:
        if self.actor_role != 'merchant' or self.review_manager is None:
            return None
        return await self.review_manager.aget(
            review_id=review_id,
            operator_id=operator_id,
            session_id=session_id,
        )

    def get_approval(self, *, approval_id: str, operator_id: int) -> ApprovalTicketResponse | None:
        if self.actor_role != 'merchant' or self.approval_manager is None:
            return None
        return self.approval_manager.get(approval_id=approval_id, operator_id=operator_id)

    async def aget_approval(self, *, approval_id: str, operator_id: int) -> ApprovalTicketResponse | None:
        if self.actor_role != 'merchant' or self.approval_manager is None:
            return None
        return await self.approval_manager.aget(approval_id=approval_id, operator_id=operator_id)

    async def confirm_approval(
        self,
        *,
        approval_id: str,
        operator_id: int,
        reason: str | None = None,
    ) -> ApprovalTicketResponse:
        if self.actor_role != 'merchant' or self.approval_manager is None:
            raise RuntimeError('当前角色不支持审批执行')

        async def _execute(ticket: ApprovalTicket) -> str:
            result = await execute_dish_status_change(
                dish_id=ticket.dish_id,
                target_status=ticket.after_status,
                expected_current_status=ticket.before_status,
            )
            return f"已确认执行：菜品「{result['dish_name']}」状态已更新为「{result['after_status_name']}」。"

        return await self.approval_manager.confirm(
            approval_id=approval_id,
            operator_id=operator_id,
            executor=_execute,
            decision_reason=reason,
        )

    async def reject_approval(
        self,
        *,
        approval_id: str,
        operator_id: int,
        reason: str | None = None,
    ) -> ApprovalTicketResponse:
        if self.actor_role != 'merchant' or self.approval_manager is None:
            raise RuntimeError('当前角色不支持审批操作')
        return await self.approval_manager.reject(
            approval_id=approval_id,
            operator_id=operator_id,
            decision_reason=reason,
        )

    @staticmethod
    def _extract_text(message: Msg) -> str:
        texts = [block.text for block in message.content if isinstance(block, TextBlock)]
        answer = "".join(texts).strip()
        return answer or "模型没有返回文本内容，请稍后重试。"

    async def _compact_if_needed(self, agent: object) -> bool:
        return await compact_agent_context_if_needed(
            agent,
            max_messages=getattr(self.settings, 'context_max_messages', 12),
            keep_recent_messages=getattr(self.settings, 'context_keep_recent_messages', 6),
            max_chars=getattr(self.settings, 'context_max_chars', 12000),
            summary_max_chars=getattr(self.settings, 'context_summary_max_chars', 1600),
        )

    async def chat(self, request: AgentChatRequest, operator_id: int) -> AgentChatResponse:
        started = time.monotonic()
        trace_id = get_current_trace_id()
        calls: list[str] = []
        results: list[tuple[str, dict]] = []
        approvals: list[ApprovalTicket] = []
        compacted = False
        reply: Msg | None = None
        token = _tool_calls.set(calls)
        results_token = _tool_results.set(results)
        operator_token = _operator.set(operator_id)
        role_token = _actor_role.set(self.actor_role)
        session_token = _session_id.set(request.session_id)
        approval_mgr_token = _approval_manager.set(self.approval_manager)
        approvals_token = _pending_approvals.set(approvals)
        try:
            key = (self.actor_role, operator_id, request.session_id)
            async with self.sessions.use(key, self._create_agent) as agent:
                compacted = await self._compact_if_needed(agent)
                current_date = today()
                date_context = (f'当前北京时间日期：{current_date}；最近7天（含今天）：'
                                f'{current_date-timedelta(days=6)} 至 {current_date}。\n')
                reply = await agent.reply(UserMsg(name=self.actor_role, content=date_context + request.message))
            answer = self._extract_text(reply)
        except Exception:
            record_chat_turn_telemetry(
                agent_role=self.actor_role,
                operator_id=operator_id,
                session_id=request.session_id,
                stream=False,
                succeeded=False,
                ttft_ms=None,
                total_ms=(time.monotonic() - started) * 1000,
                tool_calls=calls,
                compacted=compacted,
                trace_id=trace_id,
            )
            raise
        finally:
            _tool_calls.reset(token)
            _tool_results.reset(results_token)
            _operator.reset(operator_token)
            _actor_role.reset(role_token)
            _session_id.reset(session_token)
            _approval_manager.reset(approval_mgr_token)
            _pending_approvals.reset(approvals_token)

        report_review = None
        if self.actor_role == 'merchant' and self.review_manager is not None and self.review_manager.enabled:
            evidence, metrics = extract_merchant_report_bundle(results, attempted_tools=calls)
            if evidence:
                report_review = self.review_manager.submit(
                    operator_id=operator_id,
                    session_id=request.session_id,
                    draft=answer,
                    evidence=evidence,
                    metrics=metrics,
                    trace_id=trace_id,
                )

        latest_approval = approvals[-1] if approvals else None
        in_tok, out_tok, tot_tok = extract_reply_token_usage(reply)
        if self._token_recorder is not None and tot_tok:
            await self._token_recorder(role=self.actor_role, operator_id=operator_id, total_tokens=tot_tok)
        total_ms = (time.monotonic() - started) * 1000
        record_chat_turn_telemetry(
            agent_role=self.actor_role,
            operator_id=operator_id,
            session_id=request.session_id,
            stream=False,
            succeeded=True,
            ttft_ms=total_ms,
            total_ms=total_ms,
            tool_calls=calls,
            compacted=compacted,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            approval_created=latest_approval is not None,
            review_submitted=report_review is not None and report_review.review_id is not None,
            trace_id=trace_id,
        )
        return AgentChatResponse(
            session_id=request.session_id,
            answer=answer,
            tool_calls=calls,
            approval_required=latest_approval is not None,
            approval_id=latest_approval.approval_id if latest_approval is not None else None,
            approval=latest_approval.to_response() if latest_approval is not None else None,
            agent_role=self.actor_role,
            report_review=report_review,
        )

    async def chat_stream(
        self,
        request: AgentChatRequest,
        operator_id: int,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Yield incremental SSE (event_name, payload) tuples for ReAct tools and answer tokens."""
        started = time.monotonic()
        trace_id = get_current_trace_id()
        first_token_ms: float | None = None
        calls: list[str] = []
        results: list[tuple[str, dict]] = []
        approvals: list[ApprovalTicket] = []
        tool_started_at: dict[str, tuple[str, float]] = {}
        streamed_chunks: list[str] = []
        final_msg: Msg | None = None
        compacted = False

        yield ('meta', {
            'session_id': request.session_id,
            'agent_role': self.actor_role,
            'trace_id': trace_id,
        })

        token = _tool_calls.set(calls)
        results_token = _tool_results.set(results)
        operator_token = _operator.set(operator_id)
        role_token = _actor_role.set(self.actor_role)
        session_token = _session_id.set(request.session_id)
        approval_mgr_token = _approval_manager.set(self.approval_manager)
        approvals_token = _pending_approvals.set(approvals)
        try:
            key = (self.actor_role, operator_id, request.session_id)
            async with self.sessions.use(key, self._create_agent) as agent:
                compacted = await self._compact_if_needed(agent)
                current_date = today()
                date_context = (f'当前北京时间日期：{current_date}；最近7天（含今天）：'
                                f'{current_date-timedelta(days=6)} 至 {current_date}。\n')
                user_msg = UserMsg(name=self.actor_role, content=date_context + request.message)
                model = getattr(agent, 'model', None)
                orig_stream = getattr(model, 'stream', None)
                want_stream = getattr(self.settings, 'agent_stream_enabled', True)
                if model is not None and isinstance(orig_stream, bool):
                    model.stream = bool(want_stream)
                try:
                    if hasattr(agent, 'reply_stream'):
                        async for item in agent.reply_stream(user_msg, yield_final_msg=True):
                            if isinstance(item, Msg):
                                final_msg = item
                            elif isinstance(item, ModelCallStartEvent):
                                yield ('status', {'phase': 'thinking'})
                            elif isinstance(item, ToolResultStartEvent):
                                tool_started_at[item.tool_call_id] = (item.tool_call_name, time.monotonic())
                                yield ('tool_start', {
                                    'tool_call_id': item.tool_call_id,
                                    'tool_name': item.tool_call_name,
                                })
                            elif isinstance(item, ToolResultEndEvent):
                                name, t0 = tool_started_at.pop(item.tool_call_id, ('', time.monotonic()))
                                elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
                                state_text = str(getattr(item, 'state', '') or '').lower()
                                succeeded = 'error' not in state_text and 'fail' not in state_text
                                yield ('tool_end', {
                                    'tool_call_id': item.tool_call_id,
                                    'tool_name': name,
                                    'succeeded': succeeded,
                                    'elapsed_ms': elapsed_ms,
                                })
                            elif isinstance(item, TextBlockDeltaEvent) and item.delta:
                                if first_token_ms is None:
                                    first_token_ms = round((time.monotonic() - started) * 1000, 1)
                                    yield ('status', {'phase': 'answering'})
                                streamed_chunks.append(item.delta)
                                yield ('delta', {'text': item.delta})
                    else:
                        final_msg = await agent.reply(user_msg)
                        text = self._extract_text(final_msg)
                        if text:
                            first_token_ms = round((time.monotonic() - started) * 1000, 1)
                            streamed_chunks.append(text)
                            yield ('delta', {'text': text})
                finally:
                    if model is not None and isinstance(orig_stream, bool):
                        model.stream = orig_stream
            if final_msg is not None:
                answer = self._extract_text(final_msg)
            else:
                answer = ''.join(streamed_chunks).strip() or '模型没有返回文本内容，请稍后重试。'
        except Exception:
            record_chat_turn_telemetry(
                agent_role=self.actor_role,
                operator_id=operator_id,
                session_id=request.session_id,
                stream=True,
                succeeded=False,
                ttft_ms=first_token_ms,
                total_ms=(time.monotonic() - started) * 1000,
                tool_calls=calls,
                compacted=compacted,
                trace_id=trace_id,
            )
            raise
        finally:
            _tool_calls.reset(token)
            _tool_results.reset(results_token)
            _operator.reset(operator_token)
            _actor_role.reset(role_token)
            _session_id.reset(session_token)
            _approval_manager.reset(approval_mgr_token)
            _pending_approvals.reset(approvals_token)

        report_review = None
        if self.actor_role == 'merchant' and self.review_manager is not None and self.review_manager.enabled:
            evidence, metrics = extract_merchant_report_bundle(results, attempted_tools=calls)
            if evidence:
                report_review = self.review_manager.submit(
                    operator_id=operator_id,
                    session_id=request.session_id,
                    draft=answer,
                    evidence=evidence,
                    metrics=metrics,
                    trace_id=trace_id,
                )

        latest_approval = approvals[-1] if approvals else None
        response = AgentChatResponse(
            session_id=request.session_id,
            answer=answer,
            tool_calls=calls,
            approval_required=latest_approval is not None,
            approval_id=latest_approval.approval_id if latest_approval is not None else None,
            approval=latest_approval.to_response() if latest_approval is not None else None,
            agent_role=self.actor_role,
            report_review=report_review,
        )
        total_ms = round((time.monotonic() - started) * 1000, 1)
        resolved_ttft = first_token_ms if first_token_ms is not None else total_ms
        in_tok, out_tok, tot_tok = extract_reply_token_usage(final_msg)
        if self._token_recorder is not None and tot_tok:
            await self._token_recorder(role=self.actor_role, operator_id=operator_id, total_tokens=tot_tok)
        record_chat_turn_telemetry(
            agent_role=self.actor_role,
            operator_id=operator_id,
            session_id=request.session_id,
            stream=True,
            succeeded=True,
            ttft_ms=resolved_ttft,
            total_ms=total_ms,
            tool_calls=calls,
            compacted=compacted,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            approval_created=latest_approval is not None,
            review_submitted=report_review is not None and report_review.review_id is not None,
            trace_id=trace_id,
        )
        payload = response.model_dump()
        payload['trace_id'] = trace_id
        payload['metrics'] = {
            'ttft_ms': resolved_ttft,
            'total_ms': total_ms,
        }
        yield ('done', payload)
