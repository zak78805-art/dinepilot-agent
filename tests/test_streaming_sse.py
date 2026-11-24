"""Offline tests for merchant SSE streaming, tool trace events, and TTFT metrics."""
import asyncio
from datetime import date
import json
from types import SimpleNamespace

import httpx

from app.agent import merchant_agent as runtime
from app.api import routes
from app.clients import sky_backend_client
from app.clients.sky_backend_client import current_backend_token
from app.main import app
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.tools import report_utils


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.replace('\r\n', '\n').split('\n\n'):
        if not block.strip():
            continue
        name = 'message'
        data_lines = []
        for line in block.split('\n'):
            if line.startswith('event:'):
                name = line[6:].strip()
            elif line.startswith('data:'):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            events.append((name, json.loads('\n'.join(data_lines))))
    return events


def _openai_sse_body(chunks: list[dict]) -> bytes:
    lines = [f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n' for chunk in chunks]
    lines.append('data: [DONE]\n\n')
    return ''.join(lines).encode('utf-8')


def test_stream_endpoint_requires_admin_auth():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            assert (await client.post('/api/agent/merchant/chat/stream', json={'message': '查询'})).status_code == 401
            assert (await client.post(
                '/api/agent/merchant/chat/stream',
                headers={'authentication': 'user-token'},
                json={'message': '查询'},
            )).status_code == 401

    asyncio.run(run())


def test_sse_stream_emits_tool_trace_token_deltas_review_ref_and_metrics(monkeypatch):
    turns = [
        # Turn 1: tool call chunk stream
        [
            {
                'id': 'cmpl-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'offline-stream',
                'choices': [{'index': 0, 'delta': {
                    'role': 'assistant',
                    'tool_calls': [{'index': 0, 'id': 'call_sales_1', 'type': 'function',
                                    'function': {'name': 'get_today_sales', 'arguments': '{}'}}],
                }, 'finish_reason': None}],
            },
            {
                'id': 'cmpl-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'offline-stream',
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
            },
        ],
        # Turn 2: incremental text token chunks
        [
            {
                'id': 'cmpl-2', 'object': 'chat.completion.chunk', 'created': 2, 'model': 'offline-stream',
                'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '今日已完成营业额'}, 'finish_reason': None}],
            },
            {
                'id': 'cmpl-2', 'object': 'chat.completion.chunk', 'created': 2, 'model': 'offline-stream',
                'choices': [{'index': 0, 'delta': {'content': '为158元，不等于利润。'}, 'finish_reason': None}],
            },
            {
                'id': 'cmpl-2', 'object': 'chat.completion.chunk', 'created': 2, 'model': 'offline-stream',
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
            },
        ],
    ]
    models = []
    review_manager = ReportReviewManager(generate=build_mock_review_generator())

    settings = SimpleNamespace(
        dashscope_api_key='synthetic-stream-key',
        dashscope_base_url='https://model.invalid/v1',
        agent_model='offline-stream',
        agent_session_idle_seconds=1800,
        agent_session_max_entries=500,
        agent_stream_enabled=True,
        sky_backend_base_url='https://business.invalid',
    )
    for mod in (runtime, sky_backend_client):
        monkeypatch.setattr(mod, 'get_settings', lambda: settings)

    def backend_handler(request: httpx.Request):
        if request.url.path == '/admin/agent/identity':
            return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 7}})
        if request.url.path == '/admin/workspace/businessData':
            return httpx.Response(200, json={'code': 1, 'data': {
                'turnover': 158, 'validOrderCount': 2, 'orderCompletionRate': 0.5, 'unitPrice': 79, 'newUsers': 1,
            }})
        return httpx.Response(404, json={'code': 0, 'msg': 'not found'})

    def model_handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body['stream'] is True
        return httpx.Response(
            200,
            headers={'content-type': 'text/event-stream'},
            content=_openai_sse_body(turns.pop(0)),
        )

    monkeypatch.setattr(
        sky_backend_client,
        'httpx',
        SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(**kw, transport=httpx.MockTransport(backend_handler))),
    )
    actual_model = runtime.OpenAIChatModel

    def make_model(**kw):
        inst = actual_model(**kw, max_retries=0, client_kwargs={
            'max_retries': 0,
            'http_client': httpx.AsyncClient(transport=httpx.MockTransport(model_handler), trust_env=False),
        })
        models.append(inst)
        return inst

    monkeypatch.setattr(runtime, 'OpenAIChatModel', make_model)
    monkeypatch.setattr(runtime, 'record_tool_call', lambda **kw: None)
    monkeypatch.setattr(runtime, 'today', lambda: date(2026, 11, 24))
    monkeypatch.setattr(report_utils, 'today', lambda: date(2026, 11, 24))
    monkeypatch.setattr(routes, 'merchant_agent', runtime.MerchantAgent(review_manager=review_manager))

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
                resp = await client.post(
                    '/api/agent/chat',
                    headers={'token': 'admin-stream', 'Accept': 'text/event-stream'},
                    json={'session_id': 'sse-s1', 'message': '查今天经营数据'},
                )
                assert resp.status_code == 200
                assert 'text/event-stream' in resp.headers['content-type']
                assert resp.headers.get('x-accel-buffering') == 'no'

                events = _parse_sse(resp.text)
                names = [name for name, _ in events]
                assert names[0] == 'meta'
                assert 'tool_start' in names
                assert 'tool_end' in names
                assert names.count('delta') >= 2
                assert names[-1] == 'done'

                tool_start = next(p for n, p in events if n == 'tool_start')
                tool_end = next(p for n, p in events if n == 'tool_end')
                assert tool_start['tool_name'] == 'get_today_sales'
                assert tool_end['tool_name'] == 'get_today_sales'
                assert tool_end['succeeded'] is True
                assert tool_end['elapsed_ms'] >= 0

                deltas = ''.join(p['text'] for n, p in events if n == 'delta')
                assert deltas == '今日已完成营业额为158元，不等于利润。'

                done_payload = events[-1][1]
                assert done_payload['session_id'] == 'sse-s1'
                assert done_payload['answer'] == '今日已完成营业额为158元，不等于利润。'
                assert done_payload['tool_calls'] == ['get_today_sales']
                assert done_payload['report_review']['status'] == 'pending'
                assert done_payload['metrics']['ttft_ms'] >= 0
                assert done_payload['metrics']['total_ms'] >= done_payload['metrics']['ttft_ms']
                assert current_backend_token.get() is None
        finally:
            await review_manager.shutdown()
            for inst in models:
                await inst.client.close()

    asyncio.run(run())


def test_sse_stream_emits_safe_error_event_and_clears_token_on_failure(monkeypatch):
    async def identity(self, path, params=None):
        return {'code': 1, 'data': {'operator_id': 7}}

    async def failing_stream(request, operator_id):
        assert current_backend_token.get() == 'admin-secret-token'
        yield ('meta', {'session_id': request.session_id, 'agent_role': 'merchant'})
        raise RuntimeError('private-upstream-secret-traceback')

    monkeypatch.setattr(sky_backend_client.SkyBackendClient, 'get', identity)
    monkeypatch.setattr(routes.merchant_agent, 'chat_stream', failing_stream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            resp = await client.post(
                '/api/agent/merchant/chat/stream',
                headers={'token': 'admin-secret-token'},
                json={'session_id': 'sse-err', 'message': '查询'},
            )
            assert resp.status_code == 200
            events = _parse_sse(resp.text)
            assert [n for n, _ in events] == ['meta', 'error']
            assert events[1][1]['code'] == 'AGENT_ERROR'
            assert 'private-upstream-secret-traceback' not in resp.text
            assert 'admin-secret-token' not in resp.text
            assert current_backend_token.get() is None

    asyncio.run(run())
