"""Run hand-authored report cases against explicit, scripted model replies."""
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter

from tools.report_text_review import Fact, Generate, Verdict, review_text, score_cases


@dataclass(frozen=True)
class ReviewCase:
    name: str
    draft: str
    evidence: tuple[Fact, ...]
    expected: Verdict
    expected_issue: str | None = None


_ORDERS = Fact("orders", "模拟周期 8月1日至7日：4笔订单中2笔完成，其余2笔的状态和取消数量未提供。")
_RANKING = Fact("ranking", "销量榜只显示符合口径的前10个条目，本周期返回2个名称。未查询完整菜单。")
_DELIVERY = Fact("delivery", "本期和上期完成率分别为50%和75%。没有历史配送耗时或可归因数据。")
_ZERO = Fact("zero_orders", "本期全部订单数为0，没有定义完成率，无法据此评价经营。")
_TURNOVER = Fact("turnover", "本期按下单日期统计：当前已完成订单的订单金额共158元，不是全店全部实付金额或利润。")

# Labels are assigned from the supplied evidence, not inferred from reviewer responses.
CASES = (
    ReviewCase("status-ok", "有2笔订单未完成，具体有多少笔取消还不清楚。", (_ORDERS,), "pass"),
    ReviewCase("status-error", "本期有2笔订单未完成。两笔未完成都已取消。", (_ORDERS,), "revise", "status_inference"),
    ReviewCase("ranking-ok", "销量榜返回了2个商品名称，不能据此判断在售品类。", (_RANKING,), "pass"),
    ReviewCase("ranking-error", "销量榜上有2款，所以全店只有2款在售。", (_RANKING,), "revise", "ranking_scope"),
    ReviewCase("cause-ok", "完成率比上期低25个百分点，尚不能确认原因。", (_DELIVERY,), "pass"),
    ReviewCase("cause-error", "完成率低了25个百分点，肯定是配送慢造成的。", (_DELIVERY,), "revise", "unsupported_cause"),
    ReviewCase("zero-error", "没有订单，说明店铺经营一定不正常。", (_ZERO,), "revise", "overstatement"),
    ReviewCase("turnover-ok", "按下单日期计算，当前已完成订单金额共158元，不等于利润。", (_TURNOVER,), "pass"),
)


async def evaluate(generate_for_case: Callable[[ReviewCase], Generate], *, timeout: float = 15.0) -> dict:
    rows = []
    for case in CASES:
        started = perf_counter()
        result = await review_text(case.draft, case.evidence, generate_for_case(case), timeout=timeout)
        issue_codes = [issue.code for issue in result.issues]
        rows.append({
            "case": case.name, "draft": case.draft,
            "expected": case.expected, "expected_issue": case.expected_issue,
            "actual": result.verdict, "failure": result.failure,
            "issues": [issue.model_dump() for issue in result.issues],
            "expected_issue_found": case.expected_issue in issue_codes if case.expected_issue else None,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        })
    return {"score": score_cases((row["expected"], row["actual"]) for row in rows), "cases": rows}


_SCRIPTED_ISSUES = {
    "status-error": ("status_inference", "两笔未完成都已取消。", "未提供取消数。", "两笔未完成，取消数未知。", "orders"),
    "ranking-error": ("ranking_scope", "全店只有2款在售。", "销量榜的范围不是完整菜单。", "榜单返回2款，无法确认全部在售商品数量。", "ranking"),
    "cause-error": ("unsupported_cause", "肯定是配送慢造成的。", "缺少历史配送耗时。", "下降原因尚不明确。", "delivery"),
}


def _scripted(case: ReviewCase) -> Generate:
    async def generate(_):
        if case.name == "zero-error":
            await asyncio.Event().wait()  # Intentionally exercises timeout in the report.
        if case.name in _SCRIPTED_ISSUES:
            code, quote, reason, suggestion, fact_id = _SCRIPTED_ISSUES[case.name]
            payload = {"verdict": "revise", "issues": [
                {"code": code, "quote": quote, "reason": reason, "suggestion": suggestion, "evidence_ids": [fact_id]},
            ]}
        else:
            payload = {"verdict": "pass", "issues": []}
        return json.dumps(payload, ensure_ascii=False)
    return generate


async def run_demo() -> dict:
    report = await evaluate(_scripted, timeout=0.05)
    return {"mode": "scripted_offline", "real_model_calls": 0, **report}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2))
