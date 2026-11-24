"""Offline benchmark comparing serial vs asyncio.gather parallel period-comparison workflow.

Makes 0 real model or network calls; simulates realistic per-endpoint Java+MySQL latency.
"""
import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import statistics
import time

from app.tools import report_utils
from app.workflow import period_diagnosis as pd

# Simulated per-endpoint latency (seconds) reflecting local Java + MySQL aggregate queries.
_LATENCIES_SEC = {
    'current_turnover': 0.095,
    'current_orders': 0.110,
    'current_ranking': 0.125,
    'previous_turnover': 0.095,
    'previous_orders': 0.110,
}


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


async def _run_benchmark(rounds: int = 5) -> dict:
    orig_today_pd = pd.today
    orig_today_ru = report_utils.today
    orig_turnover = pd.get_turnover_statistics
    orig_orders = pd.get_order_statistics
    orig_top = pd.get_top_dishes

    fixed_today = date(2026, 8, 20)

    async def sim_turnover(begin: str, end: str) -> dict:
        key = 'current_turnover' if begin == '2026-08-08' else 'previous_turnover'
        await asyncio.sleep(_LATENCIES_SEC[key])
        amount = 880.5 if begin == '2026-08-08' else 620.0
        return {'begin': begin, 'end': end, 'daily': [{'date': begin, 'turnover': amount}]}

    async def sim_orders(begin: str, end: str) -> dict:
        key = 'current_orders' if begin == '2026-08-08' else 'previous_orders'
        await asyncio.sleep(_LATENCIES_SEC[key])
        if begin == '2026-08-08':
            return {'begin': begin, 'end': end, 'total_orders': 40, 'valid_orders': 36, 'completion_rate': 0.9}
        return {'begin': begin, 'end': end, 'total_orders': 30, 'valid_orders': 24, 'completion_rate': 0.8}

    async def sim_top(begin: str, end: str, limit: int = 10) -> dict:
        await asyncio.sleep(_LATENCIES_SEC['current_ranking'])
        return {'begin': begin, 'end': end, 'ranking': [
            {'name': '招牌酸菜鱼', 'sales': 18},
            {'name': '宫保鸡丁', 'sales': 12},
        ]}

    pd.today = lambda: fixed_today
    report_utils.today = lambda: fixed_today
    pd.get_turnover_statistics = sim_turnover
    pd.get_order_statistics = sim_orders
    pd.get_top_dishes = sim_top

    serial_runs_ms: list[float] = []
    parallel_runs_ms: list[float] = []
    sample_metrics: dict = {}

    try:
        for _ in range(rounds):
            t0 = time.monotonic()
            await sim_turnover('2026-08-08', '2026-08-14')
            await sim_orders('2026-08-08', '2026-08-14')
            await sim_top('2026-08-08', '2026-08-14', limit=10)
            await sim_turnover('2026-08-01', '2026-08-07')
            await sim_orders('2026-08-01', '2026-08-07')
            serial_runs_ms.append(round((time.monotonic() - t0) * 1000, 1))

            t1 = time.monotonic()
            report = await pd.get_period_comparison_report('2026-08-08', '2026-08-14', top_limit=10)
            parallel_runs_ms.append(round((time.monotonic() - t1) * 1000, 1))
            sample_metrics = report['execution_metrics']
    finally:
        pd.today = orig_today_pd
        report_utils.today = orig_today_ru
        pd.get_turnover_statistics = orig_turnover
        pd.get_order_statistics = orig_orders
        pd.get_top_dishes = orig_top

    serial_mean = round(statistics.mean(serial_runs_ms), 1)
    parallel_mean = round(statistics.mean(parallel_runs_ms), 1)
    speedup = round(serial_mean / parallel_mean, 2) if parallel_mean > 0 else 1.0
    reduction_pct = round((1.0 - parallel_mean / serial_mean) * 100.0, 1) if serial_mean > 0 else 0.0

    return {
        'benchmark': 'parallel_period_comparison_workflow',
        'mode': 'offline_simulated_io',
        'real_model_calls': 0,
        'rounds': rounds,
        'simulated_endpoint_latencies_ms': {k: round(v * 1000, 1) for k, v in _LATENCIES_SEC.items()},
        'serial_baseline': {
            'runs_ms': serial_runs_ms,
            'mean_ms': serial_mean,
            'p50_ms': _percentile(serial_runs_ms, 50),
            'p95_ms': _percentile(serial_runs_ms, 95),
        },
        'parallel_gather': {
            'runs_ms': parallel_runs_ms,
            'mean_ms': parallel_mean,
            'p50_ms': _percentile(parallel_runs_ms, 50),
            'p95_ms': _percentile(parallel_runs_ms, 95),
        },
        'speedup_ratio': speedup,
        'latency_reduction_pct': reduction_pct,
        'sample_execution_metrics': sample_metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Offline serial vs parallel workflow latency benchmark')
    parser.add_argument('--rounds', type=int, default=5, help='Number of benchmark rounds (default: 5)')
    parser.add_argument('--output', type=str, default='', help='Optional JSON output path')
    args = parser.parse_args()

    result = asyncio.run(_run_benchmark(rounds=max(1, args.rounds)))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()
