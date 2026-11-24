"""Offline tests for merchant async report review, ownership boundaries, and hardening."""
import asyncio
from datetime import date
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agent import merchant_agent as runtime
from app.agent.customer_agent import CustomerAgent
from app.api import routes
from app.clients import customer_backend_client, sky_backend_client
from app.clients.customer_backend_client import current_customer_id, current_customer_token
from app.clients.sky_backend_client import current_backend_token
from app.config import Settings
from app.main import app
from app.review.evidence import extract_merchant_report_evidence
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.review.text_reviewer import Fact
from app.tools import report_utils


def _order_stats_payload(*, total=4, valid=2, rate=0.5):
    return {
        'begin': '2026-08-01',
        'end': '2026-08-07',
        'daily': [{'date': '2026-08-01', 'orders': total, 'valid_orders': valid}],
        'total_orders': total,
        'valid_orders': valid,
        'completion_rate': rate,
    }


def _top_dishes_payload():
    return {
        'begin': '2026-08-01',
        'end': '2026-08-07',
        'ranking': [{'name': '酸菜鱼', 'sales': 10}, {'name': '米饭', 'sales': 8}],
    }


def test_evidence_extraction_scopes_aggregate_tools_and_excludes_raw_orders():
    assert extract_merchant_report_evidence([]) == ()
    assert extract_merchant_report_evidence([('get_order_status_overview', {
        'to_be_confirmed': 1, 'confirmed': 2, 'delivery_in_progress': 0,
    })]) == ()
    assert extract_merchant_report_evidence([('get_order_detail', {
        'id': 1, 'phone': '13800000000', 'address': 'private-addr',
    })]) == ()

    # Mixed turn with raw order tool must not auto-trigger review.
    assert extract_merchant_report_evidence(
        [('get_order_statistics', _order_stats_payload())],
        attempted_tools=['get_order_statistics', 'get_order_detail'],
    ) == ()
    assert extract_merchant_report_evidence(
        [('get_order_statistics', _order_stats_payload()),
         ('get_order_list', {'total': 1, 'returned': 1, 'orders': [{'phone': '13800000000'}]})],
    ) == ()

    facts = extract_merchant_report_evidence([
        ('get_order_statistics', _order_stats_payload(total=0, valid=0, rate=0)),
        ('get_top_dishes', _top_dishes_payload()),
        ('get_dish_catalog', {'total': 5, 'returned': 5, 'dishes': []}),
    ])
    assert [fact.id for fact in facts] == ['orders', 'ranking']
    assert '完成率不适用' in facts[0].text
    assert '不是完整商品目录' in facts[1].text


def test_manager_bounds_and_settings_validation():
    with pytest.raises(ValueError):
        ReportReviewManager(max_concurrency=5, max_pending=4)
    with pytest.raises(ValueError):
        ReportReviewManager(max_pending=10, max_entries=5)
    with pytest.raises(ValueError):
        ReportReviewManager(max_entries=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_max_concurrency=10, report_review_max_pending=5)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_timeout_seconds=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_ttl_seconds=float('inf'))


class _ScriptedAppEnv:
    def __init__(self, monkeypatch, *, review_manager: ReportReviewManager):
        self.turns: list = []
        self.backend_routes: dict[tuple[str, str], tuple[int, object]] = {
            ('GET', '/admin/agent/identity'): (200, {'code': 1, 'data': {'operator_id': 7}}),
            ('GET', '/user/agent/identity'): (200, {'code': 1, 'data': {'user_id': 12}}),
        }
        self.models: list = []
        self.review_manager = review_manager

        settings = SimpleNamespace(
            dashscope_api_key='synthetic-model-key',
            dashscope_base_url='https://model.invalid/v1',
            agent_model='offline-script',
            agent_session_idle_seconds=1800,
            agent_session_max_entries=500,
            sky_backend_base_url='https://business.invalid',
        )
        for module in (runtime, sky_backend_client, customer_backend_client):
            monkeypatch.setattr(module, 'get_settings', lambda: settings)

        def backend_handler(request: httpx.Request):
            if request.url.path == '/admin/agent/identity':
                token = request.headers.get('token')
                if token == 'admin-other':
                    return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 8}})
                if token == 'admin-main':
                    return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 7}})
                return httpx.Response(401, json={'code': 0, 'msg': 'unauthorized'})
            key = (request.method, request.url.path)
            if key not in self.backend_routes:
                return httpx.Response(500, json={'code': 0, 'msg': 'unconfigured'})
            status, payload = self.backend_routes[key]
            if isinstance(payload, str):
                return httpx.Response(status, text=payload)
            return httpx.Response(status, json=payload)

        def model_handler(request: httpx.Request):
            assert self.turns, 'model script exhausted'
            turn = self.turns.pop(0)
            message = {'role': 'assistant', 'content': turn if isinstance(turn, str) else None}
            if not isinstance(turn, str):
                message['tool_calls'] = [
                    {
                        'id': f'call_{i}',
                        'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(arguments)},
                    }
                    for i, (name, arguments) in enumerate(turn)
                ]
            return httpx.Response(200, json={
                'id': 'completion_async_test',
                'object': 'chat.completion',
                'created': 0,
                'model': 'offline-script',
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop' if isinstance(turn, str) else 'tool_calls'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            })

        backend_http = SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(
            **kwargs, transport=httpx.MockTransport(backend_handler)))
        monkeypatch.setattr(sky_backend_client, 'httpx', backend_http)
        monkeypatch.setattr(customer_backend_client, 'httpx', backend_http)

        actual_model = runtime.OpenAIChatModel

        def make_model(**kwargs):
            instance = actual_model(
                **kwargs,
                max_retries=0,
                client_kwargs={
                    'max_retries': 0,
                    'http_client': httpx.AsyncClient(transport=httpx.MockTransport(model_handler), trust_env=False),
                },
            )
            self.models.append(instance)
            return instance

        monkeypatch.setattr(runtime, 'OpenAIChatModel', make_model)
        monkeypatch.setattr(runtime, 'record_tool_call', lambda **kwargs: None)
        monkeypatch.setattr(runtime, 'today', lambda: date(2026, 11, 1))
        monkeypatch.setattr(report_utils, 'today', lambda: date(2026, 11, 1))
        monkeypatch.setattr(routes, 'merchant_agent', runtime.MerchantAgent(review_manager=review_manager))
        monkeypatch.setattr(routes, 'customer_agent', CustomerAgent())

    def add_backend(self, path: str, data):
        self.backend_routes[('GET', path)] = (200, {'code': 1, 'data': data})

    async def close(self):
        await self.review_manager.shutdown()
        for instance in self.models:
            await instance.client.close()


async def _wait_terminal(client: httpx.AsyncClient, session_id: str, review_id: str, *, token='admin-main'):
    for _ in range(80):
        resp = await client.get(
            f'/api/agent/merchant/sessions/{session_id}/report-reviews/{review_id}',
            headers={'token': token},
        )
        assert resp.status_code == 200
        body = resp.json()
        if body['status'] in ('pass', 'revise', 'unavailable'):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError('review did not reach terminal state')


def test_async_report_returns_immediately_without_holding_session_lock_or_leaking_token(monkeypatch):
    gate = asyncio.Event()
    observed_contexts: list[dict] = []

    async def gated_reviewer(messages):
        observed_contexts.append({
            'backend_token': current_backend_token.get(),
            'customer_token': current_customer_token.get(),
            'customer_id': current_customer_id.get(),
            'operator': runtime._operator.get(),
            'tool_calls': runtime._tool_calls.get(),
            'tool_results': runtime._tool_results.get(),
            'raw_messages': json.dumps(messages, ensure_ascii=False),
        })
        await gate.wait()
        return await build_mock_review_generator()(messages)

    manager = ReportReviewManager(generate=gated_reviewer, poll_after_ms=50, timeout=2.0)
    env = _ScriptedAppEnv(monkeypatch, review_manager=manager)
    env.add_backend('/admin/report/ordersStatistics', {
        'dateList': '2026-08-01',
        'orderCountList': '4',
        'validOrderCountList': '2',
        'totalOrderCount': 4,
        'validOrderCount': 2,
        'orderCompletionRate': 0.5,
    })
    env.turns = [
        [('get_order_statistics', {'begin': '2026-08-01', 'end': '2026-08-01'})],
        '本期有2笔未完成订单，两笔都已取消。',
        '普通闲聊：你好，请问想查哪天的数据？',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                first = await client.post(
                    '/api/agent/merchant/chat',
                    headers={'token': 'admin-main'},
                    json={'session_id': 'sess-1', 'message': '查订单统计'},
                )
                assert first.status_code == 200
                data = first.json()
                assert data['answer'] == '本期有2笔未完成订单，两笔都已取消。'
                assert data['tool_calls'] == ['get_order_statistics']
                assert data['agent_role'] == 'merchant'
                assert data['report_review']['status'] == 'pending'
                review_id = data['report_review']['review_id']
                assert review_id

                # While review is still blocked on gate, same session can accept another chat turn!
                second = await client.post(
                    '/api/agent/chat',
                    headers={'token': 'admin-main'},
                    json={'session_id': 'sess-1', 'message': '谢谢'},
                )
                assert second.status_code == 200
                assert second.json()['report_review'] is None

                # Status is pending/running before gate opens.
                mid = await client.get(
                    f'/api/agent/merchant/sessions/sess-1/report-reviews/{review_id}',
                    headers={'token': 'admin-main'},
                )
                assert mid.status_code == 200
                assert mid.json()['status'] in ('pending', 'running')

                # Auth & ownership boundaries while running.
                assert (await client.get(f'/api/agent/merchant/sessions/sess-1/report-reviews/{review_id}')).status_code == 401
                assert (await client.get(
                    f'/api/agent/merchant/sessions/sess-1/report-reviews/{review_id}',
                    headers={'authentication': 'user-token'},
                )).status_code == 401
                assert (await client.get(
                    f'/api/agent/merchant/sessions/sess-1/report-reviews/{review_id}',
                    headers={'token': 'admin-other'},
                )).status_code == 404
                assert (await client.get(
                    f'/api/agent/merchant/sessions/sess-other/report-reviews/{review_id}',
                    headers={'token': 'admin-main'},
                )).status_code == 404

                gate.set()
                final = await _wait_terminal(client, 'sess-1', review_id)
                assert final['status'] == 'revise'
                assert final['issues'][0]['code'] == 'status_inference'
                assert final['issues'][0]['quote'] == '都已取消'
                assert final['evidence_ids'] == ['orders']

                assert len(observed_contexts) == 1
                ctx = observed_contexts[0]
                assert ctx['backend_token'] is None
                assert ctx['customer_token'] is None
                assert ctx['customer_id'] is None
                assert ctx['operator'] is None
                assert ctx['tool_calls'] is None
                assert ctx['tool_results'] is None
                assert 'admin-main' not in ctx['raw_messages']
        finally:
            await env.close()

    asyncio.run(run())


def test_timeout_bad_json_exception_and_queue_full_never_report_pass(monkeypatch):
    mode = {'kind': 'timeout'}
    blocker = asyncio.Event()

    async def controlled_reviewer(_messages):
        if mode['kind'] == 'timeout':
            await asyncio.Event().wait()
        if mode['kind'] == 'bad_json':
            return '{not valid json'
        if mode['kind'] == 'error':
            raise RuntimeError('synthetic-secret-provider-stack')
        if mode['kind'] == 'block':
            await blocker.wait()
            return json.dumps({'verdict': 'pass', 'issues': []})
        return json.dumps({'verdict': 'pass', 'issues': []})

    manager = ReportReviewManager(
        generate=controlled_reviewer,
        max_entries=10,
        max_concurrency=1,
        max_pending=1,
        timeout=0.04,
        queue_timeout=0.04,
    )
    env = _ScriptedAppEnv(monkeypatch, review_manager=manager)
    env.add_backend('/admin/workspace/businessData', {
        'turnover': 158, 'validOrderCount': 2, 'orderCompletionRate': 0.5, 'unitPrice': 79, 'newUsers': 1,
    })
    env.turns = [
        [('get_today_sales', {})], '报告1：已完成营业额158元。',
        [('get_today_sales', {})], '报告2：已完成营业额158元。',
        [('get_today_sales', {})], '报告3：已完成营业额158元。',
        [('get_today_sales', {})], '报告4：占位任务。',
        [('get_today_sales', {})], '报告5：队列已满仍应返回报告。',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                for kind, expected_failure in [('timeout', 'timeout'), ('bad_json', 'bad_output'), ('error', 'model_error')]:
                    mode['kind'] = kind
                    res = await client.post(
                        '/api/agent/merchant/chat',
                        headers={'token': 'admin-main'},
                        json={'session_id': f's-{kind}', 'message': '今天数据'},
                    )
                    assert res.status_code == 200
                    rid = res.json()['report_review']['review_id']
                    final = await _wait_terminal(client, f's-{kind}', rid)
                    assert final['status'] == 'unavailable'
                    assert final['failure'] == expected_failure
                    assert 'synthetic-secret-provider-stack' not in json.dumps(final)

                # Fill single pending slot, then verify queue_full still returns report cleanly.
                mode['kind'] = 'block'
                first = await client.post(
                    '/api/agent/merchant/chat',
                    headers={'token': 'admin-main'},
                    json={'session_id': 's-queue', 'message': '今天数据'},
                )
                assert first.status_code == 200
                first_rid = first.json()['report_review']['review_id']
                assert first_rid

                second = await client.post(
                    '/api/agent/merchant/chat',
                    headers={'token': 'admin-main'},
                    json={'session_id': 's-queue', 'message': '今天数据'},
                )
                assert second.status_code == 200
                assert second.json()['answer'] == '报告5：队列已满仍应返回报告。'
                assert second.json()['report_review'] == {
                    'review_id': None,
                    'status': 'unavailable',
                    'failure': 'queue_full',
                    'poll_after_ms': None,
                }
                blocker.set()
                await _wait_terminal(client, 's-queue', first_rid)
        finally:
            blocker.set()
            await env.close()

    asyncio.run(run())


def test_multiple_reports_in_same_session_do_not_mix_and_clear_cancels_reviews(monkeypatch):
    manager = ReportReviewManager(generate=build_mock_review_generator(delay_seconds=0.01), timeout=2.0)
    env = _ScriptedAppEnv(monkeypatch, review_manager=manager)
    env.add_backend('/admin/report/top10', {'nameList': '酸菜鱼,米饭', 'numberList': '10,8'})
    env.turns = [
        [('get_top_dishes', {'begin': '2026-08-01', 'end': '2026-08-07'})],
        '销量榜返回2款商品，无法据此确认全店在售品类。',
        [('get_top_dishes', {'begin': '2026-08-01', 'end': '2026-08-07'})],
        '销量榜上有2款，所以全店只有2款在售。',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                r1 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 'multi', 'message': '看销量1'})
                r2 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 'multi', 'message': '看销量2'})
                id1 = r1.json()['report_review']['review_id']
                id2 = r2.json()['report_review']['review_id']
                assert id1 != id2
                res1 = await _wait_terminal(client, 'multi', id1)
                res2 = await _wait_terminal(client, 'multi', id2)
                assert res1['status'] == 'pass' and res1['issues'] == []
                assert res2['status'] == 'revise' and res2['issues'][0]['code'] == 'ranking_scope'

                cleared = await client.delete('/api/agent/merchant/sessions/multi', headers={'token': 'admin-main'})
                assert cleared.status_code == 200 and cleared.json()['cleared'] is True
                assert (await client.get(f'/api/agent/merchant/sessions/multi/report-reviews/{id1}',
                                         headers={'token': 'admin-main'})).status_code == 404
                assert (await client.get(f'/api/agent/merchant/sessions/multi/report-reviews/{id2}',
                                         headers={'token': 'admin-main'})).status_code == 404
        finally:
            await env.close()

    asyncio.run(run())


def test_failed_tool_attempt_and_customer_chat_never_trigger_review(monkeypatch):
    called = []

    async def spy_reviewer(messages):
        called.append(messages)
        return json.dumps({'verdict': 'pass', 'issues': []})

    manager = ReportReviewManager(generate=spy_reviewer, timeout=2.0)
    env = _ScriptedAppEnv(monkeypatch, review_manager=manager)
    env.add_backend('/user/shop/status', 1)
    env.turns = [
        [('get_turnover_statistics', {'begin': '2026-08-07', 'end': '2026-08-01'})],
        '日期范围无效，请重新输入。',
        [('get_customer_shop_status', {})],
        '当前正在营业中。',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                bad_tool = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                             json={'session_id': 's-fail', 'message': '查无效日期'})
                assert bad_tool.status_code == 200
                assert bad_tool.json()['tool_calls'] == ['get_turnover_statistics']
                assert bad_tool.json()['report_review'] is None

                customer = await client.post('/api/agent/customer/chat', headers={'authentication': 'user-token'},
                                             json={'session_id': 'c-1', 'message': '营业吗'})
                assert customer.status_code == 200
                assert customer.json()['report_review'] is None
                assert called == []
        finally:
            await env.close()

    asyncio.run(run())


def test_ttl_expiration_and_restart_loss_map_to_404():
    now = [100.0]
    manager = ReportReviewManager(
        generate=build_mock_review_generator(),
        ttl_seconds=30.0,
        clock=lambda: now[0],
    )

    async def run():
        ref = manager.submit(
            operator_id=7,
            session_id='s-ttl',
            draft='营业额158元，不等于利润。',
            evidence=(Fact('turnover', '营业额158元。'),),
        )
        assert ref and ref.review_id
        for _ in range(30):
            state = manager.get(review_id=ref.review_id, operator_id=7, session_id='s-ttl')
            if state and state.status == 'pass':
                break
            await asyncio.sleep(0.01)
        assert manager.get(review_id=ref.review_id, operator_id=7, session_id='s-ttl').status == 'pass'
        now[0] = 131.0
        assert manager.get(review_id=ref.review_id, operator_id=7, session_id='s-ttl') is None
        await manager.shutdown()

    asyncio.run(run())


def test_backend_clients_and_auth_hardening(monkeypatch):
    env = _ScriptedAppEnv(monkeypatch, review_manager=ReportReviewManager())
    env.backend_routes[('GET', '/admin/workspace/businessData')] = (200, '<html>not json</html>')
    env.backend_routes[('GET', '/user/shop/status')] = (200, '<html>not json</html>')

    async def run():
        try:
            # Whitespace/CRLF tokens rejected immediately with 401.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                assert (await client.post('/api/agent/merchant/chat', headers={'token': '   '},
                                          json={'message': '查询'})).status_code == 401
                assert (await client.post('/api/agent/customer/chat', headers={'authentication': '   '},
                                          json={'message': '查询'})).status_code == 401

            with pytest.raises(RuntimeError, match='非 JSON'):
                await sky_backend_client.SkyBackendClient(token='admin-main').get('/admin/workspace/businessData')
            with pytest.raises(RuntimeError, match='非 JSON'):
                await customer_backend_client.CustomerBackendClient(token='user-token').get('/user/shop/status')
            with pytest.raises(RuntimeError, match='格式异常'):
                sky_backend_client.SkyBackendClient._raise_for_business_error(['not-a-dict'])
            assert report_utils.csv_values('   ', int) == []
            with pytest.raises(RuntimeError, match='格式错误'):
                report_utils.csv_values('1,bad', int)
        finally:
            await env.close()

    asyncio.run(run())
