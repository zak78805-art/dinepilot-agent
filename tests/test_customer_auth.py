import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.clients import customer_backend_client as backend
from app.security.customer_auth import require_customer


def test_user_header_and_identity_not_taken_from_admin_context():
    from app.clients.sky_backend_client import current_backend_token
    token = current_backend_token.set('admin-token')
    try:
        with pytest.raises(RuntimeError):
            backend.CustomerBackendClient()._headers()
        assert backend.CustomerBackendClient(token='user-token')._headers() == {'authentication': 'user-token'}
    finally:
        current_backend_token.reset(token)


@pytest.mark.parametrize('path', ['/admin/report/top10', '/user/order/reminder/1', '/user/order/cancel/1',
                                 'http://elsewhere/user/order/historyOrders', '/user/order/../order/reminder/1'])
def test_customer_client_denies_non_readonly_paths_before_network(path):
    with pytest.raises(ValueError):
        asyncio.run(backend.CustomerBackendClient(token='x').get(path))


def test_identity_uses_user_endpoint(monkeypatch):
    async def get(self, path, params=None):
        assert path == '/user/agent/identity'
        assert self.token == 'customer-jwt'
        return {'code': 1, 'data': {'user_id': 12}}
    monkeypatch.setattr(backend.CustomerBackendClient, 'get', get)
    identity = asyncio.run(require_customer(authorization='Bearer customer-jwt', authentication=None))
    assert identity.user_id == 12


def test_customer_auth_rejects_absent_token():
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_customer(authorization=None, authentication=None))
    assert error.value.status_code == 401


@pytest.mark.parametrize('bad_id', [True, 0, '12', None])
def test_malformed_verified_user_id_fails_closed(monkeypatch, bad_id):
    async def get(self, path, params=None):
        return {'code': 1, 'data': {'user_id': bad_id}}
    monkeypatch.setattr(backend.CustomerBackendClient, 'get', get)
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_customer(authorization=None, authentication='x'))
    assert error.value.status_code == 502


def test_admin_jwt_denied_by_user_backend_stays_denied(monkeypatch):
    async def get(self, path, params=None):
        httpx.Response(401, request=httpx.Request('GET', 'http://test' + path)).raise_for_status()
    monkeypatch.setattr(backend.CustomerBackendClient, 'get', get)
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_customer(authorization='Bearer admin-jwt', authentication=None))
    assert error.value.status_code == 401


def test_real_customer_client_serializes_user_header_and_query_in_memory(monkeypatch):
    real_client = httpx.AsyncClient
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/user/order/historyOrders'
        assert request.url.params['page'] == '1'
        assert request.headers['authentication'] == 'unit-user-only'
        assert 'token' not in request.headers
        return httpx.Response(200, json={'code': 1, 'data': {'total': 0, 'records': []}})
    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(backend.httpx, 'AsyncClient', client)
    result = asyncio.run(backend.CustomerBackendClient(token='unit-user-only').get('/user/order/historyOrders', {'page': 1}))
    assert result['data']['records'] == []


@pytest.mark.parametrize('http_status,payload', [(401, {'code': 0}), (200, {'code': 0}), (200, [])])
def test_customer_client_rejects_real_transport_error_results(monkeypatch, http_status, payload):
    real_client = httpx.AsyncClient
    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(http_status, json=payload)))
    monkeypatch.setattr(backend.httpx, 'AsyncClient', client)
    expected = httpx.HTTPStatusError if http_status == 401 else RuntimeError
    with pytest.raises(expected):
        asyncio.run(backend.CustomerBackendClient(token='unit-user-only').get('/user/shop/status'))
