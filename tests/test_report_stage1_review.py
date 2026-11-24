"""Offline unit tests for Stage 1: numeric pre-check, bounded LLM mode, and expanded eval."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agent import merchant_agent as runtime
from app.config import Settings
from app.review.evidence import extract_merchant_report_bundle
from app.review.llm_reviewer import build_llm_review_generator
from app.review.manager import ReportReviewManager
from app.review.numeric_rules import (
    OrderSnapshot,
    RankingSnapshot,
    ReportMetrics,
    TurnoverSnapshot,
    check_numeric_rules,
    compute_completion_bps,
)
from app.review.text_reviewer import Fact
from tools.report_review_eval import EVAL_CASES, run_evaluation, run_offline_eval


def test_numeric_rules_catch_hard_contradictions_without_false_positives():
    metrics = ReportMetrics(
        orders=(OrderSnapshot('orders', 4, 2, compute_completion_bps(4, 2)),),
        turnovers=(TurnoverSnapshot('turnover', 15800),),
    )
    ids = {'orders', 'turnover'}

    # Clean draft with valid numbers and prior-period comparison must not trigger false positives.
    clean = '上期完成率为75%，本期总订单数为4单，已完成订单数为2单，完成率为50%，已完成订单金额为158元，营业额不等于净利润。'
    assert check_numeric_rules(clean, metrics, ids) == ()

    # 1-decimal rounding tolerance (2/3 = 66.67% -> 66.7%) must not trigger false positives.
    third_metrics = ReportMetrics(orders=(OrderSnapshot('orders', 3, 2, compute_completion_bps(3, 2)),))
    assert check_numeric_rules('本期总订单为3单，已完成订单为2单，订单完成率为66.7%。', third_metrics, {'orders'}) == ()

    # Contradictions must be flagged with verbatim quotes in draft.
    bad = '本期总订单数为5单，已完成订单数为3单，订单完成率为75%，营业额合计为200元，营业额就是净利润。'
    issues = check_numeric_rules(bad, metrics, ids)
    assert len(issues) == 5
    for issue in issues:
        assert issue.quote in bad
        assert set(issue.evidence_ids) <= ids
    assert any(issue.code == 'overstatement' and '营业额就是净利润' in issue.quote for issue in issues)


def test_numeric_rules_zero_order_completion_rate_respects_caveat():
    zero_metrics = ReportMetrics(orders=(OrderSnapshot('orders', 0, 0, None),))
    bad = '本期总订单为0单，订单完成率为0%。'
    issues = check_numeric_rules(bad, zero_metrics, {'orders'})
    assert len(issues) == 1
    assert issues[0].quote == '订单完成率为0%'
    assert '不适用' in issues[0].suggestion

    good = '本期总订单为0单，后端返回订单完成率为0%，因无订单完成率不适用。'
    assert check_numeric_rules(good, zero_metrics, {'orders'}) == ()


def test_manager_short_circuits_model_call_when_numeric_precheck_catches_error():
    model_calls = []

    async def spy_model(messages):
        model_calls.append(messages)
        return json.dumps({'verdict': 'pass', 'issues': []})

    facts, metrics = extract_merchant_report_bundle([
        ('get_turnover_statistics', {
            'begin': '2026-08-01',
            'end': '2026-08-01',
            'daily': [{'date': '2026-08-01', 'turnover': 158.0}],
        }),
    ])
    manager = ReportReviewManager(generate=spy_model, timeout=2.0)

    async def run():
        ref = manager.submit(
            operator_id=7,
            session_id='precheck-s1',
            draft='8月1日营业额合计为200元。',
            evidence=facts,
            metrics=metrics,
        )
        assert ref and ref.review_id
        for _ in range(30):
            state = manager.get(review_id=ref.review_id, operator_id=7, session_id='precheck-s1')
            if state and state.status == 'revise':
                break
            await asyncio.sleep(0.01)
        state = manager.get(review_id=ref.review_id, operator_id=7, session_id='precheck-s1')
        assert state is not None
        assert state.status == 'revise'
        assert state.issues[0].quote == '营业额合计为200元'
        assert model_calls == []
        await manager.shutdown()

    asyncio.run(run())


def test_llm_review_generator_passes_bounded_params_records_usage_and_handles_missing_key():
    seen_bodies = []
    usage_records = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen_bodies.append(body)
        return httpx.Response(200, json={
            'id': 'offline-llm-review',
            'object': 'chat.completion',
            'created': 1,
            'model': 'qwen-review-test',
            'choices': [{
                'index': 0,
                'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps({'verdict': 'pass', 'issues': []})},
            }],
            'usage': {'prompt_tokens': 120, 'completion_tokens': 24, 'total_tokens': 144},
        })

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        gen, closer = build_llm_review_generator(
            api_key='synthetic-review-key',
            base_url='https://offline.invalid/v1',
            model='qwen-review-test',
            max_tokens=1536,
            http_timeout=12.5,
            http_client=http,
            usage_sink=usage_records.append,
        )
        manager = ReportReviewManager(generate=gen, closer=closer, timeout=5.0)
        ref = manager.submit(
            operator_id=7,
            session_id='llm-s1',
            draft='已完成订单金额共158元，不等于利润。',
            evidence=(Fact('turnover', '已完成订单金额共158元。'),),
        )
        assert ref and ref.review_id
        for _ in range(40):
            state = manager.get(review_id=ref.review_id, operator_id=7, session_id='llm-s1')
            if state and state.status == 'pass':
                break
            await asyncio.sleep(0.01)
        state = manager.get(review_id=ref.review_id, operator_id=7, session_id='llm-s1')
        assert state and state.status == 'pass'
        await manager.shutdown()

        assert len(seen_bodies) == 1
        assert seen_bodies[0]['model'] == 'qwen-review-test'
        assert seen_bodies[0]['max_tokens'] == 1536
        assert seen_bodies[0]['response_format'] == {'type': 'json_object'}
        assert 'tools' not in seen_bodies[0]
        assert usage_records == [{'prompt_tokens': 120, 'completion_tokens': 24, 'total_tokens': 144}]

        # Missing API key degrades safely to unavailable/model_error instead of crashing startup.
        bad_gen, bad_closer = build_llm_review_generator(
            api_key='',
            base_url='https://offline.invalid/v1',
            model='qwen-review-test',
        )
        bad_manager = ReportReviewManager(generate=bad_gen, closer=bad_closer, timeout=2.0)
        bad_ref = bad_manager.submit(
            operator_id=7,
            session_id='llm-s2',
            draft='已完成订单金额共158元。',
            evidence=(Fact('turnover', '已完成订单金额共158元。'),),
        )
        assert bad_ref and bad_ref.review_id
        for _ in range(40):
            bad_state = bad_manager.get(review_id=bad_ref.review_id, operator_id=7, session_id='llm-s2')
            if bad_state and bad_state.status == 'unavailable':
                break
            await asyncio.sleep(0.01)
        bad_state = bad_manager.get(review_id=bad_ref.review_id, operator_id=7, session_id='llm-s2')
        assert bad_state and bad_state.status == 'unavailable' and bad_state.failure == 'model_error'
        await bad_manager.shutdown()

    asyncio.run(run())


def test_settings_and_merchant_agent_llm_mode_validation(monkeypatch):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_http_timeout_seconds=20.0, report_review_timeout_seconds=15.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_max_tokens=64)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, report_review_http_timeout_seconds=True)

    # Instantiating MerchantAgent in llm mode does not open any network connection at init time.
    llm_settings = SimpleNamespace(
        dashscope_api_key='synthetic-only',
        dashscope_base_url='https://offline.invalid/v1',
        agent_model='qwen-main',
        agent_session_idle_seconds=1800,
        agent_session_max_entries=500,
        report_review_mode='llm',
        report_review_model='qwen-review',
        report_review_max_tokens=1536,
        report_review_http_timeout_seconds=12.5,
        report_review_timeout_seconds=15.0,
    )
    monkeypatch.setattr(runtime, 'get_settings', lambda: llm_settings)
    agent = runtime.MerchantAgent()
    assert agent.review_manager is not None and agent.review_manager.enabled is True
    asyncio.run(agent.review_manager.shutdown())


def test_sdk_timeout_exceptions_classify_as_timeout_not_model_error():
    def timeout_handler(request: httpx.Request):
        raise httpx.ReadTimeout('synthetic read timeout', request=request)

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler), trust_env=False)
        gen, closer = build_llm_review_generator(
            api_key='synthetic-review-key',
            base_url='https://offline.invalid/v1',
            model='qwen-review-test',
            max_tokens=1536,
            http_timeout=12.5,
            http_client=http,
        )
        manager = ReportReviewManager(generate=gen, closer=closer, timeout=15.0)
        ref = manager.submit(
            operator_id=7,
            session_id='llm-timeout',
            draft='已完成订单金额共158元。',
            evidence=(Fact('turnover', '已完成订单金额共158元。'),),
        )
        assert ref and ref.review_id
        for _ in range(40):
            state = manager.get(review_id=ref.review_id, operator_id=7, session_id='llm-timeout')
            if state and state.status == 'unavailable':
                break
            await asyncio.sleep(0.01)
        state = manager.get(review_id=ref.review_id, operator_id=7, session_id='llm-timeout')
        assert state and state.status == 'unavailable' and state.failure == 'timeout'
        await manager.shutdown()

    asyncio.run(run())


def test_expanded_numeric_rules_cover_unfinished_unit_price_and_ranking():
    metrics = ReportMetrics(
        orders=(OrderSnapshot('orders', 10, 7, compute_completion_bps(10, 7)),),
        turnovers=(TurnoverSnapshot('today_sales', 15800, unit_price_cents=7900, valid_orders=2),),
        rankings=(RankingSnapshot('ranking', 0),),
    )
    ids = {'orders', 'today_sales', 'ranking'}

    clean = '本期总订单为10单，已完成订单为7单，未完成订单数为3单，平均客单价为79元，销量榜返回了0项。'
    assert check_numeric_rules(clean, metrics, ids) == ()

    bad = '未完成订单数为5单，平均客单价为99元，销量榜返回了3项，最畅销的商品是酸菜鱼。'
    issues = check_numeric_rules(bad, metrics, ids)
    assert len(issues) == 4
    assert all(issue.quote in bad and set(issue.evidence_ids) <= ids for issue in issues)


def test_expanded_offline_eval_tracks_precheck_and_rates():
    report = asyncio.run(run_offline_eval())
    assert report['mode'] == 'scripted_offline'
    assert report['real_model_calls'] == 0
    assert len(EVAL_CASES) == 36
    assert report['score']['total'] == 36
    assert report['score']['precheck_caught'] == 7
    assert report['score']['correct'] == 35
    assert report['score']['unavailable'] == 1
    assert report['score']['false_positives'] == 0
    assert report['score']['false_negatives'] == 0
    assert report['model_invocations'] == 29
    assert set(report['by_category'].keys()) == {
        'baseline',
        'numeric_precheck',
        'multi_tool',
        'today_incomplete',
        'status_inference',
        'ranking_scope',
        'unsupported_cause',
        'prompt_injection',
    }
    assert report['by_category']['numeric_precheck']['precheck_caught'] == 7
    assert report['by_category']['numeric_precheck']['false_positives'] == 0
    precheck_row = next(row for row in report['cases'] if row['case'] == 'zero-rate-precheck-error')
    assert precheck_row['stage'] == 'numeric_precheck' and precheck_row['actual'] == 'revise'
