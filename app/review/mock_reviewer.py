"""Simulated offline review generator for workflow verification (zero real model calls)."""
import asyncio
import json

from app.review.text_reviewer import Generate, Messages


def build_mock_review_generator(*, delay_seconds: float = 0.0) -> Generate:
    """Deterministic offline mock reviewer; NOT a real LLM or production accuracy claim."""
    async def generate(messages: Messages) -> str | None:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        payload = json.loads(messages[1]['content'])
        draft = payload.get('draft', '')
        evidence = payload.get('evidence', [])
        ids = {item['id'] for item in evidence if isinstance(item, dict) and 'id' in item}

        issues: list[dict] = []

        def first_id(*preferred: str) -> str | None:
            for candidate in preferred:
                if candidate in ids:
                    return candidate
            return next(iter(ids), None)

        checks = [
            (
                '都已取消',
                'status_inference',
                '现有统计未提供取消订单数量，不能把未完成订单全部推断为已取消。',
                '建议表述为「存在未完成订单，具体取消数量需按状态另行核查」。',
                ('orders', 'today_sales'),
            ),
            (
                '全店只有',
                'ranking_scope',
                '销量榜最多仅展示前10名已完成订单商品，不代表全店完整在售品类。',
                '建议表述为「销量榜返回部分商品，无法据此确认全部在售品类数」。',
                ('ranking',),
            ),
            (
                '配送慢造成的',
                'unsupported_cause',
                '本轮聚合证据不含历史配送耗时，无法证实下降原因来自配送慢。',
                '建议表述为「下降原因尚无直接证据，建议结合履约明细核查」。',
                ('orders', 'today_sales', 'turnover'),
            ),
            (
                '经营一定不正常',
                'overstatement',
                '零订单或零金额只说明当前统计口径下无记录，不能直接断定店铺经营异常。',
                '建议表述为「当前口径下无订单记录，建议结合营业状态进一步核查」。',
                ('orders', 'turnover', 'today_sales'),
            ),
        ]

        for phrase, code, reason, suggestion, preferred in checks:
            if phrase in draft:
                ev_id = first_id(*preferred)
                if ev_id:
                    issues.append({
                        'code': code,
                        'quote': phrase,
                        'reason': reason,
                        'suggestion': suggestion,
                        'evidence_ids': [ev_id],
                    })

        if issues:
            return json.dumps({'verdict': 'revise', 'issues': issues}, ensure_ascii=False)
        return json.dumps({'verdict': 'pass', 'issues': []}, ensure_ascii=False)

    return generate
