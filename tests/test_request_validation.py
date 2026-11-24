"""Chat admission checks; no real authentication or model calls."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.api import routes
from app.models.request import AgentChatRequest, CustomerChatRequest
from app.security.auth import AdminIdentity, require_admin
from app.security.customer_auth import CustomerIdentity, require_customer


@pytest.mark.parametrize('model', [AgentChatRequest, CustomerChatRequest])
@pytest.mark.parametrize('message', ['', ' ', '\r\n\t', '\u3000'])
def test_empty_or_whitespace_message_is_rejected(model, message):
    with pytest.raises(ValidationError):
        model(message=message)


@pytest.mark.parametrize('model', [AgentChatRequest, CustomerChatRequest])
@pytest.mark.parametrize('session_id', ['', 'a' * 129, 'with space', '../session', 'id\n'])
def test_invalid_session_never_enters_runtime(model, session_id):
    with pytest.raises(ValidationError):
        model(session_id=session_id, message='查询')


@pytest.mark.parametrize('model', [AgentChatRequest, CustomerChatRequest])
def test_valid_boundary_request_keeps_original_question(model):
    question = '  查询今天的数据\n'
    request = model(session_id='a' * 128, message=question)
    assert request.message == question
    assert request.session_id == 'a' * 128
    assert model(message='问' * 4000).message == '问' * 4000
    with pytest.raises(ValidationError):
        model(message='问' * 4001)


@pytest.mark.parametrize('path,agent,dependency,identity', [
    ('/api/agent/chat', routes.merchant_agent, require_admin, AdminIdentity(1, 'synthetic-admin')),
    ('/api/agent/merchant/chat', routes.merchant_agent, require_admin, AdminIdentity(1, 'synthetic-admin')),
    ('/api/agent/customer/chat', routes.customer_agent, require_customer, CustomerIdentity(1, 'synthetic-user')),
])
def test_blank_question_returns_422_without_allocating_conversation(monkeypatch, path, agent, dependency, identity):
    async def must_not_chat(*args, **kwargs):
        pytest.fail('invalid request must not allocate a conversation or call the model')

    monkeypatch.setitem(app.dependency_overrides, dependency, lambda: identity)
    monkeypatch.setattr(agent, 'chat', must_not_chat)
    result = TestClient(app).post(path, json={'message': '\n  '})
    assert result.status_code == 422
