"""Single-stock pool replay, inherited-position attribution and verified append-only resume."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from selector_app.indicators.cache import frame_fingerprint
from selector_app.market_data.models import StockRef
from selector_app.portfolio_backtest.checkpoint import SimulationCheckpoint
from selector_app.portfolio_backtest.context import _StockSeries
from selector_app.portfolio_backtest.report import _portfolio_fingerprint
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.web.schemas import PortfolioBacktestRequest

from .tracking_store import ENGINE_VERSION, dumps


def account_summary(
    checkpoint: Mapping[str, Any], launch_date: str | None, initial_cash: float
) -> dict[str, Any]:
    equity = checkpoint.get("equity", [])
    states = checkpoint.get("states", [])
    if not equity:
        raise ValueError("跟踪没有可用行情")
    boundary = launch_date or equity[-1]["date"]
    prior = [point for point in equity if point["date"] <= boundary]
    launch_equity = float(prior[-1]["total"]) if prior else initial_cash
    state: dict[str, Any] = next((row for row in reversed(states) if row["date"] <= boundary), {})
    holdings = state.get("holdings", [])
    baseline = launch_equity
    baseline_date = boundary
    inherited = bool(holdings)
    if inherited:
        entry = holdings[0]["entry_date"]
        trade = next(
            (
                row
                for row in reversed(checkpoint.get("trades", []))
                if row["date"] == entry and row["direction"] == "BUY" and not row.get("rejected")
            ),
            None,
        )
        if not trade or "equity_before" not in trade:
            raise ValueError("继承持仓缺少买入前账户净值，不能估算创建至今收益")
        baseline = float(trade["equity_before"])
        baseline_date = entry
    current = float(equity[-1]["total"])
    if baseline <= 0 or launch_equity <= 0:
        raise ValueError("跟踪收益基准无效")
    # 快照持仓只有 unrealized_pnl 金额，补算展示用的百分比字段（详情页浮动收益与
    # 跟踪投影的持仓收益都依赖它）。注意必须拷贝注入，不能原地改 checkpoint。
    latest_holdings: list[dict[str, Any]] = []
    for h in (states[-1].get("holdings", []) if states else []):
        row = dict(h)
        entry = row.get("entry_price")
        close = row.get("close")
        if isinstance(entry, (int, float)) and entry > 0 and isinstance(close, (int, float)):
            row["unrealized_pnl_pct"] = close / entry - 1.0
        latest_holdings.append(row)
    return dict(
        created_return=current / baseline - 1,
        post_launch_return=current / launch_equity - 1,
        baseline_equity=baseline,
        launch_equity=launch_equity,
        baseline_date=baseline_date,
        inherited_position=inherited,
        as_of_date=equity[-1]["date"],
        equity=current,
        holding=latest_holdings,
        has_post_launch_data=equity[-1]["date"] > boundary,
    )


def _context_prefix(context: _StockSeries, count: int, raw: pd.DataFrame) -> str:
    digest = hashlib.sha256(ENGINE_VERSION.encode())
    for field in ("dates", "opens", "highs", "lows", "closes", "atrs", "buy_mask", "sell_mask"):
        array = np.ascontiguousarray(getattr(context, field)[:count])
        digest.update(field.encode() + array.dtype.str.encode() + array.tobytes())
    for category in ("signals", "values"):
        for name, values in sorted(getattr(context, category).items()):
            array = np.ascontiguousarray(values[:count])
            digest.update((category + name).encode() + array.dtype.str.encode() + array.tobytes())
    if count:
        last = str(int(context.dates[count - 1]))
        end = pd.Timestamp(f"{last[:4]}-{last[4:6]}-{last[6:8]}")
        selected = raw.loc[pd.to_datetime(raw["date"]) <= end]
        digest.update(frame_fingerprint(selected).encode())
    return digest.hexdigest()


def replay_account(
    service: PortfolioBacktestService,
    ref: StockRef,
    version: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = version["candidate"]
    raw = service._adapter.read_stock(ref)
    if "bar_status" in raw:
        raw = raw.loc[raw["bar_status"].eq("completed")].reset_index(drop=True)
    if version.get("target_data_date"):
        raw = raw.loc[
            pd.to_datetime(raw["date"]).dt.date <= pd.Timestamp(version["target_data_date"]).date()
        ].reset_index(drop=True)
    if raw.empty:
        raise ValueError("该标的没有已完成行情")
    config_payload = dict(candidate.get("config") or {})
    config_payload.update(
        vipdoc_path="",
        universe="codes",
        codes=[f"{ref.market}:{ref.code}"],
        instrument_types=["stock"],
        boards=[],
        selected_signals=[],
        single_security_mode=True,
        ranking_value=None,
        max_positions=1,
        date_mode="custom",
        start_date=str(raw["date"].iloc[0])[:10],
        end_date=str(raw["date"].iloc[-1])[:10],
        buy_rule=candidate.get("buy_rule") or config_payload.get("buy_rule"),
        sell_rule=candidate.get("sell_rule") or config_payload.get("sell_rule"),
    )
    # Search metadata is not a trading parameter.
    fields = PortfolioBacktestRequest.model_fields
    config = PortfolioBacktestRequest.model_validate(
        {key: value for key, value in config_payload.items() if key in fields}
    ).to_config()
    service._validate_outputs(config, None)
    context = service._build_context(ref, config, None)
    if context is None:
        raise ValueError("股票没有可用于跟踪的行情")
    dates = service._global_dates([context])
    fingerprint = _portfolio_fingerprint([context])
    checkpoint = None
    mode = "initialized"
    if previous:
        mode = "rebuilt"
        try:
            saved = SimulationCheckpoint.from_dict(previous["checkpoint"])
            count = len(saved.processed_dates)
            if (
                previous.get("engine") == ENGINE_VERSION
                and previous.get("signature") == version["signature"]
                and count <= len(dates)
                and tuple(dates[:count]) == saved.processed_dates
                and previous.get("prefix_hash") == _context_prefix(context, count, raw)
            ):
                # Only a verified historical/indicator prefix may be rebound to an appended dataset.
                checkpoint = replace(saved, data_fingerprint=fingerprint)
                mode = "incremental"
        except (KeyError, TypeError, ValueError):
            checkpoint = None
    simulation = service._simulate(
        [context],
        dates,
        config,
        include_details=True,
        checkpoint=checkpoint,
        data_fingerprint=fingerprint,
    )
    captured = simulation["checkpoint"]
    if not isinstance(captured, SimulationCheckpoint):
        raise ValueError("跟踪没有生成可恢复账户状态")
    packed = captured.to_dict()
    summary = account_summary(packed, version.get("launch_data_date"), config.initial_cash)
    summary["checkpoint_hash"] = hashlib.sha256(dumps(packed).encode()).hexdigest()
    summary.update(
        processed_new_days=len(dates) - (len(checkpoint.processed_dates) if checkpoint else 0),
        mode=mode,
        recomputed_at=(datetime.now(timezone.utc).isoformat() if mode == "rebuilt" else None),
        signal_buy=bool(context.buy_mask[-1]),
        signal_sell=bool(context.sell_mask[-1]),
        pending_buys=len(captured.pending_buys),
        pending_sells=[
            dict(symbol=key, reason=value.reason) for key, value in captured.pending_sells.items()
        ],
    )
    summary["risk_exits"] = [
        dict(t)
        for t in captured.trades
        if t["date"] == summary["as_of_date"] and t.get("risk_trigger")
    ]
    report = service._build_report(
        config, dates, 1, 1, 0, {}, simulation, fingerprint, None, [context]
    )
    detail = report.to_dict()
    for key in ("equity_curve", "trades", "states", "ranking_events"):
        detail.pop(key, None)
    return dict(
        engine=ENGINE_VERSION,
        signature=version["signature"],
        checkpoint=packed,
        prefix_hash=_context_prefix(context, len(dates), raw),
        summary=summary,
        report=detail,
    )


def snapshot_cache_key(version: Mapping[str, Any], previous: Mapping[str, Any] | None) -> str:
    checkpoint = previous.get("checkpoint") if previous else None
    return hashlib.sha256(
        dumps([version["market"], version["code"], version["signature"], checkpoint]).encode()
    ).hexdigest()
