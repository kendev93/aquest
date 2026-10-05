"""Project-owned performance metrics for deterministic historical simulations."""

from __future__ import annotations

from collections import deque
from datetime import date, datetime

import numpy as np
import pandas as pd


class PerformanceAnalyzer:
    """Calculate stable metrics from a normalized equity and trade table."""

    ANNUAL_DAYS = 252

    def __init__(
        self,
        equity_curve: pd.DataFrame,
        trades: pd.DataFrame,
        risk_free_rate: float = 0.03,
    ) -> None:
        self._equity_curve = equity_curve
        self._trades = trades
        self._risk_free_rate = risk_free_rate
        self.diagnostic: str | None = None

    def compute(self) -> dict[str, float]:
        if self._equity_curve.empty or "total" not in self._equity_curve:
            self.diagnostic = "资金曲线为空，无法计算绩效"
            return self._empty_metrics()
        total = self._equity_curve["total"].to_numpy(dtype=float)
        if len(total) < 2:
            self.diagnostic = "资金曲线不足 2 根，无法计算绩效"
            metrics = self._empty_metrics()
            metrics["start_cash"] = float(total[0]) if len(total) else 0.0
            metrics["end_value"] = float(total[-1]) if len(total) else 0.0
            return metrics

        safe_previous = np.where(total[:-1] != 0, total[:-1], np.nan)
        daily_returns = np.diff(total) / safe_previous
        daily_returns = daily_returns[np.isfinite(daily_returns)]
        if len(daily_returns) == 0:
            self.diagnostic = "没有有效日收益率，无法计算绩效"
            metrics = self._empty_metrics()
            metrics["start_cash"] = float(total[0])
            metrics["end_value"] = float(total[-1])
            return metrics

        total_return = float(total[-1] / total[0] - 1) if total[0] != 0 else 0.0
        annual_return = float((1 + total_return) ** (self.ANNUAL_DAYS / len(daily_returns)) - 1)
        drawdown, drawdown_pct = self._drawdown_arrays(total)
        max_drawdown = float(np.max(drawdown_pct))
        # 回撤时长要描述的是「最大回撤那一段」，所以定位必须用与 max_drawdown
        # 同一个口径（百分比），否则小资金深回撤 + 大资金浅回撤时，报出来的幅度
        # 和时长不是同一次回撤。
        max_dd_duration = self._max_drawdown_duration(total, drawdown_pct)
        rf_daily = self._risk_free_rate / self.ANNUAL_DAYS
        excess = daily_returns - rf_daily
        daily_std = float(np.std(daily_returns))
        sharpe = (
            float(np.mean(excess) / daily_std * np.sqrt(self.ANNUAL_DAYS)) if daily_std else 0.0
        )
        # 下行差按全样本取二阶矩。只对负收益子集求 std 会丢掉非负观测的计数权重，
        # 分母被缩小 ⇒ Sortino 系统性虚高；样本里只有一笔负收益时 std 还是 0。
        downside = float(np.sqrt(np.mean(np.minimum(excess, 0.0) ** 2)))
        if downside:
            sortino = float(np.mean(excess) / downside * np.sqrt(self.ANNUAL_DAYS))
        elif float(np.mean(excess)) > 0:
            sortino = 999.0
        else:
            sortino = 0.0
        calmar = (
            annual_return / max_drawdown
            if max_drawdown > 1e-10
            else (999.0 if annual_return > 0 else 0.0)
        )

        trades = self._trades
        if trades.empty or "direction" not in trades:
            sell_trades = trades.iloc[0:0]
        else:
            sell_trades = trades.loc[trades["direction"] == "SELL"]
        pnl = (
            sell_trades["pnl"].to_numpy(dtype=float)
            if "pnl" in sell_trades
            else np.zeros(len(sell_trades))
        )
        wins = pnl > 0
        # 保本单（费用与价差刚好抵消）既不是盈利也不是亏损，用 ``~wins`` 会把它们
        # 算成亏损并把 0 塞进 loss_returns，压低胜率、拉平平均亏损。
        losses = pnl < 0
        win_pnl = pnl[wins]
        loss_pnl = pnl[losses]
        profit_factor = (
            float(win_pnl.sum() / abs(loss_pnl.sum()))
            if len(win_pnl) and len(loss_pnl) and loss_pnl.sum() != 0
            else 999.0
            if len(win_pnl) and not len(loss_pnl)
            else 0.0
        )
        cost_basis = (
            sell_trades["cost_basis"].to_numpy(dtype=float)
            if "cost_basis" in sell_trades
            else np.zeros(len(sell_trades))
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            trade_returns = np.where(cost_basis > 0, pnl / cost_basis, np.nan)
        win_returns = trade_returns[wins]
        loss_returns = trade_returns[losses]
        win_returns = win_returns[np.isfinite(win_returns)]
        loss_returns = loss_returns[np.isfinite(loss_returns)]
        rejected = int(trades["rejected"].astype(bool).sum()) if "rejected" in trades else 0
        metrics = {
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "max_dd_duration": float(max_dd_duration),
            "sharpe": sharpe,
            "sortino": sortino,
            "calmar": calmar,
            "total_trades": float(len(sell_trades)),
            "win_trades": float(wins.sum()),
            "lose_trades": float(losses.sum()),
            "rejected_trades": float(rejected),
            "win_rate": float(wins.sum() / len(sell_trades)) if len(sell_trades) else 0.0,
            "profit_factor": profit_factor,
            "avg_win": float(np.mean(win_returns)) if len(win_returns) else 0.0,
            "avg_loss": float(np.mean(loss_returns)) if len(loss_returns) else 0.0,
            "max_win": float(np.max(win_returns)) if len(win_returns) else 0.0,
            "max_loss": float(np.min(loss_returns)) if len(loss_returns) else 0.0,
            "avg_holding_days": self._average_holding_days(),
            "volatility": float(np.std(daily_returns) * np.sqrt(self.ANNUAL_DAYS)),
            "sharpe_ratio": sharpe,
            "start_cash": float(total[0]),
            "end_value": float(total[-1]),
        }
        return metrics

    @staticmethod
    def _drawdown_arrays(total: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """回撤序列。研究流程每次回测都要算，因此不构造 DataFrame。"""

        peak = np.maximum.accumulate(total)
        drawdown = peak - total
        drawdown_pct = np.divide(drawdown, peak, out=np.zeros_like(drawdown), where=peak != 0)
        return drawdown, drawdown_pct

    @staticmethod
    def _max_drawdown_duration(total: np.ndarray, drawdown_pct: np.ndarray) -> int:
        if not len(drawdown_pct) or float(np.max(drawdown_pct)) == 0:
            return 0
        max_index = int(np.argmax(drawdown_pct))
        # 回撤区间必须从「谷底之前的历史最高点」起算。此前用「回扫遇到的第一个高于
        # 谷底的点」近似，单调下跌时会退化成 1 天（正确值应为整段下跌长度）。
        peak_index = int(np.argmax(total[: max_index + 1]))
        return max_index - peak_index

    def _average_holding_days(self) -> float:
        if self._trades.empty or "datetime" not in self._trades:
            return 0.0
        queues: dict[str, deque[tuple[date, float]]] = {}
        weighted_days = 0.0
        total_size = 0.0
        for row in self._trades.to_dict(orient="records"):
            if bool(row.get("rejected", False)):
                continue
            current_date = _to_date(row.get("datetime"))
            if current_date is None:
                continue
            size = float(row.get("size", 0) or 0)
            if size <= 0:
                continue
            symbol = f"{row.get('market', '')}{row.get('code', '')}"
            queue = queues.setdefault(symbol, deque())
            if row.get("direction") == "BUY":
                queue.append((current_date, size))
                continue
            if row.get("direction") != "SELL":
                continue
            remaining = size
            while remaining > 0 and queue:
                buy_date, buy_size = queue[0]
                consumed = min(remaining, buy_size)
                weighted_days += max((current_date - buy_date).days, 0) * consumed
                total_size += consumed
                remaining -= consumed
                buy_size -= consumed
                if buy_size <= 0:
                    queue.popleft()
                else:
                    queue[0] = (buy_date, buy_size)
        return weighted_days / total_size if total_size else 0.0

    @staticmethod
    def _empty_metrics() -> dict[str, float]:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "max_dd_duration": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "total_trades": 0.0,
            "win_trades": 0.0,
            "lose_trades": 0.0,
            "rejected_trades": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "avg_holding_days": 0.0,
            "volatility": 0.0,
            "sharpe_ratio": 0.0,
            "start_cash": 0.0,
            "end_value": 0.0,
        }


def _to_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        text = str(value)
        if "-" in text:
            return datetime.fromisoformat(text).date()
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except (TypeError, ValueError, OverflowError):
        return None
