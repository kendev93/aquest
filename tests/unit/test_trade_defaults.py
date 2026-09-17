from __future__ import annotations

from selector_app.backtest.engine import SignalBacktestConfig
from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.profiles import profile_for
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig


def test_trade_profiles_use_the_new_project_default_costs() -> None:
    stock = profile_for("stock")
    assert stock.commission == 0.0001
    assert stock.min_commission == 0.1
    for instrument_type in ("fund", "index", "bond"):
        profile = profile_for(instrument_type)
        assert profile.commission == 0.00005
        assert profile.min_commission == 0.1


def test_backtest_dataclass_defaults_use_the_new_costs() -> None:
    assert BacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert BacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
    assert PortfolioBacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert PortfolioBacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
    assert SignalBacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert SignalBacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
