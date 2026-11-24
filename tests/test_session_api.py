import asyncio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import app
from app.api import routes
from app.agent.merchant_agent import MerchantAgent
from app.agent.customer_agent import CustomerAgent
from app.config import Settings
from app.memory.session_store import SessionStore
from app.models.request import AgentChatRequest
from app.security.auth import require_admin, AdminIdentity
from app.security.customer_auth import require_customer, CustomerIdentity


@pytest.mark.parametrize('role', ['merchant', 'customer'])
def test_clear_requires_login(role):
    assert TestClient(app).delete(f'/api/agent/{role}/sessions/same').status_code == 401


@pytest.mark.parametrize('role,dependency,identity,cls', [
    ('merchant', require_admin, AdminIdentity(7, 'synthetic'), MerchantAgent),
    ('customer', require_customer, CustomerIdentity(7, 'synthetic'), CustomerAgent),
])
def test_clear_only_owns_current_subject_and_is_idempotent(monkeypatch, role, dependency, identity, cls):
    agent = cls()
    monkeypatch.setattr(routes, f'{role}_agent', agent)
    monkeypatch.setitem(app.dependency_overrides, dependency, lambda: identity)
    async def populate():
        for user in (7, 8):
            async with agent.sessions.use((role, user, 'same'), object):
                pass
    asyncio.run(populate())
    client = TestClient(app)
    response = client.delete(f'/api/agent/{role}/sessions/same?user_id=8&role=other')
    assert response.status_code == 200
    assert response.json() == {'agent_role': role, 'session_id': 'same', 'cleared': True}
    assert client.delete(f'/api/agent/{role}/sessions/same').json()['cleared'] is False
    assert len(agent.sessions) == 1
    assert client.delete(f'/api/agent/{role}/sessions/with%20space').status_code == 422


@pytest.mark.parametrize('role,dependency,identity', [
    ('merchant', require_admin, AdminIdentity(7, 'synthetic')),
    ('customer', require_customer, CustomerIdentity(7, 'synthetic')),
])
def test_busy_clear_returns_409(monkeypatch, role, dependency, identity):
    from app.memory.session_store import SessionBusyError
    agent = MerchantAgent() if role == 'merchant' else CustomerAgent()
    async def busy(*args):
        raise SessionBusyError('busy')
    monkeypatch.setattr(agent, 'clear_session', busy)
    monkeypatch.setattr(routes, f'{role}_agent', agent)
    monkeypatch.setitem(app.dependency_overrides, dependency, lambda: identity)
    assert TestClient(app).delete(f'/api/agent/{role}/sessions/same').status_code == 409


@pytest.mark.parametrize('role,dependency,identity', [
    ('merchant', require_admin, AdminIdentity(7, 'synthetic')),
    ('customer', require_customer, CustomerIdentity(7, 'synthetic')),
])
def test_capacity_maps_503_before_model_creation(monkeypatch, role, dependency, identity):
    agent = MerchantAgent() if role == 'merchant' else CustomerAgent()
    agent.sessions = SessionStore(1, 1800)
    async def populate():
        async with agent.sessions.use((role, 7, 'occupied'), object):
            pass
    asyncio.run(populate())
    monkeypatch.setattr(agent, '_create_agent', lambda: pytest.fail('must not create model'))
    monkeypatch.setattr(routes, f'{role}_agent', agent)
    monkeypatch.setitem(app.dependency_overrides, dependency, lambda: identity)
    response = TestClient(app).post(f'/api/agent/{role}/chat', json={'session_id': 'new', 'message': '查询'})
    assert response.status_code == 503
    from app.agent import merchant_agent as runtime
    assert runtime._operator.get() is None and runtime._tool_calls.get() is None


@pytest.mark.parametrize('field', ['agent_session_max_entries', 'agent_session_idle_seconds'])
@pytest.mark.parametrize('value', [0, -1, True])
def test_settings_reject_invalid_session_limits(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
