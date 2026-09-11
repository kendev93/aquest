from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_discovery.replay_service import REPLAY_EVALUATION_WORKERS
from selector_app.strategy_discovery.service import StrategyDiscoveryService


def _bars() -> pd.DataFrame:
    close = np.asarray([10.0, 11.0, 12.0])
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3, freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": [1000.0] * 3,
            "amount": list(close * 1000),
            "bar_status": ["completed"] * 3,
        }
    )


def _candidate(index: int) -> dict[str, object]:
    return {
        "candidate_hash": f"candidate-{index}",
        "human_readable_rules": f"候选 {index}",
        "config": {
            "vipdoc_path": "",
            "universe": "all",
            "universe_file": None,
            "selected_signals": [],
            "combine_mode": "all",
            "minimum_matches": None,
            "buy_rule": {
                "signals": [],
                "conditions": [
                    {
                        "left_value": "technical.ma5",
                        "operator": "gt",
                        "right_value": None,
                        "right_constant": 1.0,
                    }
                ],
                "combine_mode": "all",
                "minimum_matches": None,
            },
            "ranking_value": "technical.ma5",
            "rank_order": "desc",
            "max_positions": 1,
            "rebalance_frequency": "daily",
            "sell_signal": None,
            "sell_rule": {
                "signals": [],
                "conditions": [
                    {
                        "left_value": "technical.ma5",
                        "operator": "lt",
                        "right_value": None,
                        "right_constant": 100.0,
                    }
                ],
                "combine_mode": "all",
                "minimum_matches": None,
            },
            "start_date": "2020-01-01",
            "end_date": "2020-01-02",
        },
        "metrics": {"validation": {"annual_return": 0.2, "sharpe": 1.2}},
    }


class FakePortfolioService:
    def __init__(self) -> None:
        self.configs = []

    def run(self, config):
        self.configs.append(config)
        return {
            "universe": config.universe,
            "total_candidates": 1,
            "processed": 1,
            "skipped": 0,
            "errors": 0,
            "bars": 3,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "max_positions": config.max_positions,
            "ranking_value": config.ranking_value,
            "rank_order": config.rank_order,
            "performance": {
                "total_return": 0.2,
                "annual_return": 0.3,
                "max_drawdown": 0.05,
                "sharpe": 1.4,
                "total_trades": 1.0,
                "win_rate": 1.0,
            },
            "equity_curve": [
                {"date": "2024-01-01", "total": 100.0},
                {"date": "2024-01-02", "total": 110.0},
                {"date": "2024-01-03", "total": 120.0},
            ],
            "trades": [],
            "states": [],
            "ranking_events": [],
            "failure_reasons": {},
            "diagnostic": None,
            "data_fingerprint": "replay-fingerprint",
        }


def test_replay_selects_ten_candidates_and_extends_to_latest_local_date(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    portfolio = FakePortfolioService()
    service = StrategyDiscoveryService(portfolio_service=portfolio, store=store)
    experiment = {
        "history_id": "experiment-1",
        "request": {
            "base_config": {
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
                "max_positions": 1,
            }
        },
        "data_fingerprint": "source-fingerprint",
        "results_by_sharpe": [_candidate(index) for index in range(12)],
        "results_by_return": [],
    }
    progress = []

    result = service.replay(
        experiment,
        "sharpe",
        progress_callback=lambda current, total, phase, metadata: progress.append(
            (current, total, phase, metadata)
        ),
    )

    assert result["candidate_count"] == 10
    assert len(result["candidates"]) == 10
    assert len(portfolio.configs) == 10
    assert all(config.end_date.isoformat() == "2024-01-03" for config in portfolio.configs)
    assert result["data_end"] == "2024-01-03"
    assert result["data_changed"] is True
    assert progress[-1][0:3] == (11, 11, "replaying")


def test_real_replay_uses_the_shared_snapshot_process_pool(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    close = np.linspace(10.0, 13.0, len(dates))
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(len(dates), 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", frame)
    strategy_config = {
        "vipdoc_path": str(tmp_path),
        "universe": "all",
        "universe_file": None,
        "instrument_types": [],
        "boards": [],
        "selected_signals": [],
        "combine_mode": "all",
        "minimum_matches": None,
        "buy_rule": {
            "signals": [],
            "conditions": [
                {
                    "left_value": "technical.ma5",
                    "operator": "gt",
                    "right_value": None,
                    "right_constant": 1.0,
                }
            ],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "ranking_value": "technical.ma5",
        "rank_order": "desc",
        "max_positions": 1,
        "rebalance_frequency": "daily",
        "sell_signal": None,
        "sell_rule": {
            "signals": [],
            "conditions": [
                {
                    "left_value": "technical.ma5",
                    "operator": "lt",
                    "right_value": None,
                    "right_constant": 100.0,
                }
            ],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "start_date": "2024-01-01",
        "end_date": "2024-01-30",
        "initial_cash": 100_000.0,
        "commission": 0.0001,
        "min_commission": 0.1,
        "stamp_tax": 0.001,
        "slippage": 0.0,
        "execution": "next_open",
    }
    progress = []
    result = StrategyDiscoveryService(store=store).replay(
        {
            "history_id": "experiment-1",
            "request": {"base_config": strategy_config},
            "data_fingerprint": "source-fingerprint",
            "results_by_sharpe": [
                {
                    "candidate_hash": "candidate-1",
                    "human_readable_rules": "测试策略",
                    "config": strategy_config,
                    "metrics": {"validation": {"annual_return": 0.1, "sharpe": 1.0}},
                }
            ],
            "results_by_return": [],
        },
        "sharpe",
        progress_callback=lambda current, total, phase, metadata: progress.append(
            (current, total, phase, metadata)
        ),
    )

    assert REPLAY_EVALUATION_WORKERS == 8
    assert result["candidate_count"] == 1
    assert len(result["data_fingerprint"]) == 64
    assert any(event[2] == "preparing_snapshot" for event in progress)
    assert progress[-1][0:3] == (2, 2, "replaying")
