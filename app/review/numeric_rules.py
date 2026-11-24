"""Deterministic numeric and statistical-scope pre-checks (0 model tokens).

Only flags unambiguous contradictions with verbatim quotes present in draft.
Uses exact integer cents and basis points (10000 bps = 100.00%).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from app.review.text_reviewer import Issue


@dataclass(frozen=True)
class OrderSnapshot:
    evidence_id: str
    total_orders: int
    valid_orders: int
    completion_bps: int | None  # None when total_orders == 0.


@dataclass(frozen=True)
class TurnoverSnapshot:
    evidence_id: str
    turnover_cents: int
    unit_price_cents: int | None = None
    valid_orders: int | None = None


@dataclass(frozen=True)
class RankingSnapshot:
    evidence_id: str
    item_count: int


@dataclass(frozen=True)
class ReportMetrics:
    orders: tuple[OrderSnapshot, ...] = ()
    turnovers: tuple[TurnoverSnapshot, ...] = ()
    rankings: tuple[RankingSnapshot, ...] = ()


def compute_completion_bps(total_orders: int, valid_orders: int) -> int | None:
    if total_orders <= 0:
        return None
    return (2 * valid_orders * 10_000 + total_orders) // (2 * total_orders)


def decimal_to_cents(value: Decimal) -> int:
    return int((value * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _format_cents_yuan(cents: int) -> str:
    yuan = Decimal(cents) / Decimal('100')
    text = format(yuan.quantize(Decimal('0.01')), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _format_bps_pct(bps: int) -> str:
    pct = Decimal(bps) / Decimal('100')
    text = format(pct.quantize(Decimal('0.01')), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return f'{text or "0"}%'


def _parse_number_to_cents(raw: str) -> int | None:
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return None
    if not dec.is_finite() or dec < 0:
        return None
    return decimal_to_cents(dec)


def _parse_pct_to_bps(raw: str) -> int | None:
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return None
    if not dec.is_finite() or dec < 0 or dec > Decimal('1000'):
        return None
    return int((dec * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


_ZERO_RATE_RE = re.compile(r'(?:订单)?完成率(?:为|是|:|：)\s*0(?:\.0+)?%')
_ZERO_RATE_CAVEATS = ('不适用', '无意义', '无法计算', '没有定义', '无定义')
_RATE_VALUE_RE = re.compile(r'(?:订单)?完成率(?:为|是|:|：)\s*(\d+(?:\.\d{1,2})?)%')
_PRIOR_PREFIX_RE = re.compile(r'(?:上期|同期|去年|历史|对比|原|环比|同比|昨日|昨天)\s*$')
_TOTAL_ORDERS_RE = re.compile(r'(?:全部状态)?(?:总订单数?|全部订单数?)(?:为|是|共|:|：)\s*(\d+)\s*[笔单]')
_VALID_ORDERS_RE = re.compile(r'(?:已完成订单数?|有效订单数?)(?:为|是|共|:|：)\s*(\d+)\s*[笔单]')
_UNFINISHED_ORDERS_RE = re.compile(r'(?:未完成订单数?|非已完成订单数?)(?:为|是|共|:|：)\s*(\d+)\s*[笔单]')
_TURNOVER_VALUE_RE = re.compile(
    r'(?:已完成订单(?:总)?金额|营业额(?:合计|总计)?)(?:为|是|共|:|：)\s*[¥￥]?(\d+(?:\.\d{1,2})?)\s*元'
)
_UNIT_PRICE_RE = re.compile(r'(?:平均)?客单价(?:为|是|:|：)\s*[¥￥]?(\d+(?:\.\d{1,2})?)\s*元')
_PROFIT_CONFLATION_RE = re.compile(
    r'(?<![不非])(?:营业额|已完成订单金额)(?:就是|等于|即为?)\s*(?:净?利润|纯利润)'
)
_RANKING_COUNT_RE = re.compile(r'销量榜(?:共)?返回(?:了)?\s*(\d+)\s*[款项个]')
_EMPTY_RANKING_TOP_RE = re.compile(r'(?:最畅销|销量第一|热销第一)(?:的?商品)?(?:是|为)[^，。；\n]{1,30}')


def check_numeric_rules(
    draft: str,
    metrics: ReportMetrics | None,
    evidence_ids: set[str],
) -> tuple[Issue, ...]:
    """Run high-precision deterministic checks against verified aggregate metrics."""
    if not draft or not draft.strip() or metrics is None or not evidence_ids:
        return ()

    issues: list[Issue] = []
    seen_quotes: set[tuple[str, str]] = set()

    def add_issue(code: str, quote: str, reason: str, suggestion: str, ev_id: str) -> None:
        if len(issues) >= 8:
            return
        if not quote or quote not in draft or ev_id not in evidence_ids:
            return
        key = (code, quote)
        if key in seen_quotes:
            return
        seen_quotes.add(key)
        issues.append(Issue(
            code=code,
            quote=quote,
            reason=reason,
            suggestion=suggestion,
            evidence_ids=[ev_id],
        ))

    # 1. Single unambiguous order-statistics snapshot checks.
    if len(metrics.orders) == 1:
        snap = metrics.orders[0]
        unfinished = snap.total_orders - snap.valid_orders
        if snap.total_orders == 0:
            has_caveat = any(token in draft for token in _ZERO_RATE_CAVEATS)
            if not has_caveat:
                match = _ZERO_RATE_RE.search(draft)
                if match:
                    add_issue(
                        'other',
                        match.group(0),
                        '总订单数为0时完成率无定义，后端返回0不能直接表述为完成率0%。',
                        '建议改为「总订单为0，完成率不适用」。',
                        snap.evidence_id,
                    )
        else:
            for match in _RATE_VALUE_RE.finditer(draft):
                prefix = draft[max(0, match.start() - 8):match.start()]
                if _PRIOR_PREFIX_RE.search(prefix):
                    continue
                stated_bps = _parse_pct_to_bps(match.group(1))
                if stated_bps is not None and snap.completion_bps is not None:
                    # Allow 0.10% (10 bps) tolerance for 1-decimal natural-language rounding.
                    if abs(stated_bps - snap.completion_bps) > 10:
                        expected_text = _format_bps_pct(snap.completion_bps)
                        add_issue(
                            'other',
                            match.group(0),
                            f'按聚合证据计算，完成率应为 {expected_text}（{snap.valid_orders}/{snap.total_orders}），与草稿表述不一致。',
                            f'建议核对并改为「完成率为 {expected_text}」。',
                            snap.evidence_id,
                        )
                        break

        for match in _TOTAL_ORDERS_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 8):match.start()]
            if _PRIOR_PREFIX_RE.search(prefix):
                continue
            stated = int(match.group(1))
            if stated != snap.total_orders:
                add_issue(
                    'other',
                    match.group(0),
                    f'聚合证据显示总订单数为 {snap.total_orders} 单，与草稿中的 {stated} 不一致。',
                    f'建议改为「总订单数为 {snap.total_orders} 单」。',
                    snap.evidence_id,
                )
                break

        for match in _VALID_ORDERS_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 8):match.start()]
            if _PRIOR_PREFIX_RE.search(prefix):
                continue
            stated = int(match.group(1))
            if stated != snap.valid_orders:
                add_issue(
                    'other',
                    match.group(0),
                    f'聚合证据显示已完成订单数为 {snap.valid_orders} 单，与草稿中的 {stated} 不一致。',
                    f'建议改为「已完成订单数为 {snap.valid_orders} 单」。',
                    snap.evidence_id,
                )
                break

        for match in _UNFINISHED_ORDERS_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 8):match.start()]
            if _PRIOR_PREFIX_RE.search(prefix):
                continue
            stated = int(match.group(1))
            if stated != unfinished:
                add_issue(
                    'other',
                    match.group(0),
                    f'聚合证据显示非已完成订单合计为 {unfinished} 单（{snap.total_orders}-{snap.valid_orders}），与草稿中的 {stated} 不一致。',
                    f'建议改为「非已完成订单为 {unfinished} 单」。',
                    snap.evidence_id,
                )
                break

    # 2. Single unambiguous turnover / unit-price snapshot checks (exact cents).
    if len(metrics.turnovers) == 1:
        t_snap = metrics.turnovers[0]
        for match in _TURNOVER_VALUE_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 8):match.start()]
            if _PRIOR_PREFIX_RE.search(prefix):
                continue
            stated_cents = _parse_number_to_cents(match.group(1))
            if stated_cents is not None and stated_cents != t_snap.turnover_cents:
                expected_yuan = _format_cents_yuan(t_snap.turnover_cents)
                add_issue(
                    'other',
                    match.group(0),
                    f'聚合证据显示已完成订单营业额合计为 {expected_yuan} 元，与草稿金额不一致。',
                    f'建议核对并改为「已完成订单营业额为 {expected_yuan} 元」。',
                    t_snap.evidence_id,
                )
                break

        if t_snap.unit_price_cents is not None and (t_snap.valid_orders is None or t_snap.valid_orders > 0):
            for match in _UNIT_PRICE_RE.finditer(draft):
                prefix = draft[max(0, match.start() - 8):match.start()]
                if _PRIOR_PREFIX_RE.search(prefix):
                    continue
                stated_cents = _parse_number_to_cents(match.group(1))
                if stated_cents is not None and abs(stated_cents - t_snap.unit_price_cents) > 1:
                    expected_yuan = _format_cents_yuan(t_snap.unit_price_cents)
                    add_issue(
                        'other',
                        match.group(0),
                        f'聚合证据显示客单价为 {expected_yuan} 元，与草稿数值不一致。',
                        f'建议核对并改为「客单价为 {expected_yuan} 元」。',
                        t_snap.evidence_id,
                    )
                    break

    # 3. Turnover == profit conflation check whenever turnover evidence exists.
    if metrics.turnovers:
        t_ev = metrics.turnovers[0].evidence_id
        for match in _PROFIT_CONFLATION_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 4):match.start()]
            if any(neg in prefix for neg in ('不', '非', '并非', '并不')):
                continue
            add_issue(
                'overstatement',
                match.group(0),
                '营业额仅统计当前已完成订单(status=5)金额合计，不等于利润或全部已支付金额。',
                '建议明确表述为「已完成订单金额合计，不代表利润」。',
                t_ev,
            )
            break

    # 4. Single unambiguous ranking snapshot checks.
    if len(metrics.rankings) == 1:
        r_snap = metrics.rankings[0]
        for match in _RANKING_COUNT_RE.finditer(draft):
            prefix = draft[max(0, match.start() - 8):match.start()]
            if _PRIOR_PREFIX_RE.search(prefix):
                continue
            stated = int(match.group(1))
            if stated != r_snap.item_count:
                add_issue(
                    'ranking_scope',
                    match.group(0),
                    f'销量榜实际返回 {r_snap.item_count} 项，与草稿中的 {stated} 不一致。',
                    f'建议改为「销量榜返回了 {r_snap.item_count} 项商品」。',
                    r_snap.evidence_id,
                )
                break
        if r_snap.item_count == 0:
            match = _EMPTY_RANKING_TOP_RE.search(draft)
            if match:
                add_issue(
                    'ranking_scope',
                    match.group(0),
                    '当前统计区间销量榜为空（无已完成订单销售记录），不能断言最畅销商品。',
                    '建议表述为「该统计口径下暂无商品销售记录」。',
                    r_snap.evidence_id,
                )

    return tuple(issues)
