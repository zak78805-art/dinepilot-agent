"""Extract scoped, de-identified aggregate evidence from verified merchant tool outputs."""
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import math

from app.review.numeric_rules import (
    OrderSnapshot,
    RankingSnapshot,
    ReportMetrics,
    TurnoverSnapshot,
    compute_completion_bps,
    decimal_to_cents,
)
from app.review.text_reviewer import Fact

REPORT_TRIGGER_TOOLS = frozenset({
    'get_today_sales',
    'get_turnover_statistics',
    'get_order_statistics',
    'get_top_dishes',
    'get_user_statistics',
    'get_period_comparison_report',
})
ALLOWED_EVIDENCE_TOOLS = REPORT_TRIGGER_TOOLS | frozenset({'get_order_status_overview'})
EXCLUDED_ORDER_TOOLS = frozenset({
    'get_order_list',
    'get_order_detail',
    'request_dish_status_change',
})

_BASE_IDS = {
    'get_today_sales': 'today_sales',
    'get_turnover_statistics': 'turnover',
    'get_order_statistics': 'orders',
    'get_top_dishes': 'ranking',
    'get_user_statistics': 'users',
    'get_order_status_overview': 'status_overview',
    'get_period_comparison_report': 'period_comparison',
}
_MAX_FACTS = 12
_MAX_FACT_TEXT = 1000


def _is_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _finite_number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        return None
    return dec if dec >= 0 else None


def _fmt_decimal(value: Decimal) -> str:
    quantized = value.quantize(Decimal('0.01'))
    text = format(quantized, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _fmt_percent(rate: Decimal) -> str:
    pct = (rate * Decimal('100')).quantize(Decimal('0.01'))
    return f'{_fmt_decimal(pct)}%'


def _valid_date_str(value: object) -> str | None:
    if type(value) is not str or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _clean_item_name(value: object) -> str | None:
    if type(value) is not str:
        return None
    cleaned = ' '.join(value.split())
    if not cleaned:
        return None
    return cleaned[:40]


def _format_today_sales(payload: dict) -> str | None:
    turnover = _finite_number(payload.get('turnover'))
    valid_orders = payload.get('validOrderCount')
    completion = _finite_number(payload.get('orderCompletionRate'))
    unit_price = _finite_number(payload.get('unitPrice'))
    new_users = payload.get('newUsers')
    if (turnover is None or not _is_int(valid_orders) or completion is None
            or unit_price is None or not _is_int(new_users)):
        return None
    if completion > Decimal('1'):
        return None
    rate_note = (
        f'{_fmt_percent(completion)}（已完成订单数/全部状态订单数；无订单时后端返回0，自然语言应表述为不适用）'
    )
    return (
        f'今日经营数据（按下单时间统计，当日未结束）：'
        f'当前已完成订单(status=5)营业额{_fmt_decimal(turnover)}元（非全部已支付金额，非利润），'
        f'已完成订单{valid_orders}单，订单完成率{rate_note}，'
        f'客单价{_fmt_decimal(unit_price)}元，当日新增注册用户{new_users}人。'
        f'未提供全部订单总数、取消数、拒单数或配送耗时明细。'
    )


def _format_turnover_statistics(payload: dict) -> str | None:
    begin = _valid_date_str(payload.get('begin'))
    end = _valid_date_str(payload.get('end'))
    daily = payload.get('daily')
    if not begin or not end or not isinstance(daily, list) or not daily or len(daily) > 366:
        return None
    total = Decimal('0')
    zero_days = 0
    for row in daily:
        if not isinstance(row, dict):
            return None
        if not _valid_date_str(row.get('date')):
            return None
        amount = _finite_number(row.get('turnover'))
        if amount is None:
            return None
        total += amount
        if amount == 0:
            zero_days += 1
    return (
        f'营业额统计（{begin}至{end}，共{len(daily)}天，按下单日期筛选）：'
        f'仅计当前已完成订单(status=5)金额，合计{_fmt_decimal(total)}元（非全部已支付金额，非利润），'
        f'其中{zero_days}天为0元。零金额只表示此口径无金额，不能断定未营业或经营异常；'
        f'若区间含今天则当日未结束。未提供成本、利润或未完成原因。'
    )


def _format_order_statistics(payload: dict) -> str | None:
    begin = _valid_date_str(payload.get('begin'))
    end = _valid_date_str(payload.get('end'))
    total_orders = payload.get('total_orders')
    valid_orders = payload.get('valid_orders')
    completion = _finite_number(payload.get('completion_rate'))
    if (not begin or not end or not _is_int(total_orders) or not _is_int(valid_orders)
            or valid_orders > total_orders or completion is None or completion > Decimal('1')):
        return None
    unfinished = total_orders - valid_orders
    if total_orders == 0:
        rate_desc = '总订单为0，完成率不适用（后端返回0），零订单不能直接断定店铺经营异常。'
    else:
        rate_desc = (
            f'完成率{_fmt_percent(completion)}（已完成/总订单），'
            f'非已完成订单合计{unfinished}单。'
        )
    return (
        f'订单统计（{begin}至{end}，按下单日期筛选）：'
        f'全部状态总订单{total_orders}单，当前已完成(status=5){valid_orders}单，'
        f'{rate_desc}未提供取消数、拒单数、待接单数或历史配送耗时，非已完成不等于已取消。'
    )


def _format_top_dishes(payload: dict) -> str | None:
    begin = _valid_date_str(payload.get('begin'))
    end = _valid_date_str(payload.get('end'))
    ranking = payload.get('ranking')
    if not begin or not end or not isinstance(ranking, list) or len(ranking) > 10:
        return None
    items: list[str] = []
    for row in ranking:
        if not isinstance(row, dict):
            return None
        name = _clean_item_name(row.get('name'))
        sales = row.get('sales')
        if not name or not _is_int(sales):
            return None
        items.append(f'{name}({sales}份)')
    summary = '、'.join(items) if items else '无销售记录'
    return (
        f'商品销量排行（{begin}至{end}，按下单日期筛选，仅计已完成订单status=5）：'
        f'按商品名称聚合最多返回前10名，本次返回{len(items)}项：{summary}。'
        f'销量榜不是完整商品目录，不能据此推断全店在售品类数、库存或利润。'
    )


def _format_user_statistics(payload: dict) -> str | None:
    begin = _valid_date_str(payload.get('begin'))
    end = _valid_date_str(payload.get('end'))
    daily = payload.get('daily')
    new_total = payload.get('new_users_total')
    if (not begin or not end or not isinstance(daily, list) or not daily
            or len(daily) > 366 or not _is_int(new_total)):
        return None
    last_total = None
    for row in daily:
        if not isinstance(row, dict) or not _valid_date_str(row.get('date')):
            return None
        t_users = row.get('total_users')
        n_users = row.get('new_users')
        if not _is_int(t_users) or not _is_int(n_users):
            return None
        last_total = t_users
    return (
        f'用户统计（{begin}至{end}，按注册时间筛选，共{len(daily)}天）：'
        f'区间新增注册用户合计{new_total}人，期末累计注册用户{last_total}人。'
        f'此为注册口径，不代表下单用户数或活跃用户数；新增为0不代表无下单。'
    )


def _format_status_overview(payload: dict) -> str | None:
    tbc = payload.get('to_be_confirmed')
    confirmed = payload.get('confirmed')
    delivering = payload.get('delivery_in_progress')
    if not (_is_int(tbc) and _is_int(confirmed) and _is_int(delivering)):
        return None
    return (
        f'当前订单流转快照（仅流转中状态）：待接单(status=2){tbc}单、'
        f'已接单待派送(status=3){confirmed}单、派送中(status=4){delivering}单。'
        f'不含已完成(5)、已取消(6)、待付款(1)，不代表历史流转或配送耗时。'
    )


def _format_signed_decimal(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        return None
    sign = '+' if dec > 0 else ('-' if dec < 0 else '')
    return f'{sign}{_fmt_decimal(abs(dec))}'


def _format_period_comparison(payload: dict) -> str | None:
    cur = payload.get('current')
    if not isinstance(cur, dict):
        return None
    cur_begin = _valid_date_str(cur.get('begin'))
    cur_end = _valid_date_str(cur.get('end'))
    cur_turnover = _finite_number(cur.get('turnover'))
    cur_total = cur.get('total_orders')
    cur_valid = cur.get('valid_orders')
    ranking = cur.get('ranking')
    if (not cur_begin or not cur_end or cur_turnover is None
            or not _is_int(cur_total) or not _is_int(cur_valid) or cur_valid > cur_total
            or not isinstance(ranking, list) or len(ranking) > 10):
        return None

    cur_unfinished = cur_total - cur_valid
    if cur_total == 0:
        cur_rate_desc = '完成率不适用（总订单为0）'
    else:
        cur_bps = compute_completion_bps(cur_total, cur_valid)
        cur_rate_desc = f'完成率{_fmt_decimal(Decimal(cur_bps) / Decimal("100"))}%' if cur_bps is not None else '完成率不适用'

    top_items: list[str] = []
    for row in ranking[:5]:
        if not isinstance(row, dict):
            return None
        name = _clean_item_name(row.get('name'))
        sales = row.get('sales')
        if not name or not _is_int(sales):
            return None
        top_items.append(f'{name}({sales}份)')
    rank_desc = f'销量榜返回{len(ranking)}项（前{len(top_items)}项：{"、".join(top_items)}）' if top_items else '销量榜返回0项（无销售记录）'

    parts = [
        f'环比经营诊断（按下单日期筛选，营业额仅计已完成订单status=5，非利润）：'
        f'本期（{cur_begin}至{cur_end}）营业额{_fmt_decimal(cur_turnover)}元，'
        f'总订单{cur_total}单，已完成{cur_valid}单，非已完成{cur_unfinished}单，{cur_rate_desc}；{rank_desc}。'
    ]

    prev_available = payload.get('previous_available') is True
    prev = payload.get('previous')
    comp = payload.get('comparison')
    if prev_available and isinstance(prev, dict) and isinstance(comp, dict):
        prev_begin = _valid_date_str(prev.get('begin'))
        prev_end = _valid_date_str(prev.get('end'))
        prev_turnover = _finite_number(prev.get('turnover'))
        prev_total = prev.get('total_orders')
        prev_valid = prev.get('valid_orders')
        if (prev_begin and prev_end and prev_turnover is not None
                and _is_int(prev_total) and _is_int(prev_valid) and prev_valid <= prev_total):
            if prev_total == 0:
                prev_rate_desc = '完成率不适用'
            else:
                prev_bps = compute_completion_bps(prev_total, prev_valid)
                prev_rate_desc = f'完成率{_fmt_decimal(Decimal(prev_bps) / Decimal("100"))}%' if prev_bps is not None else '完成率不适用'
            delta_str = _format_signed_decimal(comp.get('turnover_delta')) or '0'
            growth_str = (
                f'{_format_signed_decimal(comp.get("turnover_growth_pct"))}%'
                if comp.get('turnover_growth_applicable') and comp.get('turnover_growth_pct') is not None
                else '基数为0不适用'
            )
            rate_delta_str = (
                f'{_format_signed_decimal(comp.get("completion_rate_delta_pp"))}个百分点'
                if comp.get('completion_rate_delta_applicable') and comp.get('completion_rate_delta_pp') is not None
                else '不适用'
            )
            parts.append(
                f'上期（{prev_begin}至{prev_end}）营业额{_fmt_decimal(prev_turnover)}元，'
                f'总订单{prev_total}单，已完成{prev_valid}单，{prev_rate_desc}。'
                f'环比：营业额变化{delta_str}元（增长率{growth_str}），完成率变化{rate_delta_str}。'
            )
    else:
        parts.append('上期对照数据暂不可用，不得自行编造环比增减。')

    if payload.get('includes_today') is True:
        parts.append('本期包含今天，当日尚未结束，与完整上期对比须注明限制。')
    parts.append('未提供取消原因、拒单数、成本利润或完整商品目录，非已完成不等于已取消。')
    return ''.join(parts)


_FORMATTERS = {
    'get_today_sales': _format_today_sales,
    'get_turnover_statistics': _format_turnover_statistics,
    'get_order_statistics': _format_order_statistics,
    'get_top_dishes': _format_top_dishes,
    'get_user_statistics': _format_user_statistics,
    'get_order_status_overview': _format_status_overview,
    'get_period_comparison_report': _format_period_comparison,
}


def _extract_metrics_for_tool(
    name: str,
    fact_id: str,
    payload: dict,
) -> tuple[list[OrderSnapshot], list[TurnoverSnapshot], list[RankingSnapshot]]:
    if name == 'get_order_statistics':
        total_orders = payload.get('total_orders')
        valid_orders = payload.get('valid_orders')
        if _is_int(total_orders) and _is_int(valid_orders) and valid_orders <= total_orders:
            return (
                [OrderSnapshot(
                    evidence_id=fact_id,
                    total_orders=total_orders,
                    valid_orders=valid_orders,
                    completion_bps=compute_completion_bps(total_orders, valid_orders),
                )],
                [],
                [],
            )
    elif name == 'get_turnover_statistics':
        daily = payload.get('daily')
        if isinstance(daily, list) and daily:
            total = Decimal('0')
            for row in daily:
                amount = _finite_number(row.get('turnover')) if isinstance(row, dict) else None
                if amount is None:
                    return [], [], []
                total += amount
            return [], [TurnoverSnapshot(evidence_id=fact_id, turnover_cents=decimal_to_cents(total))], []
    elif name == 'get_today_sales':
        turnover = _finite_number(payload.get('turnover'))
        unit_price = _finite_number(payload.get('unitPrice'))
        valid_orders = payload.get('validOrderCount')
        if turnover is not None:
            return (
                [],
                [TurnoverSnapshot(
                    evidence_id=fact_id,
                    turnover_cents=decimal_to_cents(turnover),
                    unit_price_cents=decimal_to_cents(unit_price) if unit_price is not None else None,
                    valid_orders=valid_orders if _is_int(valid_orders) else None,
                )],
                [],
            )
    elif name == 'get_top_dishes':
        ranking = payload.get('ranking')
        if isinstance(ranking, list) and len(ranking) <= 10:
            return [], [], [RankingSnapshot(evidence_id=fact_id, item_count=len(ranking))]
    elif name == 'get_period_comparison_report':
        cur = payload.get('current')
        if not isinstance(cur, dict):
            return [], [], []
        o_snaps: list[OrderSnapshot] = []
        t_snaps: list[TurnoverSnapshot] = []
        r_snaps: list[RankingSnapshot] = []
        cur_total = cur.get('total_orders')
        cur_valid = cur.get('valid_orders')
        if _is_int(cur_total) and _is_int(cur_valid) and cur_valid <= cur_total:
            o_snaps.append(OrderSnapshot(
                evidence_id=fact_id,
                total_orders=cur_total,
                valid_orders=cur_valid,
                completion_bps=compute_completion_bps(cur_total, cur_valid),
            ))
        cur_turnover = _finite_number(cur.get('turnover'))
        if cur_turnover is not None:
            t_snaps.append(TurnoverSnapshot(
                evidence_id=fact_id,
                turnover_cents=decimal_to_cents(cur_turnover),
            ))
        ranking = cur.get('ranking')
        if isinstance(ranking, list) and len(ranking) <= 10:
            r_snaps.append(RankingSnapshot(evidence_id=fact_id, item_count=len(ranking)))
        # When previous period is also present, emit its snapshots so multi-period drafts
        # avoid single-snapshot false positives while keeping profit-conflation & ranking rules active.
        prev = payload.get('previous')
        if payload.get('previous_available') is True and isinstance(prev, dict):
            prev_total = prev.get('total_orders')
            prev_valid = prev.get('valid_orders')
            if _is_int(prev_total) and _is_int(prev_valid) and prev_valid <= prev_total:
                o_snaps.append(OrderSnapshot(
                    evidence_id=fact_id,
                    total_orders=prev_total,
                    valid_orders=prev_valid,
                    completion_bps=compute_completion_bps(prev_total, prev_valid),
                ))
            prev_turnover = _finite_number(prev.get('turnover'))
            if prev_turnover is not None:
                t_snaps.append(TurnoverSnapshot(
                    evidence_id=fact_id,
                    turnover_cents=decimal_to_cents(prev_turnover),
                ))
        return o_snaps, t_snaps, r_snaps
    return [], [], []


def extract_merchant_report_bundle(
    tool_results: Sequence[tuple[str, dict]],
    *,
    attempted_tools: Sequence[str] = (),
) -> tuple[tuple[Fact, ...], ReportMetrics]:
    """Extract both scoped Fact tuples and exact integer-based ReportMetrics."""
    empty_metrics = ReportMetrics()
    if any(name in EXCLUDED_ORDER_TOOLS for name in attempted_tools):
        return (), empty_metrics
    if any(name in EXCLUDED_ORDER_TOOLS for name, _ in tool_results):
        return (), empty_metrics

    facts: list[Fact] = []
    orders: list[OrderSnapshot] = []
    turnovers: list[TurnoverSnapshot] = []
    rankings: list[RankingSnapshot] = []
    counts: dict[str, int] = {}
    has_trigger_fact = False

    for name, payload in tool_results:
        if name not in ALLOWED_EVIDENCE_TOOLS or not isinstance(payload, dict):
            continue
        formatter = _FORMATTERS.get(name)
        if formatter is None:
            continue
        text = formatter(payload)
        if not text:
            continue
        text = text[:_MAX_FACT_TEXT]
        base = _BASE_IDS[name]
        counts[base] = counts.get(base, 0) + 1
        fact_id = base if counts[base] == 1 else f'{base}_{counts[base]}'
        facts.append(Fact(id=fact_id, text=text))
        order_snaps, turnover_snaps, ranking_snaps = _extract_metrics_for_tool(name, fact_id, payload)
        orders.extend(order_snaps)
        turnovers.extend(turnover_snaps)
        rankings.extend(ranking_snaps)
        if name in REPORT_TRIGGER_TOOLS:
            has_trigger_fact = True
        if len(facts) >= _MAX_FACTS:
            break

    if not has_trigger_fact:
        return (), empty_metrics
    return tuple(facts), ReportMetrics(
        orders=tuple(orders),
        turnovers=tuple(turnovers),
        rankings=tuple(rankings),
    )


def extract_merchant_report_evidence(
    tool_results: Sequence[tuple[str, dict]],
    *,
    attempted_tools: Sequence[str] = (),
) -> tuple[Fact, ...]:
    """Build bounded Fact tuples strictly from verified merchant aggregate tool results.

    Returns an empty tuple when:
    - no aggregate report tool succeeded with valid structured data; or
    - any raw order tool (order list/detail) was attempted in the same turn.
    """
    facts, _ = extract_merchant_report_bundle(tool_results, attempted_tools=attempted_tools)
    return facts
