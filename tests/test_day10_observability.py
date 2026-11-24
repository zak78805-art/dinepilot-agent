"""Day 10 tests: end-to-end trace_id correlation, PII-free telemetry, and P50/P95 reporter."""
import asyncio
from datetime import date
import io
import json
import logging
from types import SimpleNamespace

from agentscope.message import AssistantMsg, TextBlock, Usage
import httpx

from app import audit
from app.agent import merchant_agent as runtime
from app.api import routes
from app.approval.manager import ApprovalManager
from app.clients.sky_backend_client import SkyBackendClient, current_backend_token
from app.main import app
from app.observability import telemetry as telemetry_mod
from app.observability.context import current_trace_id, sanitize_or_generate_trace_id
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.tools import dish_tools, sales_tools
from tools.telemetry_report import summarize_telemetry


def run(coro):
    return asyncio.run(coro)


def test_trace_id_sanitization_blocks_header_and_log_injection():
    assert sanitize_or_generate_trace_id('trc_valid-id_2026') == 'trc_valid-id_2026'
    for bad in (None, '', 'short', 'evil\r\nSet-Cookie: a=1', 'bad space id', 'x' * 100):
        generated = sanitize_or_generate_trace_id(bad)
        assert generated.startswith('trc_') and len(generated) == 28


def test_e2e_trace_id_correlates_http_tool_audit_telemetry_review_and_approval(monkeypatch, tmp_path):
    audit_stream = io.StringIO()
    test_logger = logging.Logger('test.day10.audit')
    test_logger.addHandler(logging.StreamHandler(audit_stream))
    monkeypatch.setattr(audit, 'audit_logger', lambda: test_logger)

    telemetry_file = tmp_path / 'telemetry.jsonl'
    approval_file = tmp_path / 'approval_audit.jsonl'
    monkeypatch.setattr(telemetry_mod, '_DEFAULT_TELEMETRY_PATH', telemetry_file)

    monkeypatch.setattr(runtime, 'today', lambda: date(2026, 8, 20))
    monkeypatch.setattr(
        runtime,
        'get_settings',
        lambda: SimpleNamespace(
            dashscope_api_key='synthetic-key',
            dashscope_base_url='http://127.0.0.1:1/v1',
            agent_model='offline-model',
            agent_session_idle_seconds=1800,
            agent_session_max_entries=500,
            report_review_mode='mock',
        ),
    )

    async def fake_identity(self, path, params=None):
        assert path == '/admin/agent/identity'
        return {'code': 1, 'data': {'operator_id': 7}}

    monkeypatch.setattr(SkyBackendClient, 'get', fake_identity)

    async def fake_today_sales():
        return {
            'turnover': 320.0,
            'validOrderCount': 8,
            'orderCompletionRate': 0.8,
            'unitPrice': 40.0,
            'newUsers': 2,
        }

    async def fake_lookup_dish(*, dish_id=None, dish_name=None):
        return {
            'id': 12,
            'name': '酸菜鱼',
            'category': '招牌菜',
            'price': 58.0,
            'status': 1,
            'status_name': '起售',
        }

    monkeypatch.setattr(runtime, 'get_today_sales', fake_today_sales)
    monkeypatch.setattr(sales_tools, 'get_today_sales', fake_today_sales)
    monkeypatch.setattr(runtime, 'lookup_dish_for_status_change', fake_lookup_dish)
    monkeypatch.setattr(dish_tools, 'lookup_dish_for_status_change', fake_lookup_dish)

    token_seen_in_review: list[str | None] = []
    base_mock = build_mock_review_generator()

    async def spied_reviewer(prompt: str) -> str:
        # Must remain None inside isolated review context even though trace_id was propagated!
        token_seen_in_review.append(current_backend_token.get())
        return await base_mock(prompt)

    review_mgr = ReportReviewManager(generate=spied_reviewer, telemetry_log_path=telemetry_file)
    approval_mgr = ApprovalManager(audit_log_path=approval_file)
    service = runtime.MerchantAgent(review_manager=review_mgr, approval_manager=approval_mgr)

    class StubAgent:
        async def reply(self, msg):
            txt = msg.get_text_content()
            if '停售' in txt:
                await runtime._request_dish_status_change(target_status=0, dish_name='酸菜鱼', reason='售罄')
                return AssistantMsg(
                    name='merchant_assistant',
                    content=[TextBlock(text='已生成酸菜鱼停售审批单。')],
                    usage=Usage(input_tokens=410, output_tokens=90),
                )
            await runtime._tracked_get_today_sales()
            return AssistantMsg(
                name='merchant_assistant',
                content=[TextBlock(text='今日已完成订单营业额320元，已完成8单。')],
                usage=Usage(input_tokens=520, output_tokens=130),
            )

    monkeypatch.setattr(service, '_create_agent', lambda: StubAgent())
    monkeypatch.setattr(routes, 'merchant_agent', service)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://in-memory') as client:
            # 1. Report turn with explicit upstream X-Trace-Id
            r1 = await client.post(
                '/api/agent/merchant/chat',
                headers={'Authorization': 'Bearer secret-admin-jwt-token', 'X-Trace-Id': 'trc_e2e_report_001'},
                json={'session_id': 's-obs', 'message': '查今天经营数据 13800138000'},
            )
            assert r1.status_code == 200
            assert r1.headers['x-trace-id'] == 'trc_e2e_report_001'
            rev_id = r1.json()['report_review']['review_id']

            for _ in range(30):
                rs = await client.get(
                    f'/api/agent/merchant/sessions/s-obs/report-reviews/{rev_id}',
                    headers={'Authorization': 'Bearer secret-admin-jwt-token'},
                )
                if rs.json()['status'] not in ('pending', 'running'):
                    break
                await asyncio.sleep(0.02)

            # 2. Approval turn with malicious X-Trace-Id -> replaced with clean generated trc_...
            r2 = await client.post(
                '/api/agent/merchant/chat',
                headers={'Authorization': 'Bearer secret-admin-jwt-token', 'X-Trace-Id': 'bad\r\ninject'},
                json={'session_id': 's-obs', 'message': '把酸菜鱼停售'},
            )
            assert r2.status_code == 200
            generated_trace = r2.headers['x-trace-id']
            assert generated_trace.startswith('trc_')

        await service.shutdown()
        return generated_trace

    gen_trace = run(scenario())
    assert current_trace_id.get() is None
    assert token_seen_in_review == [None]

    # Verify tool_calls.log has trace_id
    audit_text = audit_stream.getvalue()
    assert 'trace_id=trc_e2e_report_001' in audit_text
    assert f'trace_id={gen_trace}' in audit_text
    assert 'secret-admin-jwt-token' not in audit_text
    assert '13800138000' not in audit_text

    # Verify telemetry.jsonl correlates chat_turn + report_review_completed and has ZERO PII/JWT
    raw_telemetry = telemetry_file.read_text(encoding='utf-8')
    assert 'secret-admin-jwt-token' not in raw_telemetry
    assert '13800138000' not in raw_telemetry
    assert '查今天经营数据' not in raw_telemetry

    t_rows = [json.loads(line) for line in raw_telemetry.splitlines() if line.strip()]
    turn_1 = next(r for r in t_rows if r['event'] == 'chat_turn' and r['trace_id'] == 'trc_e2e_report_001')
    assert turn_1['total_tokens'] == 650
    assert turn_1['review_submitted'] is True

    rev_row = next(r for r in t_rows if r['event'] == 'report_review_completed')
    assert rev_row['trace_id'] == 'trc_e2e_report_001'
    assert rev_row['verdict'] == 'pass'

    # Verify approval_audit.jsonl carries generated_trace
    apr_rows = [json.loads(line) for line in approval_file.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert apr_rows[0]['trace_id'] == gen_trace

    # Verify P50/P95 reporter + resilience to a corrupted line
    with telemetry_file.open('a', encoding='utf-8') as f:
        f.write('{corrupted-partial-line\n')

    summary = summarize_telemetry(telemetry_file, approval_file)
    assert summary['corrupted_lines'] == 1
    assert summary['turns']['total'] == 2
    assert summary['turns']['succeeded'] == 2
    assert summary['tokens']['grand_total'] == 1150  # 650 + 500
    assert summary['async_reviews']['total'] == 1
    assert summary['async_reviews']['pass'] == 1
    assert summary['approvals']['outcomes']['pending'] == 1
