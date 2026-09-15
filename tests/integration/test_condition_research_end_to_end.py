from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.backtest.service import BacktestService
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.condition_research.service import ConditionResearchService
from selector_app.market_data.models import StockRef
from selector_app.screening.rules import RuleGroup


class FakeUniverseAdapter:
    def __init__(self) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="测试甲", instrument_type="stock"),
            StockRef("SZ", "000001", Path("/tmp/vipdoc"), name="测试乙", instrument_type="stock"),
        ]
        self.frames = {ref.code: self._frame(offset) for offset, ref in enumerate(self.refs)}

    @staticmethod
    def _frame(offset: int) -> pd.DataFrame:
        pattern = np.array([0, 1, 2, 1, 0, -1, 0, 2, 3, 2, 0, -1], dtype=float)
        close = 10 + pattern + offset
        close = np.tile(close, 4)
        return pd.DataFrame(
            {
                "date": pd.date_range("2020-01-01", periods=len(close), freq="D"),
                "open": close,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": np.full(len(close), 100_000.0),
                "amount": close * 100_000.0,
                "bar_status": "completed",
            }
        )

    def list_stock_refs(
        self,
        vipdoc_path,
        universe,
        universe_file=None,
        instrument_types=None,
        boards=None,
    ):
        del vipdoc_path, universe_file, boards
        refs = self.refs
        if universe == "sh":
            refs = [ref for ref in refs if ref.market == "SH"]
        elif universe == "sz":
            refs = [ref for ref in refs if ref.market == "SZ"]
        if instrument_types:
            refs = [ref for ref in refs if ref.instrument_type in instrument_types]
        return refs

    def stock_ref(self, vipdoc_path, market, code):
        del vipdoc_path
        return next(ref for ref in self.refs if ref.market == market and ref.code == code)

    def read_stock(self, ref):
        return self.frames[ref.code].copy(deep=True)


def test_condition_research_runs_independent_stock_baselines_and_time_splits() -> None:
    adapter = FakeUniverseAdapter()
    service = ConditionResearchService(
        adapter=adapter,
        backtest_service=BacktestService(adapter=adapter),
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=("BUY:CROSS(C,REF(C,1)); SELL:CROSS(REF(C,1),C); SCORE:C/MA(C,3);"),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 2, 18),
        min_trades=1,
        min_instruments=1,
        candidate_limit=3,
        holdout_candidates=1,
    )

    result = service.run(config)

    assert result["total_candidates"] == 2
    assert result["processed"] == 2
    assert "validation" in result["baseline"]
    assert "full_period" in result["baseline"]
    assert "holdout" in result["baseline"]
    assert result["splits"]["train"]["end_date"] < result["splits"]["validation"]["start_date"]
    assert result["splits"]["validation"]["end_date"] < result["splits"]["holdout"]["start_date"]
