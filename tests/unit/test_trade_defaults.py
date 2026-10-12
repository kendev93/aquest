from __future__ import annotations

from aquest.backtest.engine import SignalBacktestConfig
from aquest.backtest.models import BacktestConfig
from aquest.backtest.profiles import profile_for
from aquest.portfolio_backtest.models import PortfolioBacktestConfig


def test_trade_profiles_use_the_new_project_default_costs() -> None:
    stock = profile_for("stock")
    assert stock.commission == 0.0001
    assert stock.min_commission == 0.1


def test_backtest_dataclass_defaults_use_the_new_costs() -> None:
    assert BacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert BacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
    assert PortfolioBacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert PortfolioBacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
    assert SignalBacktestConfig.__dataclass_fields__["commission"].default == 0.0001
    assert SignalBacktestConfig.__dataclass_fields__["min_commission"].default == 0.1
