"""Day 7 tests: controlled parallel period-comparison workflow & reviewer integration."""
import asyncio
from datetime import date
from types import SimpleNamespace

from agentscope.message import Msg, TextBlock
import pytest

from app.agent import merchant_agent
from app.models.request import AgentChatRequest
from app.review.evidence import extract_merchant_report_bundle
from app.review.manager import ReportReviewManager
from app.review.mock_reviewer import build_mock_review_generator
from app.review.numeric_rules import check_numeric_rules
from app.workflow import period_diagnosis as pd


def run(coro):
    return asyncio.run(coro)


def _mock_today(monkeypatch, value: date):
    monkeypatch.setattr(pd, 'today', lambda: value)
    import app.tools.report_utils as report_utils
    monkeypatch.setattr(report_utils, 'today', lambda: value)


def test_plan_comparison_periods_equal_length_and_today_flag(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))
    plan = pd.plan_comparison_periods('2026-08-08', '2026-08-14')
    assert plan['current'] == {'begin': '2026-08-08', 'end': '2026-08-14', 'days': 7}
    assert plan['previous'] == {'begin': '2026-08-01', 'end': '2026-08-07', 'days': 7}
    assert plan['includes_today'] is False

    _mock_today(monkeypatch, date(2026, 8, 14))
    plan_today = pd.plan_comparison_periods('2026-08-08', '2026-08-14')
    assert plan_today['includes_today'] is True


def test_plan_comparison_periods_rejects_invalid_dates(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))
    with pytest.raises(ValueError):
        pd.plan_comparison_periods('2026-08-14', '2026-08-08')
    with pytest.raises(ValueError):
        pd.plan_comparison_periods('2026-08-19', '2026-08-25')


@pytest.mark.parametrize('bad_limit', [0, 11, -1, True, '5'])
def test_period_comparison_validates_top_limit(bad_limit):
    with pytest.raises(ValueError, match='top_limit'):
        run(pd.get_period_comparison_report('2026-08-08', '2026-08-14', top_limit=bad_limit))


def test_period_comparison_runs_five_queries_concurrently_with_exact_math(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))

    async def fake_turnover(begin: str, end: str) -> dict:
        await asyncio.sleep(0.06)
        if begin == '2026-08-08':
            # Classic IEEE-754 trap: 0.1 + 0.2 must equal exact 0.30 (30 cents).
            return {'begin': begin, 'end': end, 'daily': [
                {'date': '2026-08-08', 'turnover': 0.1},
                {'date': '2026-08-09', 'turnover': 0.2},
            ]}
        return {'begin': begin, 'end': end, 'daily': [
            {'date': '2026-08-01', 'turnover': 0.2},
        ]}

    async def fake_orders(begin: str, end: str) -> dict:
        await asyncio.sleep(0.06)
        if begin == '2026-08-08':
            return {'begin': begin, 'end': end, 'total_orders': 10, 'valid_orders': 8, 'completion_rate': 0.8}
        return {'begin': begin, 'end': end, 'total_orders': 5, 'valid_orders': 3, 'completion_rate': 0.6}

    async def fake_top(begin: str, end: str, limit: int = 10) -> dict:
        await asyncio.sleep(0.06)
        assert limit == 5
        return {'begin': begin, 'end': end, 'ranking': [
            {'name': '招牌酸菜鱼', 'sales': 6},
            {'name': '米饭', 'sales': 2},
        ]}

    monkeypatch.setattr(pd, 'get_turnover_statistics', fake_turnover)
    monkeypatch.setattr(pd, 'get_order_statistics', fake_orders)
    monkeypatch.setattr(pd, 'get_top_dishes', fake_top)

    result = run(pd.get_period_comparison_report('2026-08-08', '2026-08-14', top_limit=5))

    assert result['workflow'] == 'parallel_period_comparison'
    assert result['includes_today'] is False
    assert result['current']['turnover'] == 0.3
    assert result['current']['turnover_cents'] == 30
    assert result['current']['total_orders'] == 10
    assert result['current']['valid_orders'] == 8
    assert result['current']['unfinished_orders'] == 2
    assert result['current']['completion_rate_pct'] == 80.0
    assert result['previous_available'] is True
    assert result['previous']['turnover'] == 0.2
    assert result['previous']['completion_rate_pct'] == 60.0
    assert result['comparison'] == {
        'turnover_delta': 0.1,
        'turnover_growth_pct': 50.0,
        'turnover_growth_applicable': True,
        'total_orders_delta': 5,
        'valid_orders_delta': 5,
        'completion_rate_delta_pp': 20.0,
        'completion_rate_delta_applicable': True,
    }
    metrics = result['execution_metrics']
    assert metrics['parallel_tasks'] == 5
    assert metrics['serial_estimate_ms'] >= 250.0
    assert metrics['wall_ms'] < 220.0
    assert metrics['speedup_ratio'] >= 2.5


def test_period_comparison_handles_zero_previous_base(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))

    async def fake_turnover(begin: str, end: str) -> dict:
        val = 120.0 if begin == '2026-08-08' else 0.0
        return {'begin': begin, 'end': end, 'daily': [{'date': begin, 'turnover': val}]}

    async def fake_orders(begin: str, end: str) -> dict:
        if begin == '2026-08-08':
            return {'begin': begin, 'end': end, 'total_orders': 4, 'valid_orders': 4, 'completion_rate': 1.0}
        return {'begin': begin, 'end': end, 'total_orders': 0, 'valid_orders': 0, 'completion_rate': 0.0}

    async def fake_top(begin: str, end: str, limit: int = 10) -> dict:
        return {'begin': begin, 'end': end, 'ranking': []}

    monkeypatch.setattr(pd, 'get_turnover_statistics', fake_turnover)
    monkeypatch.setattr(pd, 'get_order_statistics', fake_orders)
    monkeypatch.setattr(pd, 'get_top_dishes', fake_top)

    result = run(pd.get_period_comparison_report('2026-08-08', '2026-08-14'))
    assert result['previous']['completion_rate_applicable'] is False
    assert result['previous']['completion_rate_pct'] is None
    assert result['comparison']['turnover_growth_applicable'] is False
    assert result['comparison']['turnover_growth_pct'] is None
    assert result['comparison']['completion_rate_delta_applicable'] is False
    assert result['comparison']['completion_rate_delta_pp'] is None


def test_period_comparison_degrades_gracefully_when_previous_fails(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))

    async def fake_turnover(begin: str, end: str) -> dict:
        if begin == '2026-08-01':
            raise RuntimeError('上期营业额接口超时')
        return {'begin': begin, 'end': end, 'daily': [{'date': begin, 'turnover': 300.0}]}

    async def fake_orders(begin: str, end: str) -> dict:
        return {'begin': begin, 'end': end, 'total_orders': 10, 'valid_orders': 9, 'completion_rate': 0.9}

    async def fake_top(begin: str, end: str, limit: int = 10) -> dict:
        return {'begin': begin, 'end': end, 'ranking': [{'name': '宫保鸡丁', 'sales': 5}]}

    monkeypatch.setattr(pd, 'get_turnover_statistics', fake_turnover)
    monkeypatch.setattr(pd, 'get_order_statistics', fake_orders)
    monkeypatch.setattr(pd, 'get_top_dishes', fake_top)

    result = run(pd.get_period_comparison_report('2026-08-08', '2026-08-14'))
    assert result['previous_available'] is False
    assert result['previous'] is None
    assert result['comparison'] is None
    assert result['current']['turnover'] == 300.0
    assert result['current']['valid_orders'] == 9


def test_period_comparison_raises_when_current_period_fails(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))

    async def fake_turnover(begin: str, end: str) -> dict:
        if begin == '2026-08-08':
            raise RuntimeError('本期营业额查询失败')
        return {'begin': begin, 'end': end, 'daily': [{'date': begin, 'turnover': 100.0}]}

    async def fake_orders(begin: str, end: str) -> dict:
        return {'begin': begin, 'end': end, 'total_orders': 2, 'valid_orders': 2, 'completion_rate': 1.0}

    async def fake_top(begin: str, end: str, limit: int = 10) -> dict:
        return {'begin': begin, 'end': end, 'ranking': []}

    monkeypatch.setattr(pd, 'get_turnover_statistics', fake_turnover)
    monkeypatch.setattr(pd, 'get_order_statistics', fake_orders)
    monkeypatch.setattr(pd, 'get_top_dishes', fake_top)

    with pytest.raises(RuntimeError, match='本期营业额查询失败'):
        run(pd.get_period_comparison_report('2026-08-08', '2026-08-14'))


def test_evidence_and_numeric_rules_for_multi_period_and_degraded_modes():
    multi_payload = {
        'workflow': 'parallel_period_comparison',
        'includes_today': True,
        'current': {
            'begin': '2026-08-08', 'end': '2026-08-14', 'days': 7,
            'turnover': 800.0, 'turnover_cents': 80000,
            'total_orders': 10, 'valid_orders': 8, 'unfinished_orders': 2,
            'completion_rate_pct': 80.0, 'completion_rate_applicable': True,
            'ranking': [],
        },
        'previous_available': True,
        'previous': {
            'begin': '2026-08-01', 'end': '2026-08-07', 'days': 7,
            'turnover': 500.0, 'turnover_cents': 50000,
            'total_orders': 10, 'valid_orders': 5, 'unfinished_orders': 5,
            'completion_rate_pct': 50.0, 'completion_rate_applicable': True,
        },
        'comparison': {
            'turnover_delta': 300.0, 'turnover_growth_pct': 60.0, 'turnover_growth_applicable': True,
            'total_orders_delta': 0, 'valid_orders_delta': 3,
            'completion_rate_delta_pp': 30.0, 'completion_rate_delta_applicable': True,
        },
    }
    facts, metrics = extract_merchant_report_bundle(
        [('get_period_comparison_report', multi_payload)],
        attempted_tools=['get_period_comparison_report'],
    )
    assert len(facts) == 1
    assert facts[0].id == 'period_comparison'
    assert '环比：营业额变化+300元（增长率+60%）' in facts[0].text
    assert '本期包含今天' in facts[0].text
    assert len(metrics.turnovers) == 2
    assert len(metrics.orders) == 2
    assert len(metrics.rankings) == 1

    # Truthful multi-period draft must NOT trigger single-snapshot false positives.
    truthful = '上个周期（2026-08-01至2026-08-07）营业额为500元，完成率为50%；本周期营业额为800元，完成率为80%。'
    assert check_numeric_rules(truthful, metrics, {'period_comparison'}) == ()

    # Profit conflation & empty ranking top claim still caught deterministically at 0 tokens.
    bad_multi = '本期营业额等于净利润，最畅销的商品是酸菜鱼。'
    issues = check_numeric_rules(bad_multi, metrics, {'period_comparison'})
    codes = {i.code for i in issues}
    assert codes == {'overstatement', 'ranking_scope'}

    # Degraded single-period mode emits single snapshot so exact numeric mismatches are caught.
    degraded = {**multi_payload, 'previous_available': False, 'previous': None, 'comparison': None}
    facts_deg, metrics_deg = extract_merchant_report_bundle(
        [('get_period_comparison_report', degraded)],
        attempted_tools=['get_period_comparison_report'],
    )
    assert '上期对照数据暂不可用' in facts_deg[0].text
    assert len(metrics_deg.turnovers) == 1
    assert len(metrics_deg.orders) == 1
    issues_deg = check_numeric_rules('本期营业额为999元，完成率为95%。', metrics_deg, {'period_comparison'})
    assert len(issues_deg) == 2


def test_merchant_agent_chat_triggers_async_review_for_period_comparison(monkeypatch):
    _mock_today(monkeypatch, date(2026, 8, 20))
    monkeypatch.setattr(
        merchant_agent,
        'get_settings',
        lambda: SimpleNamespace(
            dashscope_api_key='test-key',
            dashscope_base_url='http://127.0.0.1:1/v1',
            agent_model='test-model',
            agent_session_idle_seconds=1800,
            agent_session_max_entries=500,
            report_review_mode='mock',
        ),
    )

    async def fake_turnover(begin: str, end: str) -> dict:
        return {'begin': begin, 'end': end, 'daily': [{'date': begin, 'turnover': 200.0}]}

    async def fake_orders(begin: str, end: str) -> dict:
        return {'begin': begin, 'end': end, 'total_orders': 4, 'valid_orders': 4, 'completion_rate': 1.0}

    async def fake_top(begin: str, end: str, limit: int = 10) -> dict:
        return {'begin': begin, 'end': end, 'ranking': [{'name': '酸菜鱼', 'sales': 4}]}

    monkeypatch.setattr(pd, 'get_turnover_statistics', fake_turnover)
    monkeypatch.setattr(pd, 'get_order_statistics', fake_orders)
    monkeypatch.setattr(pd, 'get_top_dishes', fake_top)

    class StubAgent:
        async def reply(self, msg):
            await merchant_agent._period_comparison('2026-08-08', '2026-08-14', top_limit=5)
            return Msg(
                name='merchant_assistant',
                role='assistant',
                content=[TextBlock(text='本期营业额等于净利润，详见环比报告。')],
            )

    async def scenario():
        mgr = ReportReviewManager(generate=build_mock_review_generator(), timeout=5.0)
        ma = merchant_agent.MerchantAgent(review_manager=mgr)
        monkeypatch.setattr(ma, '_create_agent', lambda: StubAgent())
        resp = await ma.chat(AgentChatRequest(session_id='s-day7', message='对比最近7天和前7天'), operator_id=1)
        assert resp.tool_calls == ['get_period_comparison_report']
        assert resp.report_review is not None
        for _ in range(30):
            status = ma.get_report_review(
                review_id=resp.report_review.review_id,
                operator_id=1,
                session_id='s-day7',
            )
            if status and status.status not in ('pending', 'running'):
                break
            await asyncio.sleep(0.02)
        await mgr.shutdown()
        return status

    final_review = run(scenario())
    assert final_review is not None
    assert final_review.status == 'revise'
    assert any(i.code == 'overstatement' for i in final_review.issues)
