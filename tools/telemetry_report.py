"""Aggregate P50/P95 latency, token FinOps, review SLA, and HITL funnel from JSONL logs.

Strictly offline (`real_model_calls: 0`) and resilient to malformed/partial JSONL lines.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
from typing import Any


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 1)
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    weight = rank - low
    interpolated = ordered[low] * (1.0 - weight) + ordered[high] * weight
    return round(float(interpolated), 1)


def _dist_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {'count': 0, 'mean': None, 'p50': None, 'p95': None, 'max': None}
    return {
        'count': len(values),
        'mean': round(float(statistics.mean(values)), 1),
        'p50': _percentile(values, 50),
        'p95': _percentile(values, 95),
        'max': round(float(max(values)), 1),
    }


def _read_jsonl(path: Path | None) -> tuple[list[dict[str, Any]], int]:
    if path is None or not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    corrupted = 0
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                rows.append(parsed)
            else:
                corrupted += 1
        except ValueError:
            corrupted += 1
    return rows, corrupted


def summarize_telemetry(
    telemetry_path: Path | None,
    approval_path: Path | None = None,
) -> dict[str, Any]:
    """Compute P50/P95 latency, token usage, tool frequency, review verdicts, and approval funnel."""
    events, corrupted_telemetry = _read_jsonl(telemetry_path)
    approvals, corrupted_approvals = _read_jsonl(approval_path)

    turns = [e for e in events if e.get('event') == 'chat_turn']
    reviews = [e for e in events if e.get('event') == 'report_review_completed']

    succeeded_turns = [t for t in turns if t.get('succeeded') is True]
    failed_turns = [t for t in turns if t.get('succeeded') is False]
    stream_turns = [t for t in turns if t.get('stream') is True]
    compacted_turns = [t for t in turns if t.get('compacted') is True]

    def _nums(items: list[dict[str, Any]], field: str) -> list[float]:
        out: list[float] = []
        for item in items:
            val = item.get(field)
            if not isinstance(val, bool) and isinstance(val, (int, float)) and math.isfinite(val) and val >= 0:
                out.append(float(val))
        return out

    tool_counter: Counter[str] = Counter()
    for t in turns:
        for tname in t.get('tool_calls') or ():
            if isinstance(tname, str):
                tool_counter[tname] += 1

    in_tokens = [int(t['input_tokens']) for t in turns if type(t.get('input_tokens')) is int]
    out_tokens = [int(t['output_tokens']) for t in turns if type(t.get('output_tokens')) is int]
    tot_tokens = [float(t['total_tokens']) for t in turns if type(t.get('total_tokens')) is int]

    by_role: dict[str, dict[str, Any]] = {}
    for role in ('merchant', 'customer'):
        role_turns = [t for t in succeeded_turns if t.get('agent_role') == role]
        by_role[role] = {
            'turns': len(role_turns),
            'ttft_ms': _dist_summary(_nums(role_turns, 'ttft_ms')),
            'total_ms': _dist_summary(_nums(role_turns, 'total_ms')),
        }

    verdict_counter: Counter[str] = Counter()
    issue_counter: Counter[str] = Counter()
    precheck_hits = 0
    for r in reviews:
        verdict = str(r.get('verdict') or 'unavailable')
        verdict_counter[verdict] += 1
        if r.get('precheck_hit') is True:
            precheck_hits += 1
        for code in r.get('issue_codes') or ():
            if isinstance(code, str):
                issue_counter[code] += 1

    approval_outcomes: Counter[str] = Counter()
    for a in approvals:
        outcome = a.get('outcome')
        if isinstance(outcome, str):
            approval_outcomes[outcome] += 1

    total_turn_count = len(turns)
    total_review_count = len(reviews)

    return {
        'report': 'sky_agent_observability_summary',
        'real_model_calls': 0,
        'corrupted_lines': corrupted_telemetry + corrupted_approvals,
        'turns': {
            'total': total_turn_count,
            'succeeded': len(succeeded_turns),
            'failed': len(failed_turns),
            'success_rate_pct': round(len(succeeded_turns) * 100.0 / total_turn_count, 1) if total_turn_count else None,
            'streaming_turns': len(stream_turns),
            'compacted_turns': len(compacted_turns),
        },
        'latency_ms': {
            'ttft': _dist_summary(_nums(succeeded_turns, 'ttft_ms')),
            'total': _dist_summary(_nums(succeeded_turns, 'total_ms')),
            'by_role': by_role,
        },
        'tokens': {
            'turns_with_usage': len(tot_tokens),
            'input_total': sum(in_tokens),
            'output_total': sum(out_tokens),
            'grand_total': int(sum(tot_tokens)),
            'per_turn': _dist_summary(tot_tokens),
        },
        'tools': dict(tool_counter.most_common()),
        'async_reviews': {
            'total': total_review_count,
            'pass': verdict_counter.get('pass', 0),
            'revise': verdict_counter.get('revise', 0),
            'unavailable': verdict_counter.get('unavailable', 0),
            'precheck_hits': precheck_hits,
            'precheck_hit_rate_pct': round(precheck_hits * 100.0 / total_review_count, 1) if total_review_count else None,
            'elapsed_ms': _dist_summary(_nums(reviews, 'elapsed_ms')),
            'issue_codes': dict(issue_counter.most_common()),
        },
        'approvals': {
            'events_total': len(approvals),
            'outcomes': dict(approval_outcomes),
        },
    }


def _build_synthetic_demo_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate a deterministic, PII-free synthetic workload for CLI verification."""
    telemetry_rows = [
        {
            'timestamp': '2026-11-24T10:00:01.000+08:00',
            'event': 'chat_turn',
            'trace_id': f'trc_demo_{i:02d}',
            'agent_role': 'merchant' if i % 4 != 0 else 'customer',
            'operator_id': 1 if i % 4 != 0 else 12,
            'session_id': f's_{i // 3}',
            'stream': True,
            'succeeded': True,
            'ttft_ms': 420.0 + i * 35.0,
            'total_ms': 1150.0 + i * 90.0,
            'tool_calls': ['get_period_comparison_report'] if i % 3 == 0 else ['get_today_sales'],
            'tool_count': 1,
            'compacted': i >= 8,
            'input_tokens': 680 + i * 25,
            'output_tokens': 210 + i * 10,
            'total_tokens': 890 + i * 35,
            'approval_created': i == 5,
            'review_submitted': i % 4 != 0,
        }
        for i in range(1, 11)
    ]
    review_rows = [
        {
            'timestamp': '2026-11-24T10:00:05.000+08:00',
            'event': 'report_review_completed',
            'trace_id': f'trc_demo_{i:02d}',
            'operator_id': 1,
            'session_id': f's_{i // 3}',
            'review_id': f'rev_{i:02d}',
            'verdict': 'revise' if i in (2, 6) else 'pass',
            'precheck_hit': i == 2,
            'issue_codes': ['overstatement'] if i in (2, 6) else [],
            'failure': None,
            'elapsed_ms': 0.2 if i == 2 else 8200.0 + i * 150.0,
        }
        for i in (1, 2, 3, 5, 6, 7, 9, 10)
    ]
    approval_rows = [
        {'event': 'approval_created', 'trace_id': 'trc_demo_05', 'outcome': 'pending'},
        {'event': 'approval_confirm', 'trace_id': 'trc_demo_05b', 'outcome': 'executed'},
    ]
    return telemetry_rows + review_rows, approval_rows


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='Summarize Sky Agent P50/P95 latency and token telemetry')
    parser.add_argument('--telemetry-path', default=str(root / 'logs' / 'telemetry.jsonl'))
    parser.add_argument('--approval-path', default=str(root / 'logs' / 'approval_audit.jsonl'))
    parser.add_argument('--synthetic-demo', action='store_true', help='Summarize a synthetic in-memory demo dataset')
    parser.add_argument('--output', default='')
    args = parser.parse_args()

    if args.synthetic_demo:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            t_path = Path(tmp) / 'telemetry.jsonl'
            a_path = Path(tmp) / 'approval_audit.jsonl'
            t_rows, a_rows = _build_synthetic_demo_rows()
            t_path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in t_rows) + '\n', encoding='utf-8')
            a_path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in a_rows) + '\n', encoding='utf-8')
            summary = summarize_telemetry(t_path, a_path)
    else:
        summary = summarize_telemetry(Path(args.telemetry_path), Path(args.approval_path))

    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()
