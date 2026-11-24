import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api import routes
from app.agent.merchant_agent import MerchantAgent
from app.agent.customer_agent import CustomerAgent
from app.clients.customer_backend_client import CustomerBackendClient, current_customer_id, current_customer_token
from app.clients.sky_backend_client import current_backend_token


def test_customer_route_requires_user_header_not_admin_token():
    response = TestClient(app).post('/api/agent/customer/chat', headers={'token': 'admin'}, json={'message': '推荐'})
    assert response.status_code == 401


@pytest.mark.parametrize('raises', [False, True])
def test_customer_route_sets_verified_context_and_restores_it(monkeypatch, raises):
    async def identity(self, path, params=None):
        assert path == '/user/agent/identity'
        return {'code': 1, 'data': {'user_id': 12}}
    async def chat(request, operator_id):
        assert operator_id == 12
        assert current_customer_id.get() == 12
        assert current_customer_token.get() == 'user-token'
        assert current_backend_token.get() is None
        if raises:
            raise RuntimeError('private-error')
        return {'session_id': request.session_id, 'answer': 'ok', 'tool_calls': [], 'agent_role': 'customer'}
    monkeypatch.setattr(CustomerBackendClient, 'get', identity)
    monkeypatch.setattr(routes.customer_agent, 'chat', chat)
    response = TestClient(app).post('/api/agent/customer/chat', headers={'authentication': 'user-token'}, json={'session_id': 'same', 'message': '推荐'})
    assert response.status_code == (502 if raises else 200)
    assert 'private-error' not in response.text
    assert current_customer_id.get() is None and current_customer_token.get() is None


def test_customer_cannot_select_role_or_other_user(monkeypatch):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'user_id': 12}}
    monkeypatch.setattr(CustomerBackendClient, 'get', get)
    response = TestClient(app).post('/api/agent/customer/chat', headers={'authentication': 'user-token'},
                                   json={'message': '查看订单', 'role': 'merchant', 'user_id': 999})
    assert response.status_code == 422


def test_same_ids_in_two_agents_never_share_conversation(monkeypatch):
    merchant, customer = MerchantAgent(), CustomerAgent()
    monkeypatch.setattr(merchant, '_create_agent', lambda: object())
    monkeypatch.setattr(customer, '_create_agent', lambda: object())
    async def run():
        async with merchant.sessions.use(('merchant', 12, 'same'), merchant._create_agent) as first:
            async with customer.sessions.use(('customer', 12, 'same'), customer._create_agent) as second:
                assert first is not second
        async with customer.sessions.use(('customer', 12, 'same'), customer._create_agent) as again:
            assert second is again
        async with customer.sessions.use(('customer', 13, 'same'), customer._create_agent) as other:
            assert second is not other
    asyncio.run(run())


def test_customer_toolkit_contains_only_customer_read_tools(monkeypatch):
    import app.agent.merchant_agent as module
    monkeypatch.setattr(module, 'get_settings', lambda: SimpleNamespace(dashscope_api_key='offline-test', dashscope_base_url='http://127.0.0.1:1/v1', agent_model='offline-model', agent_session_idle_seconds=1800, agent_session_max_entries=500))
    async def collect():
        agent = CustomerAgent()._create_agent()
        schemas = await agent.toolkit.get_tool_schemas()
        names = set()
        for schema in schemas:
            name = schema.get('function', schema)['name']
            names.add(name)
            tool = agent.toolkit.get_tool(name)
            if hasattr(tool, '__await__'):
                tool = await tool
            assert tool.is_read_only
        return names
    assert asyncio.run(collect()) == {'get_customer_shop_status', 'get_customer_categories', 'get_customer_menu',
                                      'get_customer_setmeal_items', 'get_my_orders', 'get_my_order_detail'}


def test_explicit_merchant_route_keeps_existing_auth():
    assert TestClient(app).post('/api/agent/merchant/chat', json={'message': '统计'}).status_code == 401


def test_customer_runtime_records_role_and_clears_audit_context(monkeypatch):
    from app.agent import merchant_agent as runtime
    from app.models.request import AgentChatRequest
    class FakeAgent:
        async def reply(self, message):
            assert runtime._actor_role.get() == 'customer'
            assert runtime._operator.get() == 12
            raise RuntimeError('offline failure')
    customer = CustomerAgent()
    monkeypatch.setattr(customer, '_create_agent', lambda: FakeAgent())
    with pytest.raises(RuntimeError):
        asyncio.run(customer.chat(AgentChatRequest(message='推荐'), 12))
    assert runtime._actor_role.get() == 'merchant'
    assert runtime._operator.get() is None


def test_overlapping_customer_and_merchant_requests_keep_identity_contexts_separate(monkeypatch):
    import httpx
    from app.clients.sky_backend_client import SkyBackendClient
    async def user_identity(self, path, params=None):
        return {'code': 1, 'data': {'user_id': {'user-a': 11, 'user-b': 22}[self.token]}}
    async def admin_identity(self, path, params=None):
        return {'code': 1, 'data': {'operator_id': 11}}
    async def user_chat(request, operator_id):
        before = current_customer_token.get()
        await asyncio.sleep(0)
        assert current_customer_token.get() == before
        assert current_customer_id.get() == operator_id
        assert current_backend_token.get() is None
        return {'agent_role': 'customer', 'session_id': request.session_id, 'answer': str(operator_id), 'tool_calls': []}
    async def admin_chat(request, operator_id):
        await asyncio.sleep(0)
        assert current_backend_token.get() == 'admin-a'
        assert current_customer_token.get() is None and current_customer_id.get() is None
        return {'agent_role': 'merchant', 'session_id': request.session_id, 'answer': str(operator_id), 'tool_calls': []}
    monkeypatch.setattr(CustomerBackendClient, 'get', user_identity)
    monkeypatch.setattr(SkyBackendClient, 'get', admin_identity)
    monkeypatch.setattr(routes.customer_agent, 'chat', user_chat)
    monkeypatch.setattr(routes.merchant_agent, 'chat', admin_chat)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            responses = await asyncio.gather(
                client.post('/api/agent/customer/chat', headers={'authentication': 'user-a'}, json={'session_id': 'same', 'message': '查询'}),
                client.post('/api/agent/customer/chat', headers={'authentication': 'user-b'}, json={'session_id': 'same', 'message': '查询'}),
                client.post('/api/agent/merchant/chat', headers={'token': 'admin-a'}, json={'session_id': 'same', 'message': '查询'}),
            )
        assert [r.status_code for r in responses] == [200, 200, 200]
        assert [r.json()['answer'] for r in responses] == ['11', '22', '11']
        assert current_customer_token.get() is None and current_backend_token.get() is None
    asyncio.run(run())
