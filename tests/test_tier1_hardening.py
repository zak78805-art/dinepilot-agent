"""Tier 1 hardening tests: timeout alignment, per-actor 429 rate limiting, token budget, Docker/CI."""
import asyncio
from pathlib import Path

import httpx
import pytest

from app.api import routes
from app.clients.customer_backend_client import CustomerBackendClient
from app.clients.sky_backend_client import SkyBackendClient
from app.config import Settings
from app.main import app
from app.security.rate_limit import ActorRateLimiter, RateLimitExceededError


def run(coro):
    return asyncio.run(coro)


def test_settings_defaults_align_with_optimized_review_timeouts_and_rate_limits():
    cfg = Settings(_env_file=None)
    assert cfg.report_review_http_timeout_seconds == 22.0
    assert cfg.report_review_timeout_seconds == 26.0
    assert cfg.chat_rate_limit_per_minute == 20
    assert cfg.daily_token_budget_per_user == 200000


def test_actor_rate_limiter_enforces_sliding_window_rpm_and_actor_isolation():
    now = [100.0]
    limiter = ActorRateLimiter(
        max_requests_per_minute=2,
        daily_token_budget=10000,
        window_seconds=60.0,
        clock=lambda: now[0],
    )

    async def scenario():
        await limiter.check_and_consume_request(role='merchant', operator_id=7)
        now[0] = 110.0
        await limiter.check_and_consume_request(role='merchant', operator_id=7)

        # 3rd request within 60s window for (merchant, 7) is rejected with retry_after
        with pytest.raises(RateLimitExceededError) as exc:
            await limiter.check_and_consume_request(role='merchant', operator_id=7)
        assert exc.value.reason == 'rpm'
        assert exc.value.retry_after >= 50

        # Different operator or different role is completely isolated!
        await limiter.check_and_consume_request(role='merchant', operator_id=8)
        await limiter.check_and_consume_request(role='customer', operator_id=7)

        # Once oldest timestamp slides past 60s window, (merchant, 7) is allowed again
        now[0] = 161.0
        await limiter.check_and_consume_request(role='merchant', operator_id=7)

    run(scenario())


def test_actor_rate_limiter_enforces_daily_token_budget():
    limiter = ActorRateLimiter(max_requests_per_minute=100, daily_token_budget=1000)

    async def scenario():
        await limiter.check_and_consume_request(role='merchant', operator_id=7)
        await limiter.record_tokens(role='merchant', operator_id=7, total_tokens=600)
        await limiter.check_and_consume_request(role='merchant', operator_id=7)
        await limiter.record_tokens(role='merchant', operator_id=7, total_tokens=450)

        with pytest.raises(RateLimitExceededError) as exc:
            await limiter.check_and_consume_request(role='merchant', operator_id=7)
        assert exc.value.reason == 'daily_token_budget'
        assert exc.value.retry_after >= 1

        # Other user still has full daily budget
        await limiter.check_and_consume_request(role='merchant', operator_id=8)

    run(scenario())


def test_chat_routes_return_http_429_with_retry_after_on_rate_limit(monkeypatch):
    strict_limiter = ActorRateLimiter(max_requests_per_minute=1, daily_token_budget=5000)
    monkeypatch.setattr(routes, 'chat_rate_limiter', strict_limiter)

    async def fake_admin_get(self, path, params=None):
        return {'code': 1, 'data': {'operator_id': 7}}

    async def fake_customer_get(self, path, params=None):
        return {'code': 1, 'data': {'user_id': 12}}

    async def fake_chat(request, operator_id):
        return {
            'session_id': request.session_id,
            'answer': 'ok',
            'tool_calls': [],
            'agent_role': 'merchant',
        }

    async def fake_cust_chat(request, operator_id):
        return {
            'session_id': request.session_id,
            'answer': 'ok',
            'tool_calls': [],
            'agent_role': 'customer',
        }

    monkeypatch.setattr(SkyBackendClient, 'get', fake_admin_get)
    monkeypatch.setattr(CustomerBackendClient, 'get', fake_customer_get)
    monkeypatch.setattr(routes.merchant_agent, 'chat', fake_chat)
    monkeypatch.setattr(routes.customer_agent, 'chat', fake_cust_chat)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            r1 = await client.post(
                '/api/agent/merchant/chat',
                headers={'Authorization': 'Bearer admin-ok'},
                json={'session_id': 's1', 'message': 'first'},
            )
            assert r1.status_code == 200

            r2 = await client.post(
                '/api/agent/merchant/chat',
                headers={'Authorization': 'Bearer admin-ok'},
                json={'session_id': 's1', 'message': 'second'},
            )
            assert r2.status_code == 429
            assert int(r2.headers['retry-after']) >= 1

            # Stream endpoint also respects the same per-actor limiter
            r3 = await client.post(
                '/api/agent/merchant/chat/stream',
                headers={'Authorization': 'Bearer admin-ok'},
                json={'session_id': 's1', 'message': 'third'},
            )
            assert r3.status_code == 429

            # Customer actor is isolated and has its own 1-req allowance
            c1 = await client.post(
                '/api/agent/customer/chat',
                headers={'Authorization': 'Bearer cust-ok'},
                json={'session_id': 'c1', 'message': 'first'},
            )
            assert c1.status_code == 200
            c2 = await client.post(
                '/api/agent/customer/chat',
                headers={'Authorization': 'Bearer cust-ok'},
                json={'session_id': 'c1', 'message': 'second'},
            )
            assert c2.status_code == 429
            assert int(c2.headers['retry-after']) >= 1

    run(scenario())


def test_docker_and_ci_artifacts_exist_and_follow_security_basics():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
    assert 'USER appuser' in dockerfile
    assert 'HEALTHCHECK' in dockerfile

    compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'STATE_STORE_MODE: redis' in compose
    assert 'redis:7-alpine' in compose

    ci = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'pytest tests' in ci
    assert 'secret_scan.py' in ci
    assert 'report_review_eval --mode offline' in ci
