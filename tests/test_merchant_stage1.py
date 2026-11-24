import asyncio
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.clients.sky_backend_client import SkyBackendClient, current_backend_token
from app.tools import sales_tools, order_tools, dish_tools
from app.tools.report_utils import date_range
from app.security import auth
from app.agent.merchant_agent import MerchantAgent
from app.api import routes


def test_chat_requires_verified_admin():
    response = TestClient(app).post('/api/agent/chat', json={'message':'分析最近七天'})
    assert response.status_code == 401


def test_auth_rejects_backend_denial(monkeypatch):
    async def deny(self, path, params=None):
        response = httpx.Response(401, request=httpx.Request('GET','http://test/admin/agent/identity'))
        response.raise_for_status()
    monkeypatch.setattr(SkyBackendClient, 'get', deny)
    response = TestClient(app).post('/api/agent/chat', headers={'token':'invalid'}, json={'message':'test'})
    assert response.status_code == 401


def test_verified_identity_and_token_reach_chat_and_context_is_reset(monkeypatch):
    async def identity(self, path, params=None):
        assert path == '/admin/agent/identity'
        assert self.token == 'admin-a'
        return {'code':1,'data':{'operator_id':7}}
    async def chat(request, operator_id):
        assert operator_id == 7
        assert current_backend_token.get() == 'admin-a'
        return {'session_id':request.session_id,'answer':'ok','tool_calls':[]}
    monkeypatch.setattr(SkyBackendClient, 'get', identity)
    monkeypatch.setattr(routes.merchant_agent, 'chat', chat)
    response = TestClient(app).post('/api/agent/chat', headers={'Authorization':'Bearer admin-a'},json={'message':'test'})
    assert response.status_code == 200
    assert current_backend_token.get() is None


def test_sessions_are_namespaced_by_operator(monkeypatch):
    service = MerchantAgent()
    monkeypatch.setattr(service, '_create_agent', lambda: object())
    async def run():
        async with service.sessions.use(('merchant', 1, 'same-id'), service._create_agent) as first:
            pass
        async with service.sessions.use(('merchant', 1, 'same-id'), service._create_agent) as again:
            assert first is again
        async with service.sessions.use(('merchant', 2, 'same-id'), service._create_agent) as other:
            assert first is not other
    asyncio.run(run())


@pytest.mark.parametrize('begin,end',[('2026-02-02','2026-02-01'),('2024-01-01','2026-01-01'),('2999-01-01','2999-01-02')])
def test_invalid_date_ranges_rejected(begin,end):
    with pytest.raises(ValueError):
        date_range(begin,end)


def test_three_reports_map_backend_csv_to_structured_data(monkeypatch):
    async def get(self, path, params=None):
        assert self.token == 'request-token'
        assert params == {'begin':'2026-01-01','end':'2026-01-02'}
        data = {
            '/admin/report/turnoverStatistics':{'dateList':'2026-01-01,2026-01-02','turnoverList':'12.5,20'},
            '/admin/report/ordersStatistics':{'dateList':'2026-01-01,2026-01-02','orderCountList':'2,3','validOrderCountList':'1,3','totalOrderCount':5,'validOrderCount':4,'orderCompletionRate':0.8},
            '/admin/report/top10':{'nameList':'鱼,饭','numberList':'10,8'},
        }[path]
        return {'code':1,'data':data}
    monkeypatch.setattr(SkyBackendClient,'get',get)
    async def run():
        token=current_backend_token.set('request-token')
        try:
            sales=await sales_tools.get_turnover_statistics('2026-01-01','2026-01-02')
            orders=await order_tools.get_order_statistics('2026-01-01','2026-01-02')
            dishes=await dish_tools.get_top_dishes('2026-01-01','2026-01-02',1)
            assert sales['daily'][1] == {'date':'2026-01-02','turnover':20.0}
            assert orders['daily'][0]['valid_orders'] == 1
            assert orders['completion_rate'] == 0.8
            assert dishes['ranking'] == [{'name':'鱼','sales':10}]
        finally:
            current_backend_token.reset(token)
    asyncio.run(run())


def test_missing_request_token_does_not_use_shared_config():
    token=current_backend_token.set(None)
    try:
        with pytest.raises(RuntimeError):
            SkyBackendClient()._headers()
    finally:
        current_backend_token.reset(token)
