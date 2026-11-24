from dataclasses import replace

import pytest

from tools.report_review_demo import (
    Evidence, Report, checks, demo_cases, review_report, run_demo, simulate_handoff,
)


def test_correct_report_passes_with_exact_cents_and_basis_points():
    evidence = Evidence("week-a", 15800, 4, 2, 1)
    draft = Report("week-a", 15800, "completed_orders", 4, 2, 5000, 1, "unknown")
    assert checks(evidence, draft) == ()
    assert review_report(evidence, draft).final == draft


def test_review_catches_wrong_numbers_scope_and_unsupported_delivery_reason():
    evidence = Evidence("week-a", 15800, 4, 2, 1)
    draft = Report("week-b", 17600, "all_paid_orders", 4, 2, 7500, 2, "delivery_delay")
    result = review_report(evidence, draft)
    assert set(result.issues) == {
        "period_mismatch", "turnover_mismatch", "turnover_scope_mismatch", "rate_mismatch",
        "cancellations_mismatch", "unsupported_cause",
    }
    assert result.final == Report("week-a", 15800, "completed_orders", 4, 2, 5000, 1, "unknown")
    assert draft.completion_bps == 7500


def test_missing_cancellation_counts_and_no_orders_do_not_become_zero_rates():
    evidence = Evidence("week-empty", 0, 0, 0, None)
    draft = Report("week-empty", 0, "completed_orders", 0, 0, 0, 0, "unknown")
    result = review_report(evidence, draft)
    assert set(result.issues) == {"rate_mismatch", "cancellations_mismatch"}
    assert result.final.completion_bps is None
    assert result.final.canceled_orders is None


def test_review_rejects_inconsistent_evidence_instead_of_publishing_it():
    with pytest.raises(ValueError, match="counts"):
        review_report(Evidence("week-a", 100, 3, 2, 2), demo_cases()[0][2])


def test_completion_rate_rounds_to_nearest_basis_point():
    evidence = Evidence("week-a", 100, 3, 2, 0)
    draft = Report("week-a", 100, "completed_orders", 3, 2, 6666, 0, "unknown")
    assert review_report(evidence, draft).final.completion_bps == 6667


def test_comparison_runs_both_paths_over_each_identical_draft():
    result = run_demo()
    assert result["mode"] == "scripted_offline"
    assert result["real_model_calls"] == 0
    assert result["baseline_failed_cases"] == 3
    assert result["reviewed_failed_cases"] == 0
    assert result["total_cases"] == 4
    clean = next(case for case in result["cases"] if case["name"] == "clean")
    assert clean["draft"] == clean["reviewed"] and clean["issues"] == []


def test_handoff_calls_writer_then_reviewer_and_withholds_unverified_output():
    events = []
    evidence = Evidence("week-a", 15800, 4, 2, 1)
    bad = Report("week-a", 15800, "completed_orders", 4, 2, 7500, 1, "unknown")
    def writer(received):
        assert received is evidence
        events.append("writer")
        return bad
    def reviewer(received, draft):
        assert received is evidence and draft is bad
        events.append("reviewer")
        return replace(review_report(received, draft), final=bad)
    with pytest.raises(ValueError, match="verification"):
        simulate_handoff(evidence, writer, reviewer)
    assert events == ["writer", "reviewer"]
