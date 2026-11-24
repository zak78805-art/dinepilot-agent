"""Offline end-to-end ASGI demo for async merchant report review (0 real model calls)."""
import asyncio
from datetime import date
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import merchant_agent as runtime
from app.api import routes
from app.clients import sky_backend_client
from app.main import app
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.tools import report_utils


async def run_async_review_demo() -> dict:
    mode = {'kind': 'mock'}

    async def scripted_reviewer(messages):
        if mode['kind'] == 'timeout':
            await asyncio.Event().wait()
        return await build_mock_review_generator(delay_seconds=0.015)(messages)

    manager = ReportReviewManager(generate=scripted_reviewer, timeout=0.05, poll_after_ms=20)
    turns = [
        [('get_order_statistics', {'begin': '2026-08-01', 'end': '2026-08-07'})],
        '8月1日至7日总订单4笔，已完成2笔，其余2笔未完成，具体取消数尚不明确。',
        [('get_order_statistics', {'begin': '2026-08-01', 'end': '2026-08-07'})],
        '8月1日至7日总订单4笔，已完成2笔，其余2笔都已取消。',
        [('get_order_statistics', {'begin': '2026-08-01', 'end': '2026-08-07'})],
        '8月1日至7日总订单4笔，已完成2笔，未完成原因需进一步核查。',
    ]
    models = []

    def backend_handler(request: httpx.Request):
        if request.url.path == '/admin/agent/identity':
            return httpx.Response(200, json={'code': 1, 'data': {'operator_id': 7}})
        if request.url.path == '/admin/report/ordersStatistics':
            return httpx.Response(200, json={'code': 1, 'data': {
                'dateList': '2026-08-01', 'orderCountList': '4', 'validOrderCountList': '2',
                'totalOrderCount': 4, 'validOrderCount': 2, 'orderCompletionRate': 0.5,
            }})
        return httpx.Response(404, json={'code': 0, 'msg': 'not found'})

    def model_handler(_request: httpx.Request):
        turn = turns.pop(0)
        message = {'role': 'assistant', 'content': turn if isinstance(turn, str) else None}
        if not isinstance(turn, str):
            message['tool_calls'] = [
                {'id': f'call_{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}}
                for i, (n, a) in enumerate(turn)
            ]
        return httpx.Response(200, json={
            'id': 'offline-async-demo', 'object': 'chat.completion', 'created': 0, 'model': 'offline-script',
            'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop' if isinstance(turn, str) else 'tool_calls'}],
        })

    orig_settings_rt = runtime.get_settings
    orig_settings_bk = sky_backend_client.get_settings
    orig_httpx = sky_backend_client.httpx
    orig_model = runtime.OpenAIChatModel
    orig_audit = runtime.record_tool_call
    orig_today_rt = runtime.today
    orig_today_ru = report_utils.today
    orig_merchant = routes.merchant_agent

    settings = SimpleNamespace(
        dashscope_api_key='synthetic-demo-key',
        dashscope_base_url='https://model.invalid/v1',
        agent_model='offline-script',
        agent_session_idle_seconds=1800,
        agent_session_max_entries=500,
        sky_backend_base_url='https://business.invalid',
    )

    try:
        runtime.get_settings = lambda: settings
        sky_backend_client.get_settings = lambda: settings
        sky_backend_client.httpx = SimpleNamespace(
            AsyncClient=lambda **kw: httpx.AsyncClient(**kw, transport=httpx.MockTransport(backend_handler))
        )

        def make_model(**kw):
            inst = orig_model(**kw, max_retries=0, client_kwargs={
                'max_retries': 0,
                'http_client': httpx.AsyncClient(transport=httpx.MockTransport(model_handler), trust_env=False),
            })
            models.append(inst)
            return inst

        runtime.OpenAIChatModel = make_model
        runtime.record_tool_call = lambda **kw: None
        runtime.today = lambda: date(2026, 11, 1)
        report_utils.today = lambda: date(2026, 11, 1)
        routes.merchant_agent = runtime.MerchantAgent(review_manager=manager)

        results = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            for case_name, review_mode in [('pass-case', 'mock'), ('revise-case', 'mock'), ('timeout-case', 'timeout')]:
                mode['kind'] = review_mode
                chat_resp = await client.post(
                    '/api/agent/merchant/chat',
                    headers={'token': 'synthetic-admin'},
                    json={'session_id': f'demo-{case_name}', 'message': '分析订单统计'},
                )
                chat_body = chat_resp.json()
                review_id = chat_body['report_review']['review_id']
                initial_status = chat_body['report_review']['status']
                final = None
                for _ in range(50):
                    poll = await client.get(
                        f'/api/agent/merchant/sessions/demo-{case_name}/report-reviews/{review_id}',
                        headers={'token': 'synthetic-admin'},
                    )
                    body = poll.json()
                    if body['status'] in ('pass', 'revise', 'unavailable'):
                        final = body
                        break
                    await asyncio.sleep(0.01)
                results.append({
                    'case': case_name,
                    'first_screen_answer': chat_body['answer'],
                    'initial_review_status': initial_status,
                    'final_review_status': final['status'] if final else None,
                    'failure': final['failure'] if final else None,
                    'issues': final['issues'] if final else [],
                })
        return {'mode': 'scripted_offline_async', 'real_model_calls': 0, 'cases': results}
    finally:
        await manager.shutdown()
        for inst in models:
            await inst.client.close()
        runtime.get_settings = orig_settings_rt
        sky_backend_client.get_settings = orig_settings_bk
        sky_backend_client.httpx = orig_httpx
        runtime.OpenAIChatModel = orig_model
        runtime.record_tool_call = orig_audit
        runtime.today = orig_today_rt
        report_utils.today = orig_today_ru
        routes.merchant_agent = orig_merchant


if __name__ == '__main__':
    print(json.dumps(asyncio.run(run_async_review_demo()), ensure_ascii=False, indent=2))
