"""Day 8-9 tests: shared HTTP pool, sliding-window context compaction, and Redis persistence."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UserMsg,
)
from agentscope.state import AgentState
from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest

from app.approval.manager import ApprovalConflictError, ApprovalManager
from app.clients import customer_backend_client, sky_backend_client
from app.clients.http_pool import (
    close_shared_backend_client,
    get_shared_backend_client,
    open_shared_backend_client,
)
from app.config import Settings
from app.main import app
from app.memory.context_compactor import (
    build_bounded_summary,
    compact_agent_context_if_needed,
    estimate_message_chars,
)
from app.memory.session_store import SessionStore
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.review.text_reviewer import Fact
from app.storage.redis_store import RedisStateStore


def run(coro):
    return asyncio.run(coro)


class FakeAsyncRedis:
    """Deterministic in-memory async Redis double supporting GET/SET(NX,EX,PX)/DEL/SADD/SMEMBERS/EVAL."""

    def __init__(self, *, fail_io: bool = False) -> None:
        self.fail_io = fail_io
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.sets: dict[str, set[str]] = {}
        self.eval_calls = 0

    def _check(self) -> None:
        if self.fail_io:
            raise OSError('simulated redis connection error secret-redis-pass')

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> str | None:
        self._check()
        return self.kv.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._check()
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        elif px is not None:
            self.ttls[key] = max(1, px // 1000)
        return True

    async def delete(self, *keys: str) -> int:
        self._check()
        removed = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                removed += 1
            if key in self.sets:
                del self.sets[key]
                removed += 1
            self.ttls.pop(key, None)
        return removed

    async def sadd(self, key: str, *members: str) -> int:
        self._check()
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        return len(bucket) - before

    async def smembers(self, key: str) -> set[str]:
        self._check()
        return set(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> bool:
        self._check()
        self.ttls[key] = seconds
        return True

    async def eval(self, _script: str, numkeys: int, *keys_and_args: str) -> int:
        self._check()
        self.eval_calls += 1
        assert numkeys == 1
        key, expected = keys_and_args[0], keys_and_args[1]
        if self.kv.get(key) == expected:
            del self.kv[key]
            return 1
        return 0


# ---------------- 1. Config & Shared HTTP Connection Pool ----------------

def test_day8_9_settings_validation():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backend_http_max_connections=10, backend_http_max_keepalive=20)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_max_messages=6, context_keep_recent_messages=6)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backend_http_max_connections=True)
    valid = Settings(_env_file=None, state_store_mode='redis', context_max_messages=14, context_keep_recent_messages=6)
    assert valid.state_store_mode == 'redis'


def test_shared_http_pool_reuses_client_and_isolates_admin_and_customer_headers(monkeypatch):
    seen_headers: list[tuple[str, dict[str, str]]] = []
    settings = SimpleNamespace(
        sky_backend_base_url='https://pooled.invalid',
        backend_http_timeout_seconds=8.0,
    )
    monkeypatch.setattr(sky_backend_client, 'get_settings', lambda: settings)
    monkeypatch.setattr(customer_backend_client, 'get_settings', lambda: settings)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append((
            request.url.path,
            {k.lower(): v for k, v in request.headers.items() if k.lower() in ('token', 'authentication')},
        ))
        return httpx.Response(200, json={'code': 1, 'data': {'status': 1}})

    async def scenario():
        pooled = open_shared_backend_client(
            base_url='https://pooled.invalid/',
            timeout_seconds=8.0,
            max_connections=20,
            max_keepalive_connections=10,
            transport=httpx.MockTransport(handler),
        )
        try:
            assert get_shared_backend_client('https://pooled.invalid') is pooled
            assert pooled.headers.get('token') is None
            assert pooled.headers.get('authentication') is None

            await asyncio.gather(
                sky_backend_client.SkyBackendClient(token='admin-secret-1').get('/admin/shop/status'),
                customer_backend_client.CustomerBackendClient(token='user-secret-2').get('/user/shop/status'),
            )
            # Pooled client stays open across requests!
            assert not pooled.is_closed
        finally:
            await close_shared_backend_client()
        assert pooled.is_closed
        assert get_shared_backend_client('https://pooled.invalid') is None

    run(scenario())
    by_path = dict(seen_headers)
    assert by_path['/admin/shop/status'] == {'token': 'admin-secret-1'}
    assert by_path['/user/shop/status'] == {'authentication': 'user-secret-2'}


def test_app_lifespan_opens_and_closes_shared_http_pool():
    with TestClient(app) as client:
        resp = client.get('/health')
        assert resp.status_code == 200
    assert get_shared_backend_client('http://localhost:8080') is None


# ---------------- 2. Multi-Turn Sliding Window + Summary Compaction ----------------

def test_context_compactor_preserves_turn_boundary_and_bounds_iterative_summaries():
    state = AgentState(session_id='s-compact')
    for idx in range(1, 6):
        state.context.append(UserMsg(
            name='merchant',
            content=f'当前北京时间日期：2026-08-14；最近7天（含今天）：2026-08-08 至 2026-08-14。\n第{idx}轮查询今日数据',
        ))
        state.context.append(AssistantMsg(
            name='merchant_assistant',
            content=[
                ToolCallBlock(id=f'tc_{idx}', name='get_today_sales', input='{}'),
                ToolResultBlock(
                    id=f'tc_{idx}',
                    name='get_today_sales',
                    output=[TextBlock(text=f'{{"turnover": {100 * idx}, "raw_blob": "{"x" * 500}"}}')],
                ),
                TextBlock(text=f'第{idx}轮结论：已完成营业额 {100 * idx} 元。'),
            ],
        ))

    agent = SimpleNamespace(state=state)
    assert sum(estimate_message_chars(m) for m in state.context) > 2500

    # Wave 1 compaction: 10 messages > max_messages=6, keep_recent_messages=4 (last 2 turns: #4, #5)
    compacted = run(compact_agent_context_if_needed(
        agent,
        max_messages=6,
        keep_recent_messages=4,
        max_chars=50000,
        summary_max_chars=900,
    ))
    assert compacted is True
    assert len(state.context) == 4
    assert state.context[0].role == 'user'
    assert '第4轮' in state.context[0].get_text_content()
    assert '<conversation-summary>' in state.summary
    assert '严禁直接把历史摘要中的旧数值当作当前事实' in state.summary
    assert '当前北京时间日期' not in state.summary
    assert '工具：get_today_sales' in state.summary
    assert '第1轮查询今日数据' in state.summary
    assert len(state.summary) <= 900

    # Wave 2 compaction: add 2 more turns and verify nested <conversation-summary> never occurs
    for idx in (6, 7):
        state.context.append(UserMsg(name='merchant', content=f'第{idx}轮继续追问热销菜'))
        state.context.append(AssistantMsg(
            name='merchant_assistant',
            content=[TextBlock(type='text', text=f'第{idx}轮回复内容。')],
        ))
    compacted_again = run(compact_agent_context_if_needed(
        agent,
        max_messages=6,
        keep_recent_messages=4,
        max_chars=50000,
        summary_max_chars=500,
    ))
    assert compacted_again is True
    assert state.summary.count('<conversation-summary>') == 1
    assert state.summary.count('</conversation-summary>') == 1
    assert len(state.summary) <= 500
    assert len(state.context) == 4
    assert state.context[0].role == 'user'


def test_context_compactor_noop_when_below_thresholds():
    state = AgentState(session_id='s-small', context=[
        UserMsg(name='merchant', content='你好'),
        AssistantMsg(name='merchant_assistant', content=[TextBlock(type='text', text='您好')]),
    ])
    agent = SimpleNamespace(state=state)
    assert run(compact_agent_context_if_needed(agent, max_messages=12, keep_recent_messages=6)) is False
    assert state.summary == ''
    assert len(state.context) == 2

    # Ensure build_bounded_summary handles empty inputs safely
    assert build_bounded_summary('', [], max_chars=400) == ''


# ---------------- 3. Redis Persistence Across Workers & Distributed Lock ----------------

def test_redis_session_persistence_across_two_workers_and_operator_isolation():
    fake_redis = FakeAsyncRedis()
    store = RedisStateStore(key_prefix='test_sky', client=fake_redis)

    def make_dummy_agent():
        return SimpleNamespace(state=AgentState(session_id='init'))

    async def scenario():
        worker_a = SessionStore(10, 1800, state_store=store)
        worker_b = SessionStore(10, 1800, state_store=store)

        # Worker A writes conversation turn
        async with worker_a.use(('merchant', 7, 'sess-1'), make_dummy_agent) as agent_a:
            agent_a.state.summary = '早期摘要'
            agent_a.state.context.append(UserMsg(name='merchant', content='今天卖了多少'))
            agent_a.state.context.append(AssistantMsg(
                name='merchant_assistant',
                content=[TextBlock(type='text', text='今日已完成订单营业额 268 元。')],
            ))

        # Worker B (cold memory) restores the exact AgentState!
        async with worker_b.use(('merchant', 7, 'sess-1'), make_dummy_agent) as agent_b:
            assert agent_b.state.summary == '早期摘要'
            assert len(agent_b.state.context) == 2
            assert '268 元' in agent_b.state.context[1].content[0].text

        # Different operator_id with same session_id is strictly isolated!
        async with worker_b.use(('merchant', 8, 'sess-1'), make_dummy_agent) as agent_other:
            assert agent_other.state.summary == ''
            assert agent_other.state.context == []

        # Fresh Worker C clears session -> deletes from Redis and returns True even with empty memory!
        worker_c = SessionStore(10, 1800, state_store=store)
        assert await worker_c.clear(('merchant', 7, 'sess-1')) is True
        assert await worker_c.clear(('merchant', 7, 'sess-1')) is False

    run(scenario())


def test_redis_report_review_persists_across_workers_and_clears():
    fake_redis = FakeAsyncRedis()
    store = RedisStateStore(key_prefix='test_sky', client=fake_redis)

    async def scenario():
        mgr_a = ReportReviewManager(generate=build_mock_review_generator(), state_store=store)
        mgr_b = ReportReviewManager(generate=build_mock_review_generator(), state_store=store)
        ref = mgr_a.submit(
            operator_id=7,
            session_id='s-rev',
            draft='已完成订单营业额为100元，无异常。',
            evidence=(Fact(id='turnover', text='营业额100元'),),
        )
        assert ref is not None and ref.review_id is not None

        for _ in range(30):
            local = mgr_a.get(review_id=ref.review_id, operator_id=7, session_id='s-rev')
            if local and local.status not in ('pending', 'running'):
                break
            await asyncio.sleep(0.02)

        # Worker B (which never ran this review in memory) reads terminal result from Redis!
        remote = await mgr_b.aget(review_id=ref.review_id, operator_id=7, session_id='s-rev')
        assert remote is not None
        assert remote.status == 'pass'
        # Wrong operator cannot read it
        assert await mgr_b.aget(review_id=ref.review_id, operator_id=8, session_id='s-rev') is None

        # Session clear on Worker B removes Redis review keys
        removed = await mgr_b.acancel_session(operator_id=7, session_id='s-rev')
        assert removed >= 1
        assert await mgr_b.aget(review_id=ref.review_id, operator_id=7, session_id='s-rev') is None
        await mgr_a.shutdown()
        await mgr_b.shutdown()

    run(scenario())


def test_redis_approval_persists_across_workers_and_enforces_distributed_idempotency(tmp_path):
    fake_redis = FakeAsyncRedis()
    store = RedisStateStore(key_prefix='test_sky', client=fake_redis)
    audit_log = tmp_path / 'approval.jsonl'
    executions = []

    async def slow_executor(ticket) -> str:
        executions.append(ticket.approval_id)
        await asyncio.sleep(0.06)
        return '已执行停售'

    async def scenario():
        worker_a = ApprovalManager(audit_log_path=audit_log, state_store=store)
        worker_b = ApprovalManager(audit_log_path=audit_log, state_store=store)

        ticket = await worker_a.acreate_dish_status_approval(
            operator_id=7,
            session_id='s-apr',
            dish_id=12,
            dish_name='酸菜鱼',
            category='招牌菜',
            price=58.0,
            before_status=1,
            before_status_name='起售',
            after_status=0,
            after_status_name='停售',
            reason='售罄',
        )

        # Worker B can query the pending ticket created on Worker A
        loaded_b = await worker_b.aget(approval_id=ticket.approval_id, operator_id=7)
        assert loaded_b is not None and loaded_b.status == 'pending'
        assert await worker_b.aget(approval_id=ticket.approval_id, operator_id=99) is None

        # Concurrent confirm on Worker A and Worker B simultaneously:
        # Distributed SET NX PX lock + state reload ensures executor runs EXACTLY ONCE!
        outcomes = await asyncio.gather(
            worker_a.confirm(approval_id=ticket.approval_id, operator_id=7, executor=slow_executor),
            worker_b.confirm(approval_id=ticket.approval_id, operator_id=7, executor=slow_executor),
            return_exceptions=True,
        )
        successes = [o for o in outcomes if not isinstance(o, BaseException)]
        conflicts = [o for o in outcomes if isinstance(o, ApprovalConflictError)]
        assert len(successes) == 1 and successes[0].status == 'executed'
        assert len(conflicts) == 1
        assert len(executions) == 1
        assert fake_redis.eval_calls >= 1

    run(scenario())


def test_redis_outage_degrades_gracefully_without_leaking_credentials(caplog):
    broken_redis = FakeAsyncRedis(fail_io=True)
    store = RedisStateStore(
        redis_url='redis://:super-secret-pwd@127.0.0.1:6379/1',
        key_prefix='test_sky',
        client=broken_redis,
    )

    async def scenario():
        assert await store.ping() is False
        sess = SessionStore(10, 1800, state_store=store)
        async with sess.use(('merchant', 1, 's-fallback'), lambda: SimpleNamespace(state=AgentState())) as ag:
            ag.state.summary = 'ok'
        assert await sess.clear(('merchant', 1, 's-fallback')) is True

    run(scenario())
    logged_text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'super-secret-pwd' not in logged_text
    assert 'secret-redis-pass' not in logged_text
