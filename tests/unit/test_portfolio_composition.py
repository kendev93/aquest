from aquest.portfolio_backtest.context import PortfolioContextMixin
from aquest.portfolio_backtest.report import PortfolioReportMixin
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.portfolio_backtest.simulation import PortfolioSimulationMixin


def test_portfolio_service_is_split_into_domain_mixins() -> None:
    service = PortfolioBacktestService()

    assert isinstance(service, PortfolioContextMixin)
    assert isinstance(service, PortfolioSimulationMixin)
    assert isinstance(service, PortfolioReportMixin)
