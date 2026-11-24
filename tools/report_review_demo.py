"""Offline handoff experiment: scripted merchant writer, rule-based reviewer.

This module does not import the app, read environment settings or call any service.
"""
from dataclasses import asdict, dataclass, fields
import json
from collections.abc import Callable


@dataclass(frozen=True)
class Evidence:
    period: str
    completed_turnover_cents: int
    total_orders: int
    completed_orders: int
    canceled_orders: int | None  # None means the source did not provide it.

    def __post_init__(self):
        numbers = (self.completed_turnover_cents, self.total_orders, self.completed_orders)
        if any(type(value) is not int or value < 0 for value in numbers):
            raise ValueError("evidence amounts and counts must be nonnegative integers")
        canceled = self.canceled_orders
        if (self.completed_orders > self.total_orders
                or canceled is not None and (
                    type(canceled) is not int or not 0 <= canceled <= self.total_orders - self.completed_orders
                )):
            raise ValueError("evidence order counts contradict one another")
        if not self.completed_orders and self.completed_turnover_cents:
            raise ValueError("turnover must be zero when no orders are complete")


@dataclass(frozen=True)
class Report:
    period: str
    turnover_cents: int
    turnover_scope: str
    total_orders: int
    completed_orders: int
    completion_bps: int | None  # 5,000 basis points = 50.00%.
    canceled_orders: int | None
    delivery_cause: str


@dataclass(frozen=True)
class Review:
    final: Report
    issues: tuple[str, ...]


_ISSUE_CODES = (
    "period_mismatch", "turnover_mismatch", "turnover_scope_mismatch", "total_mismatch",
    "completed_mismatch", "rate_mismatch", "cancellations_mismatch", "unsupported_cause",
)


def _from_evidence(evidence: Evidence) -> Report:
    n = evidence.total_orders
    # Use integer arithmetic and round half up. No orders means no defined rate.
    bps = (2 * evidence.completed_orders * 10_000 + n) // (2 * n) if n else None
    return Report(
        evidence.period, evidence.completed_turnover_cents, "completed_orders",
        n, evidence.completed_orders, bps, evidence.canceled_orders, "unknown",
    )


def checks(evidence: Evidence, candidate: Report) -> tuple[str, ...]:
    expected = _from_evidence(evidence)
    issues = []
    for field, issue in zip(fields(Report), _ISSUE_CODES, strict=True):
        value, reference = getattr(candidate, field.name), getattr(expected, field.name)
        if type(value) is not type(reference) or value != reference:
            issues.append(issue)
    return tuple(issues)


def review_report(evidence: Evidence, draft: Report) -> Review:
    issues = checks(evidence, draft)
    return Review(_from_evidence(evidence) if issues else draft, issues)


def simulate_handoff(
    evidence: Evidence,
    writer: Callable[[Evidence], Report],
    reviewer: Callable[[Evidence, Report], Review] = review_report,
) -> tuple[Report, Review]:
    draft = writer(evidence)
    reviewed = reviewer(evidence, draft)
    if checks(evidence, reviewed.final):
        raise ValueError("final report did not pass verification")
    return draft, reviewed


def demo_cases() -> tuple[tuple[str, Evidence, Report], ...]:
    week = Evidence("2026-08-01/2026-08-07", 15800, 4, 2, 1)
    empty = Evidence("2026-08-08/2026-08-14", 0, 0, 0, None)
    limited = Evidence("2026-08-15/2026-08-21", 6200, 4, 1, None)
    return (
        ("clean", week, Report(week.period, 15800, "completed_orders", 4, 2, 5000, 1, "unknown")),
        ("wrong_numbers", week, Report(week.period, 17600, "all_paid_orders", 4, 2, 7500, 1, "unknown")),
        ("empty_week", empty, Report(empty.period, 0, "completed_orders", 0, 0, 0, 0, "unknown")),
        ("unknown_cause", limited, Report(limited.period, 6200, "completed_orders", 4, 1, 2500, 3, "delivery_delay")),
    )


def run_demo() -> dict:
    cases = []
    for name, evidence, scripted_draft in demo_cases():
        draft, reviewed = simulate_handoff(evidence, lambda _: scripted_draft)
        cases.append({
            "name": name,
            "evidence": asdict(evidence),
            "draft": asdict(draft),
            "issues": list(reviewed.issues),
            "reviewed": asdict(reviewed.final),
            "baseline_pass": not checks(evidence, draft),
            "reviewed_pass": not checks(evidence, reviewed.final),
        })
    return {
        "mode": "scripted_offline",
        "real_model_calls": 0,
        "total_cases": len(cases),
        "baseline_failed_cases": sum(not case["baseline_pass"] for case in cases),
        "reviewed_failed_cases": sum(not case["reviewed_pass"] for case in cases),
        "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
