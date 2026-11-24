"""Offline tests for Human-in-the-Loop dish status write-approval workflow."""
import asyncio
from datetime import date
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent import merchant_agent as runtime
from app.agent.customer_agent import CustomerAgent
from app.api import routes
from app.approval.manager import ApprovalManager
from app.clients import customer_backend_client, sky_backend_client
from app.main import app
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.tools import dish_tools, report_utils


class _ApprovalTestEnv:
    def __init__(self, monkeypatch, tmp_path, *, ttl_seconds: float = 300.0):
        self.turns: list = []
        self.now = [1000.0]
        self.audit_log = tmp_path / 'approval_audit.jsonl'
        self.dishes: dict[int, dict] = {
            12: {'id': 12, 'name': '酸菜鱼', 'categoryName': '招牌菜', 'price': 58.0, 'status': 1},
            13: {'id': 13, 'name': '酸菜鱼大份', 'categoryName': '招牌菜', 'price': 78.0, 'status': 1},
            14: {'id': 14, 'name': '宫保鸡丁', 'categoryName': '热菜', 'price': 36.0, 'status': 0},
        }
        self.write_calls: list[tuple[str, str, dict]] = []
        self.models: list = []
        self.review_manager = ReportReviewManager(generate=build_mock_review_generator())
        self.approval_manager = ApprovalManager(
            ttl_seconds=ttl_seconds,
            clock=lambda: self.now[0],
            audit_log_path=self.audit_log,
        )

        settings = SimpleNamespace(
            dashscope_api_key='synthetic-key',
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
                if token == 'admin-main':
                    return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 7}})
                if token == 'admin-other':
                    return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 8}})
                return httpx.Response(401, json={'code': 0, 'msg': 'unauthorized'})
            if request.method == 'GET' and request.url.path == '/admin/dish/page':
                keyword = request.url.params.get('name', '')
                records = [d for d in self.dishes.values() if keyword in d['name']]
                return httpx.Response(200, json={'code': 1, 'data': {'total': len(records), 'records': records}})
            if request.method == 'GET' and request.url.path.startswith('/admin/dish/'):
                dish_id = int(request.url.path.rsplit('/', 1)[-1])
                dish = self.dishes.get(dish_id)
                if dish is None:
                    return httpx.Response(200, json={'code': 1, 'data': None})
                return httpx.Response(200, json={'code': 1, 'data': dict(dish)})
            if request.method == 'POST' and request.url.path.startswith('/admin/dish/status/'):
                target_status = int(request.url.path.rsplit('/', 1)[-1])
                dish_id = int(request.url.params['id'])
                self.write_calls.append((request.headers.get('token', ''), request.url.path, dict(request.url.params)))
                self.dishes[dish_id]['status'] = target_status
                return httpx.Response(200, json={'code': 1, 'data': None})
            return httpx.Response(404, json={'code': 0, 'msg': 'not found'})

        def model_handler(_request: httpx.Request):
            assert self.turns, 'model script exhausted'
            turn = self.turns.pop(0)
            message = {'role': 'assistant', 'content': turn if isinstance(turn, str) else None}
            if not isinstance(turn, str):
                message['tool_calls'] = [
                    {'id': f'call_{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}}
                    for i, (n, a) in enumerate(turn)
                ]
            return httpx.Response(200, json={
                'id': 'offline-approval',
                'object': 'chat.completion',
                'created': 0,
                'model': 'offline-script',
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop' if isinstance(turn, str) else 'tool_calls'}],
            })

        backend_http = SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(
            **kw, transport=httpx.MockTransport(backend_handler)))
        monkeypatch.setattr(sky_backend_client, 'httpx', backend_http)
        monkeypatch.setattr(customer_backend_client, 'httpx', backend_http)

        actual_model = runtime.OpenAIChatModel

        def make_model(**kw):
            inst = actual_model(**kw, max_retries=0, client_kwargs={
                'max_retries': 0,
                'http_client': httpx.AsyncClient(transport=httpx.MockTransport(model_handler), trust_env=False),
            })
            self.models.append(inst)
            return inst

        monkeypatch.setattr(runtime, 'OpenAIChatModel', make_model)
        monkeypatch.setattr(runtime, 'record_tool_call', lambda **kw: None)
        monkeypatch.setattr(runtime, 'today', lambda: date(2026, 11, 24))
        monkeypatch.setattr(report_utils, 'today', lambda: date(2026, 11, 24))
        monkeypatch.setattr(routes, 'merchant_agent', runtime.MerchantAgent(
            review_manager=self.review_manager,
            approval_manager=self.approval_manager,
        ))
        monkeypatch.setattr(routes, 'customer_agent', CustomerAgent())

    async def close(self):
        await self.review_manager.shutdown()
        for inst in self.models:
            await inst.client.close()


def test_hitl_approval_propose_confirm_idempotency_isolation_and_audit(monkeypatch, tmp_path):
    env = _ApprovalTestEnv(monkeypatch, tmp_path)
    env.turns = [
        [('request_dish_status_change', {'dish_name': '酸菜鱼', 'target_status': 0, 'reason': '今日沽清'})],
        '已生成「酸菜鱼」停售审批单，请在页面点击确认执行后生效。',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                chat_resp = await client.post(
                    '/api/agent/merchant/chat',
                    headers={'token': 'admin-main'},
                    json={'session_id': 'apr-s1', 'message': '把酸菜鱼停售，今天卖完了'},
                )
                assert chat_resp.status_code == 200
                body = chat_resp.json()
                assert body['approval_required'] is True
                assert body['approval_id'].startswith('apr_')
                assert body['approval']['status'] == 'pending'
                assert body['approval']['target']['dish_id'] == 12
                assert body['approval']['before_state'] == {'status': 1, 'status_name': '在售'}
                assert body['approval']['after_state'] == {'status': 0, 'status_name': '停售'}
                assert body['report_review'] is None  # Write-approval turn never triggers report review.
                # CRITICAL: LLM tool call MUST NOT have written to Java backend!
                assert env.write_calls == []
                assert env.dishes[12]['status'] == 1

                approval_id = body['approval_id']

                # Unauthenticated / customer / other admin cannot read or confirm.
                assert (await client.get(f'/api/agent/merchant/approvals/{approval_id}')).status_code == 401
                assert (await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/confirm',
                    headers={'authentication': 'user-token'},
                )).status_code == 401
                assert (await client.get(
                    f'/api/agent/merchant/approvals/{approval_id}',
                    headers={'token': 'admin-other'},
                )).status_code == 404
                assert (await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/confirm',
                    headers={'token': 'admin-other'},
                )).status_code == 404
                # Client cannot spoof operator_id in request body (extra='forbid').
                assert (await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/confirm',
                    headers={'token': 'admin-main'},
                    json={'operator_id': 999},
                )).status_code == 422
                assert env.write_calls == []

                # Legitimate owner confirms -> executes Java POST once!
                confirmed = await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/confirm',
                    headers={'token': 'admin-main'},
                    json={'reason': '确认沽清'},
                )
                assert confirmed.status_code == 200
                c_body = confirmed.json()
                assert c_body['status'] == 'executed'
                assert env.dishes[12]['status'] == 0
                assert env.write_calls == [('admin-main', '/admin/dish/status/0', {'id': '12'})]

                # Replay confirm or reject must fail with 409 Conflict without touching Java again!
                assert (await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/confirm',
                    headers={'token': 'admin-main'},
                )).status_code == 409
                assert (await client.post(
                    f'/api/agent/merchant/approvals/{approval_id}/reject',
                    headers={'token': 'admin-main'},
                )).status_code == 409
                assert len(env.write_calls) == 1

                # Persistent JSONL audit log records creation & confirm without leaking admin token.
                lines = [json.loads(line) for line in env.audit_log.read_text(encoding='utf-8').splitlines()]
                assert [row['event'] for row in lines] == ['approval_created', 'approval_confirm']
                assert lines[1]['outcome'] == 'executed' and lines[1]['dish_id'] == 12
                assert 'admin-main' not in env.audit_log.read_text(encoding='utf-8')
        finally:
            await env.close()

    asyncio.run(run())


def test_hitl_approval_reject_expiry_stale_conflict_and_session_clear(monkeypatch, tmp_path):
    env = _ApprovalTestEnv(monkeypatch, tmp_path, ttl_seconds=60.0)
    env.turns = [
        [('request_dish_status_change', {'dish_id': 14, 'target_status': 1})],
        '已生成「宫保鸡丁」起售审批单。',
        [('request_dish_status_change', {'dish_id': 12, 'target_status': 0})],
        '已生成「酸菜鱼」停售审批单。',
        [('request_dish_status_change', {'dish_id': 13, 'target_status': 0})],
        '已生成「酸菜鱼大份」停售审批单。',
        [('request_dish_status_change', {'dish_id': 12, 'target_status': 0})],
        '已生成清空会话测试审批单。',
    ]

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                # 1. Reject path -> Java untouched.
                r1 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 's-rej', 'message': '起售宫保鸡丁'})
                id1 = r1.json()['approval_id']
                rej = await client.post(f'/api/agent/merchant/approvals/{id1}/reject',
                                        headers={'token': 'admin-main'}, json={'reason': '暂不恢复'})
                assert rej.status_code == 200 and rej.json()['status'] == 'rejected'
                assert env.dishes[14]['status'] == 0 and env.write_calls == []

                # 2. Optimistic stale-state conflict -> external change before confirm returns 409.
                r2 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 's-stale', 'message': '停售酸菜鱼'})
                id2 = r2.json()['approval_id']
                env.dishes[12]['status'] = 0  # Externally changed!
                conflict = await client.post(f'/api/agent/merchant/approvals/{id2}/confirm',
                                             headers={'token': 'admin-main'})
                assert conflict.status_code == 409
                assert env.write_calls == []

                # 3. TTL expiration -> returns 410 Gone.
                r3 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 's-exp', 'message': '停售酸菜鱼大份'})
                id3 = r3.json()['approval_id']
                env.now[0] += 61.0
                expired = await client.post(f'/api/agent/merchant/approvals/{id3}/confirm',
                                            headers={'token': 'admin-main'})
                assert expired.status_code == 410
                assert env.write_calls == []

                # 4. Clear session expires pending approvals in that session.
                env.dishes[12]['status'] = 1
                r4 = await client.post('/api/agent/merchant/chat', headers={'token': 'admin-main'},
                                       json={'session_id': 's-clr', 'message': '停售酸菜鱼'})
                id4 = r4.json()['approval_id']
                assert (await client.delete('/api/agent/merchant/sessions/s-clr',
                                            headers={'token': 'admin-main'})).status_code == 200
                assert (await client.post(f'/api/agent/merchant/approvals/{id4}/confirm',
                                          headers={'token': 'admin-main'})).status_code == 410
                assert env.write_calls == []
        finally:
            await env.close()

    asyncio.run(run())


def test_direct_change_dish_status_stub_remains_blocked():
    with pytest.raises(RuntimeError, match='request_dish_status_change'):
        asyncio.run(dish_tools.change_dish_status(12, 0))
