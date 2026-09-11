from selector_app.portfolio_backtest.context import PortfolioContextMixin
from selector_app.portfolio_backtest.report import PortfolioReportMixin
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.portfolio_backtest.simulation import PortfolioSimulationMixin


def test_portfolio_service_is_split_into_domain_mixins() -> None:
    service = PortfolioBacktestService()

    assert isinstance(service, PortfolioContextMixin)
    assert isinstance(service, PortfolioSimulationMixin)
    assert isinstance(service, PortfolioReportMixin)
