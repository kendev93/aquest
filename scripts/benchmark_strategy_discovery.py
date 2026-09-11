#!/usr/bin/env python3
"""Run a deterministic strategy-discovery performance matrix.

The benchmark deliberately uses a fixed synthetic data fingerprint and a
deterministic evaluator.  It measures the search/dispatch machinery without
requiring a user's DuckDB or changing local market data.  Use the real
experiment endpoint separately for a market-data benchmark.

Examples::

    python scripts/benchmark_strategy_discovery.py --trials 100,500,1000
    python scripts/benchmark_strategy_discovery.py --trials 100 --workers 1,4,8
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from selector_app.screening.models import ScreenCondition  # noqa: E402
from selector_app.screening.rules import RuleGroup  # noqa: E402
from selector_app.strategy_discovery.models import (  # noqa: E402
    DiscoveryRequest,
    StrategyCandidate,
)
from selector_app.strategy_discovery.search import HybridSearchEngine  # noqa: E402

FIXED_DATASET_FINGERPRINT = "strategy-discovery-benchmark-v1"
DuplicateMode = Literal["low", "high"]
TradeMode = Literal["trade", "no_trade"]


@dataclass
class BenchmarkCache:
    values: dict[str, dict[str, object]]
    hits: int = 0
    misses: int = 0


def _request(trials: int) -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={
            "vipdoc_path": "<synthetic>",
            "universe": "all",
            "max_positions": 5,
            "initial_cash": 1_000_000.0,
            "commission": 0.0001,
            "min_commission": 0.1,
            "stamp_tax": 0.001,
            "slippage": 0.0,
            "execution": "next_open",
        },
        splits={
            "search": {"start_date": "2020-01-01", "end_date": "2023-12-31"},
            "validation": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
            "holdout": {"start_date": "2025-01-01", "end_date": "2025-12-31"},
        },
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=1,
        max_trials=trials,
        validation_candidates=10,
        holdout_candidates=5,
        min_trades=0,
        max_drawdown=None,
        max_complexity=12,
        multifidelity_enabled=False,
        convergence_enabled=False,
        seed=20260911,
    )


def _candidate_builder(
    duplicate_mode: DuplicateMode,
) -> Callable[[DiscoveryRequest, object], StrategyCandidate]:
    sequence = count()

    def builder(request: DiscoveryRequest, _rng: object) -> StrategyCandidate:
        index = next(sequence) if duplicate_mode == "low" else 0
        threshold = float(index if duplicate_mode == "low" else 0)
        return StrategyCandidate(
            buy_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="gt",
                        right_constant=threshold,
                    ),
                ),
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="lt",
                        right_constant=-1.0,
                    ),
                ),
            ),
            ranking_value="technical.ma5",
            rank_order="desc",
            max_positions=5,
            base_config=request.base_config,
        )

    return builder


def _metrics(candidate: StrategyCandidate, trade_mode: TradeMode) -> dict[str, object]:
    if trade_mode == "no_trade":
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "total_trades": 0,
            "win_rate": None,
            "volatility": 0.0,
            "data_fingerprint": FIXED_DATASET_FINGERPRINT,
            "preflight_skipped": True,
        }
    digest = hashlib.sha256(candidate.candidate_hash.encode("ascii")).digest()
    score = int.from_bytes(digest[:8], "big") / float(2**64)
    return {
        "total_return": score,
        "annual_return": score / 4.0,
        "sharpe": score * 2.0,
        "max_drawdown": 0.05 + score * 0.1,
        "total_trades": 10,
        "win_rate": 0.5,
        "volatility": 0.2,
        "data_fingerprint": FIXED_DATASET_FINGERPRINT,
        "preflight_skipped": False,
    }


def _run_case(
    trials: int,
    workers: int,
    duplicate_mode: DuplicateMode,
    trade_mode: TradeMode,
    cache: BenchmarkCache,
) -> dict[str, object]:
    request = _request(trials)
    builder = _candidate_builder(duplicate_mode)

    def evaluate(candidate: StrategyCandidate, _stage: str) -> dict[str, object]:
        cached = cache.values.get(candidate.candidate_hash)
        if cached is not None:
            cache.hits += 1
            return dict(cached)
        cache.misses += 1
        result = _metrics(candidate, trade_mode)
        cache.values[candidate.candidate_hash] = result
        return dict(result)

    def batch_evaluate(
        candidates: Iterable[StrategyCandidate], stage: str
    ) -> list[dict[str, object]]:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(lambda item: evaluate(item, stage), candidates))

    started = time.perf_counter()
    result = HybridSearchEngine().search(
        request,
        candidate_builder=builder,
        evaluator=evaluate,
        batch_evaluator=batch_evaluate,
        batch_size=min(8, workers),
        evaluation_workers=workers,
        evaluation_mode="thread_pool",
        max_trials=trials,
        seed=request.seed,
        validation_limit=request.validation_candidates,
        holdout_limit=request.holdout_candidates,
        convergence_enabled=False,
    )
    metadata = dict(result.search_metadata)
    stage_durations = metadata.get("stage_durations_seconds")
    return {
        "trials": trials,
        "workers": workers,
        "duplicate_mode": duplicate_mode,
        "trade_mode": trade_mode,
        "dataset_fingerprint": FIXED_DATASET_FINGERPRINT,
        "wall_seconds": time.perf_counter() - started,
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "requested_trials": metadata.get("requested_trials"),
        "evaluated_trials": metadata.get("evaluated_trials"),
        "unique_evaluated_trials": metadata.get("unique_evaluated_trials"),
        "duplicate_trials": metadata.get("duplicate_trials"),
        "proposal_attempts": metadata.get("proposal_attempts"),
        "stop_reason": metadata.get("stop_reason"),
        "search_stage_seconds": (
            stage_durations.get("search", 0.0) if isinstance(stage_durations, Mapping) else 0.0
        ),
    }


def _parse_ints(value: str, *, allowed: set[int]) -> list[int]:
    parsed = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not parsed or any(item not in allowed for item in parsed):
        allowed_text = ", ".join(str(item) for item in sorted(allowed))
        raise argparse.ArgumentTypeError(f"只允许: {allowed_text}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trials",
        default="100,500,1000",
        help="候选预算，逗号分隔（默认 100,500,1000）",
    )
    parser.add_argument(
        "--workers",
        default="1,4,8",
        help="评估并发数，逗号分隔（默认 1,4,8）",
    )
    parser.add_argument("--output", help="可选 JSON 输出文件；不传则输出到 stdout")
    args = parser.parse_args()
    trials = _parse_ints(args.trials, allowed={100, 500, 1000})
    workers = _parse_ints(args.workers, allowed={1, 4, 8})
    os.environ.setdefault("INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK", "1")

    rows: list[dict[str, object]] = []
    for trial_limit in trials:
        for worker_count in workers:
            for duplicate_mode in ("low", "high"):
                for trade_mode in ("trade", "no_trade"):
                    cold_cache = BenchmarkCache({})
                    rows.append(
                        {
                            "cache_mode": "cold",
                            **_run_case(
                                trial_limit,
                                worker_count,
                                duplicate_mode,
                                trade_mode,
                                cold_cache,
                            ),
                        }
                    )
                    warm_cache = BenchmarkCache(dict(cold_cache.values))
                    rows.append(
                        {
                            "cache_mode": "warm",
                            **_run_case(
                                trial_limit,
                                worker_count,
                                duplicate_mode,
                                trade_mode,
                                warm_cache,
                            ),
                        }
                    )
    payload = {
        "benchmark": "strategy_discovery",
        "dataset_fingerprint": FIXED_DATASET_FINGERPRINT,
        "seed": 20260911,
        "rows": rows,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(f"{encoded}\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
