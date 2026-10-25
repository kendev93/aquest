"""信号扫描用哪一天：日线决定「实际有哪一天」，交易日历决定「应该到哪一天」。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.sync_session import latest_completed_session, shanghai_now


@dataclass(frozen=True)
class SignalSession:
    """一次信号扫描的日期口径。

    ``scanned`` 是实际要扫的日期，``expected`` 是交易日历判出的最近已收盘交易日。
    两者不同（``scanned`` 更早）说明最新交易日的行情还没同步完。
    """

    scanned: date | None
    expected: date | None

    @property
    def lag(self) -> dict[str, str] | None:
        """最新交易日缺行情时返回缺口，供界面说明「为什么不是那一天」。"""

        if self.expected is None or self.scanned is None or self.scanned >= self.expected:
            return None
        return {
            "expected_session": self.expected.isoformat(),
            "scanned_session": self.scanned.isoformat(),
        }


def resolve_signal_session(
    store: DuckDbMarketDataStore, *, now: datetime | None = None
) -> SignalSession:
    """只在「有完整行情、且不晚于今天」的日子里选扫描日。

    只用交易日历选，会在当天行情还没同步完时拿缺最后一根 bar 的行情去评估；只用
    ``MAX(trade_date)`` 选，未来日期或错年份导入的已完成行会把信号日推到后面。所以
    日线给出上界，日历给出「应该到哪天」的对照；日历覆盖不足时（判不出今天是不是
    交易日）退回日线口径，行为与改动前一致。
    """

    today = shanghai_now(now).date()
    with store.read_session() as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM daily_bars WHERE "
            "instrument_type='stock' AND bar_status='completed' AND trade_date <= ?",
            [today],
        ).fetchone()
    value = row[0] if row else None
    if value is None:
        return SignalSession(scanned=None, expected=None)
    scanned = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return SignalSession(scanned=scanned, expected=latest_completed_session(store, now=now))
