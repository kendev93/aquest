"""Default online source used by the full-market synchronization task."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pandas as pd

from selector_app.market_data.models import MarketCode

from .online_market_sync import (
    OnlineMarketClient,
    OnlineProviderError,
    _market_for_stock_code,
)


class OnlineFullMarketSource:
    """Expose one stable catalog/history contract for all daily asset types."""

    def __init__(self, client_factory: Callable[[], OnlineMarketClient] | None = None) -> None:
        self._client_factory = client_factory or OnlineMarketClient
        self.last_provider = "eastmoney"

    def get_stock_list(self, *, timeout: float = 30.0) -> pd.DataFrame:
        return self._client_factory().fetch_instruments(timeout=timeout)

    def get_etf_list(self, *, timeout: float = 30.0) -> pd.DataFrame:
        return self._client_factory().fetch_fund_instruments(timeout=timeout)

    def get_lof_list(self, *, timeout: float = 30.0) -> pd.DataFrame:
        return self._client_factory().fetch_lof_instruments(timeout=timeout)

    def get_index_list(self, *, timeout: float = 30.0) -> pd.DataFrame:
        return self._client_factory().fetch_index_instruments(timeout=timeout)

    def get_daily_data_for_asset(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        asset_type: str,
        *,
        adjust_type: str = "qfq",
        timeout: float = 30.0,
    ) -> pd.DataFrame:
        del adjust_type
        client = self._client_factory()
        self.last_provider = "eastmoney"
        try:
            result = client.fetch_daily_history_for_asset(
                symbol,
                asset_type,
                start_date,
                end_date,
                timeout=timeout,
            )
        except OnlineProviderError:
            result = pd.DataFrame()
        normalized_type = str(asset_type).strip().lower()
        if result.empty and normalized_type in {"stock", "股票"}:
            raw = str(symbol).strip().lower()
            market = raw[:2].upper() if raw[:2] in {"sh", "sz"} else ""
            code = raw[2:] if market else raw
            market = market or (_market_for_stock_code(code) or "")
            if market in {"SH", "SZ"} and len(code) == 6:
                result = client.fetch_tencent_daily_history(
                    cast(MarketCode, market),
                    code,
                    start_date.replace("-", ""),
                    end_date.replace("-", ""),
                    timeout=timeout,
                )
                self.last_provider = "tencent"
        return result


__all__ = ["OnlineFullMarketSource"]
