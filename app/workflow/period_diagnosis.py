"""Controlled Planner -> Parallel Executor -> Exact Diff merchant diagnosis workflow."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import time

from app.review.numeric_rules import compute_completion_bps, decimal_to_cents
from app.tools.dish_tools import get_top_dishes
from app.tools.order_tools import get_order_statistics
from app.tools.report_utils import date_range, today
from app.tools.sales_tools import get_turnover_statistics


def _cents_to_yuan_float(cents: int) -> float:
    return float((Decimal(cents) / Decimal('100')).quantize(Decimal('0.01')))


def _sum_turnover_cents(turnover_payload: dict) -> int:
    total = Decimal('0')
    for row in turnover_payload.get('daily', []):
        total += Decimal(str(row['turnover']))
    return decimal_to_cents(total)


def _bps_to_pct_float(bps: int | None) -> float | None:
    if bps is None:
        return None
    return float((Decimal(bps) / Decimal('100')).quantize(Decimal('0.01')))


def plan_comparison_periods(begin: str, end: str) -> dict:
    """Validate current range and compute equal-length previous period."""
    span = date_range(begin, end)
    start = date.fromisoformat(span['begin'])
    finish = date.fromisoformat(span['end'])
    days = (finish - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    current_today = today()
    return {
        'current': {'begin': start.isoformat(), 'end': finish.isoformat(), 'days': days},
        'previous': {'begin': prev_start.isoformat(), 'end': prev_end.isoformat(), 'days': days},
        'includes_today': start <= current_today <= finish,
    }


async def _timed(coro):
    started = time.monotonic()
    result = await coro
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    return result, elapsed_ms


async def get_period_comparison_report(begin: str, end: str, top_limit: int = 10) -> dict:
    """并发拉取本期与等长上期的营业额、订单完成率和热销榜，并做精确分/万分比环比计算。"""
    if type(top_limit) is not int or not 1 <= top_limit <= 10:
        raise ValueError('top_limit 必须在 1 到 10 之间')

    plan = plan_comparison_periods(begin, end)
    cur = plan['current']
    prev = plan['previous']

    wall_start = time.monotonic()
    cur_turnover_res, cur_orders_res, cur_ranking_res, prev_turnover_res, prev_orders_res = await asyncio.gather(
        _timed(get_turnover_statistics(cur['begin'], cur['end'])),
        _timed(get_order_statistics(cur['begin'], cur['end'])),
        _timed(get_top_dishes(cur['begin'], cur['end'], limit=top_limit)),
        _timed(get_turnover_statistics(prev['begin'], prev['end'])),
        _timed(get_order_statistics(prev['begin'], prev['end'])),
        return_exceptions=True,
    )
    wall_ms = round((time.monotonic() - wall_start) * 1000, 1)

    # Current period queries are mandatory; fail clearly if any current call failed.
    for item in (cur_turnover_res, cur_orders_res, cur_ranking_res):
        if isinstance(item, BaseException):
            raise item

    cur_turnover, cur_t_ms = cur_turnover_res
    cur_orders, cur_o_ms = cur_orders_res
    cur_ranking, cur_r_ms = cur_ranking_res

    cur_turnover_cents = _sum_turnover_cents(cur_turnover)
    cur_total = int(cur_orders['total_orders'])
    cur_valid = int(cur_orders['valid_orders'])
    cur_unfinished = cur_total - cur_valid
    cur_bps = compute_completion_bps(cur_total, cur_valid)

    sub_timings: dict[str, float] = {
        'current_turnover_ms': cur_t_ms,
        'current_orders_ms': cur_o_ms,
        'current_ranking_ms': cur_r_ms,
    }

    previous_available = not isinstance(prev_turnover_res, BaseException) and not isinstance(prev_orders_res, BaseException)
    previous_summary: dict | None = None
    comparison: dict | None = None

    if previous_available:
        prev_turnover, prev_t_ms = prev_turnover_res
        prev_orders, prev_o_ms = prev_orders_res
        sub_timings['previous_turnover_ms'] = prev_t_ms
        sub_timings['previous_orders_ms'] = prev_o_ms

        prev_turnover_cents = _sum_turnover_cents(prev_turnover)
        prev_total = int(prev_orders['total_orders'])
        prev_valid = int(prev_orders['valid_orders'])
        prev_unfinished = prev_total - prev_valid
        prev_bps = compute_completion_bps(prev_total, prev_valid)

        delta_cents = cur_turnover_cents - prev_turnover_cents
        if prev_turnover_cents > 0:
            growth = (Decimal(delta_cents) * Decimal('100') / Decimal(prev_turnover_cents)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            turnover_growth_pct: float | None = float(growth)
        else:
            turnover_growth_pct = None

        if cur_bps is not None and prev_bps is not None:
            rate_delta_pp: float | None = _bps_to_pct_float(cur_bps - prev_bps)
        else:
            rate_delta_pp = None

        previous_summary = {
            'begin': prev['begin'],
            'end': prev['end'],
            'days': prev['days'],
            'turnover': _cents_to_yuan_float(prev_turnover_cents),
            'turnover_cents': prev_turnover_cents,
            'total_orders': prev_total,
            'valid_orders': prev_valid,
            'unfinished_orders': prev_unfinished,
            'completion_rate_pct': _bps_to_pct_float(prev_bps),
            'completion_rate_applicable': prev_bps is not None,
        }
        comparison = {
            'turnover_delta': _cents_to_yuan_float(delta_cents),
            'turnover_growth_pct': turnover_growth_pct,
            'turnover_growth_applicable': turnover_growth_pct is not None,
            'total_orders_delta': cur_total - prev_total,
            'valid_orders_delta': cur_valid - prev_valid,
            'completion_rate_delta_pp': rate_delta_pp,
            'completion_rate_delta_applicable': rate_delta_pp is not None,
        }

    serial_estimate_ms = round(sum(sub_timings.values()), 1)
    speedup = round(serial_estimate_ms / wall_ms, 2) if wall_ms > 0 else 1.0

    return {
        'workflow': 'parallel_period_comparison',
        'includes_today': plan['includes_today'],
        'current': {
            'begin': cur['begin'],
            'end': cur['end'],
            'days': cur['days'],
            'turnover': _cents_to_yuan_float(cur_turnover_cents),
            'turnover_cents': cur_turnover_cents,
            'total_orders': cur_total,
            'valid_orders': cur_valid,
            'unfinished_orders': cur_unfinished,
            'completion_rate_pct': _bps_to_pct_float(cur_bps),
            'completion_rate_applicable': cur_bps is not None,
            'ranking': cur_ranking['ranking'],
        },
        'previous_available': previous_available,
        'previous': previous_summary,
        'comparison': comparison,
        'execution_metrics': {
            'parallel_tasks': 5,
            'wall_ms': wall_ms,
            'serial_estimate_ms': serial_estimate_ms,
            'speedup_ratio': speedup,
            'sub_timings_ms': sub_timings,
        },
        'statistics_scope': {
            'time_field': 'orders.order_time',
            'turnover_status_filter': 5,
            'turnover_definition': '仅计当前已完成订单(status=5)金额合计，单位元，不是全部已支付金额，也不是利润。',
            'completion_rate_note': '已完成订单数 / 全部状态订单数；总订单为0时完成率不适用。',
            'unfinished_note': '总数减已完成数仅为非已完成合计，不能直接推断为取消单或拒单。',
            'ranking_note': '销量榜最多返回前10项，不是完整在售菜单或库存/利润依据。',
            'today_note': '若本期包含今天，则当日尚未结束，与完整上期对比时必须注明此限制。',
            'unavailable_evidence': '未提供取消原因、拒单数、历史配送耗时、成本利润与完整商品目录，不得臆测原因。',
        },
    }
