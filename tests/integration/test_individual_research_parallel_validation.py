import numpy as np
import pandas as pd

from selector_app.backtest.service import BacktestService
from selector_app.indicators.cache import IndicatorCache
from selector_app.individual_research.models import IndividualResearchConfig
from selector_app.individual_research.service import IndividualStockResearchService
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget


def test_parallel_rule_validation_matches_serial_metrics_and_preserves_pair_order(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    close = 10 + np.sin(np.arange(140) / 5) + np.arange(140) / 100
    dates = pd.date_range("2020-01-01", periods=140, freq="B")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000.0,
                "amount": close * 1000,
                "bar_status": "completed",
            }
        ),
    )
    adapter = DuckDbMarketDataAdapter(store)
    cache = IndicatorCache(store)
    common = dict(
        adapter=adapter,
        backtest_service=BacktestService(adapter=adapter),
        portfolio_service=PortfolioBacktestService(adapter=adapter, indicator_cache=cache),
        strategy_service=object(),
    )
    serial = IndividualStockResearchService(**common)
    budget = EvaluationWorkerBudget(2)
    parallel = IndividualStockResearchService(
        **common,
        validation_database_path=str(store.database_path),
        validation_workers=2,
        evaluation_worker_budget=budget,
    )
    config = IndividualResearchConfig(
        market="SH",
        code="600000",
        name="test",
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        value_ids=("technical.macd",),
        signal_ids=(),
    )
    ref = adapter.stock_ref("", "SH", "600000")
    pairs = []
    for value, threshold in [("price.close", 10.5), ("technical.macd", 0.0)]:
        buy = RuleGroup(conditions=(ScreenCondition(value, "gt", right_constant=threshold),))
        sell = RuleGroup(conditions=(ScreenCondition(value, "lt", right_constant=threshold),))
        pairs.append((value, {"rule": buy}, {"rule": sell}))
    train = {"start": dates[0].date(), "end": dates[79].date()}
    validation = {"start": dates[85].date(), "end": dates[109].date()}
    expected = list(serial._rule_pair_metrics(config, ref, pairs, train, validation, None))
    with budget.stock():
        actual = list(parallel._rule_pair_metrics(config, ref, pairs, train, validation, None))
    assert actual == expected
    assert budget.available_workers == 2
