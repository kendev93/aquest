from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.condition_research.resources import ResearchResourceBudgetError
from selector_app.condition_research.service import ConditionResearchService
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.rules import RuleGroup

_PATTERN = np.array([0, 1, 2, 3, 4, 3, 2, 1, 0, -1, -2, -1], dtype=float)


def _bars(market: str, code: str, offset: float, tiles: int = 20) -> pd.DataFrame:
    close = np.tile(10 + _PATTERN + offset, tiles)
    return pd.DataFrame(
        {
            "market": market,
            "code": code,
            "instrument_type": "stock",
            "date": pd.date_range("2020-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 100_000.0,
            "amount": close * 100_000.0,
            "source": "local",
            "price_basis": "hfq",
            "bar_status": "completed",
        }
    )


def _config() -> ConditionResearchConfig:
    return ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=("BUY:CROSS(C,REF(C,1)); SELL:CROSS(REF(C,1),C); SCORE:C/MA(C,3);"),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 8, 27),
        min_trades=1,
        min_instruments=1,
        candidate_limit=3,
        holdout_candidates=1,
    )


def _store(store_path: Path) -> DuckDbMarketDataStore:
    return DuckDbMarketDataStore(store_path, read_only=True)


def test_parallel_workers_reproduce_in_process_results(tmp_path: Path) -> None:
    """回归：spawn 进程池产出的结果必须与进程内执行逐字段一致。"""

    store_path = tmp_path / "market.duckdb"
    writer = DuckDbMarketDataStore(store_path)
    writer.replace_local_bars(
        "SH", "600000", "stock", _bars("SH", "600000", 0.0), price_basis="hfq"
    )
    writer.replace_local_bars(
        "SZ", "000001", "stock", _bars("SZ", "000001", 3.0), price_basis="hfq"
    )

    in_process = ConditionResearchService(store=_store(store_path), workers=1).run(_config())
    workers = ConditionResearchService(store=_store(store_path), workers=2).run(_config())

    assert in_process["processed"] == 2
    assert in_process["candidates"], "fixture 必须能挖出候选，否则比对没有意义"
    assert workers == in_process


def test_parallel_workers_report_progress_for_every_stock(tmp_path: Path) -> None:
    """并行执行也必须走完三轮进度，总数与串行一致。"""

    store_path = tmp_path / "market.duckdb"
    writer = DuckDbMarketDataStore(store_path)
    writer.replace_local_bars(
        "SH", "600000", "stock", _bars("SH", "600000", 0.0), price_basis="hfq"
    )
    writer.replace_local_bars(
        "SZ", "000001", "stock", _bars("SZ", "000001", 3.0), price_basis="hfq"
    )
    seen: list[tuple[int, int]] = []

    result = ConditionResearchService(store=_store(store_path), workers=2).run(
        _config(), progress_callback=lambda current, total: seen.append((current, total))
    )

    assert result["processed"] == 2
    assert seen, "并行执行必须上报进度"
    assert all(total == 6 for _, total in seen)
    assert max(current for current, _ in seen) == 6


def test_unknown_adapter_stays_in_process(tmp_path: Path) -> None:
    """注入无法重建的适配器时必须退回进程内执行，而不是起 worker。"""

    class FakeAdapter:
        def list_stock_refs(
            self,
            vipdoc_path: object,
            universe: object,
            universe_file: object = None,
            instrument_types: object = None,
            boards: object = None,
        ) -> list[object]:
            return []

    service = ConditionResearchService(
        adapter=FakeAdapter(),  # type: ignore[arg-type]
        workers=4,
    )

    assert service._parallel_target is None  # noqa: SLF001


def test_service_refuses_to_start_when_memory_budget_is_too_small(tmp_path: Path) -> None:
    """预算连单进程都放不下时，必须在跑任何股票之前就报错。"""

    store_path = tmp_path / "market.duckdb"
    writer = DuckDbMarketDataStore(store_path)
    writer.replace_local_bars(
        "SH", "600000", "stock", _bars("SH", "600000", 0.0), price_basis="hfq"
    )

    service = ConditionResearchService(
        store=_store(store_path),
        workers=4,
        memory_budget_bytes=1,
    )

    with pytest.raises(ResearchResourceBudgetError, match="内存"):
        service.run(_config())
