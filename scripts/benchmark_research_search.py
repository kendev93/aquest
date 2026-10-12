"""Offline comparison; read-only database copy, no pool/job writes."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from aquest.backtest.service import BacktestService
from aquest.individual_research.models import IndividualResearchConfig
from aquest.individual_research.service import IndividualStockResearchService, _stable_seed
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.strategy_discovery.catalog import default_indicator_catalog
from aquest.strategy_discovery.service import StrategyDiscoveryService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--symbols", nargs=6, required=True, help="Six symbols, e.g. SH600000")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = DuckDbMarketDataStore(args.database, read_only=True)
    adapter = DuckDbMarketDataAdapter(store)
    catalog = default_indicator_catalog()
    runs: list[dict[str, object]] = []
    for symbol in args.symbols:
        ref = adapter.stock_ref("", symbol[:2], symbol[2:])
        if ref.first_date is None or ref.last_date is None:
            raise ValueError(f"No local history: {symbol}")
        base = IndividualResearchConfig(
            ref.market,
            ref.code,
            ref.name or "",
            ref.first_date,
            min(args.as_of, ref.last_date),
            tuple(catalog.value_ids),
            tuple(catalog.signal_ids),
            risk_trials=3000,
        )
        for seed in (7, 23, 71):
            for name, config in (
                ("legacy", replace(base, candidate_limit=10, search_policy="legacy")),
                ("expanded", replace(base, search_policy="legacy")),
                ("adaptive", base),
            ):
                # Fresh services avoid cross-plan cache advantages; one evaluator is held constant.
                portfolio = PortfolioBacktestService(adapter=adapter)
                discovery = StrategyDiscoveryService(
                    store=store,
                    market_data_adapter=adapter,
                    portfolio_service=portfolio,
                    evaluation_workers=1,
                    persist_indicator_cache=False,
                )
                research = IndividualStockResearchService(
                    adapter=adapter,
                    backtest_service=BacktestService(adapter=adapter),
                    portfolio_service=portfolio,
                    strategy_service=discovery,
                )
                started = time.perf_counter()
                with patch(
                    "aquest.individual_research.service._stable_seed",
                    side_effect=lambda c, p, salt=seed: (_stable_seed(c, p) + salt) % 2**32,
                ):
                    result = research.run(config)
                runs.append(
                    {
                        "symbol": symbol,
                        "seed": seed,
                        "plan": name,
                        "seconds": time.perf_counter() - started,
                        "date_range": result.get("date_range"),
                        "stage_durations_seconds": result.get("stage_durations_seconds"),
                        "risk_search_runs": result.get("risk_search_runs"),
                        "rule_candidates": result.get("rule_candidates"),
                        "results_by_return": result.get("results_by_return"),
                        "results_by_sharpe": result.get("results_by_sharpe"),
                    }
                )
                args.output.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2))
                print(symbol, seed, name, runs[-1]["seconds"], flush=True)
    medians = {
        p: statistics.median(float(r["seconds"]) for r in runs if r["plan"] == p)
        for p in ("legacy", "expanded", "adaptive")
    }
    args.output.write_text(
        json.dumps(
            {
                "runs": runs,
                "median_seconds": medians,
                "performance_passed": medians["adaptive"] <= medians["legacy"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
