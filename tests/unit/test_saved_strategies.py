"""策略库：保存的配置必须能原样重放，删掉的就是删掉了。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from selector_app.market_data.models import DataStoreStatus
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_history import ASSET_TABLES
from selector_app.web.routers.strategies import _freeze_dates, _strategy_summary
from selector_app.web.schemas import PortfolioBacktestRequest

_REQUEST = {
    "universe": "all",
    "ranking_value": "indicator_one.wash",
    "rank_order": "desc",
    "selected_signals": ["indicator_one.main_force_entry"],
    "stop_loss_pct": 0.1,
    "max_positions": 5,
}


def _make_request(**overrides: object) -> PortfolioBacktestRequest:
    payload = {**_REQUEST, **overrides}
    return PortfolioBacktestRequest.model_validate(payload)


def _status() -> DataStoreStatus:
    return DataStoreStatus(
        database_path="/tmp/market.duckdb",
        schema_version=14,
        instrument_count=10,
        bar_count=100,
        data_start="2024-01-02",
        data_end="2026-09-18",
    )


def test_strategy_round_trip_keeps_the_replayable_request(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    request = {"universe": "all", "ranking_value": "indicator_one.wash"}

    store.save_strategy(
        "strategy-1",
        name="主力进场轮动",
        note="五槽位 + 10% 止损",
        request=request,
        summary={"performance": {"annual_return": 0.21}},
        source_history_id="history-1",
    )

    record = store.read_strategy("strategy-1")
    assert record is not None
    assert record["name"] == "主力进场轮动"
    assert record["note"] == "五槽位 + 10% 止损"
    assert record["source_history_id"] == "history-1"
    assert record["request"] == request
    assert record["summary"]["performance"]["annual_return"] == 0.21
    # 列表不带 request：列表页一次读全部策略，带着它等于把整份配置反复反序列化。
    assert "request" not in store.list_strategies()[0]


def test_saving_twice_updates_instead_of_duplicating(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy("strategy-1", name="旧名字", request={"a": 1}, summary={})
    store.save_strategy("strategy-1", name="新名字", request={"a": 2}, summary={})

    records = store.list_strategies()
    assert len(records) == 1
    assert records[0]["name"] == "新名字"
    assert store.read_strategy("strategy-1")["request"] == {"a": 2}


def test_duplicate_names_are_rejected(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy("strategy-1", name="重名策略", request={}, summary={})

    with pytest.raises(ValueError, match="已存在同名策略"):
        store.save_strategy("strategy-2", name="重名策略", request={}, summary={})
    # 自己改名成自己不算冲突。
    store.save_strategy("strategy-1", name="重名策略", request={}, summary={"b": 1})


def test_blank_and_oversized_names_are_rejected(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    with pytest.raises(ValueError, match="策略名称不能为空"):
        store.save_strategy("strategy-1", name="   ", request={}, summary={})
    with pytest.raises(ValueError, match="不能超过 60"):
        store.save_strategy("strategy-2", name="x" * 61, request={}, summary={})
    with pytest.raises(ValueError, match="不能超过 500"):
        store.save_strategy("strategy-3", name="ok", note="y" * 501, request={}, summary={})


def test_update_touches_metadata_only(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy(
        "strategy-1",
        name="原名",
        note="原备注",
        request={"ranking_value": "indicator_one.wash"},
        summary={},
    )

    updated = store.update_strategy("strategy-1", name="新名")

    assert updated is not None
    assert updated["name"] == "新名"
    # 备注没传就不动，配置更不能被改名顺手改掉。
    assert updated["note"] == "原备注"
    assert updated["request"] == {"ranking_value": "indicator_one.wash"}


def test_update_rejects_duplicate_names(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy("strategy-1", name="甲", request={}, summary={})
    store.save_strategy("strategy-2", name="乙", request={}, summary={})

    with pytest.raises(ValueError, match="已存在同名策略"):
        store.update_strategy("strategy-2", name="甲")


def test_update_and_delete_report_missing_records(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    assert store.update_strategy("不存在的策略", name="新名") is None
    assert store.delete_strategy("不存在的策略") is False
    assert store.delete_strategy("  ") is False


def test_delete_removes_only_the_target(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy("strategy-1", name="甲", request={}, summary={})
    store.save_strategy("strategy-2", name="乙", request={}, summary={})

    assert store.delete_strategy("strategy-1") is True
    assert [row["name"] for row in store.list_strategies()] == ["乙"]


def test_list_orders_by_recent_update(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, name in enumerate(("甲", "乙", "丙")):
        store.save_strategy(
            f"strategy-{index}",
            name=name,
            request={},
            summary={},
            created_at=base.replace(year=2026, month=1, day=index + 1),
        )

    assert [row["name"] for row in store.list_strategies()] == ["丙", "乙", "甲"]


def test_saved_strategies_travels_with_the_user_asset_backup() -> None:
    """策略是用户研究成果，导出备份必须带上它。"""

    assert "saved_strategies" in ASSET_TABLES


def test_summary_trimming_drops_the_bulk_series() -> None:
    """equity_curve / trades 动辄上万行，不能复制进策略表。"""

    trimmed = _strategy_summary(
        {
            "start_date": "2025-01-02",
            "end_date": "2026-01-02",
            "bars": 240,
            "max_positions": 5,
            "performance": {
                "annual_return": 0.21,
                "max_drawdown": -0.12,
                "sharpe": 1.4,
                "noise": "not-a-number",
            },
            "benchmark": {
                "performance": {"annual_return": 0.09},
                "dates": [20250102, 20250103],
                "totals": [1_000_000.0, 1_010_000.0],
            },
            "equity_curve": [{"date": 20250102, "total": 1.0}] * 240,
            "trades": [{"direction": "BUY"}] * 400,
        }
    )

    assert "equity_curve" not in trimmed
    assert "trades" not in trimmed
    assert trimmed["bars"] == 240
    assert trimmed["performance"] == {
        "annual_return": 0.21,
        "max_drawdown": -0.12,
        "sharpe": 1.4,
    }
    # 基准只留绩效，曲线是画在详情页的，列表页不需要。
    assert trimmed["benchmark_performance"] == {"annual_return": 0.09}
    assert "dates" not in trimmed


def test_relative_date_ranges_are_frozen_on_save() -> None:
    """保存时把「近一年」换成绝对日期，否则三个月后的重算会悄悄换样本。"""

    frozen = _freeze_dates(_make_request(date_mode="recent5"), _status())

    assert frozen.date_mode == "custom"
    assert frozen.start_date is not None
    assert frozen.end_date == date(2026, 9, 18)
    assert frozen.start_date < frozen.end_date


def test_explicit_dates_are_kept_and_marked_custom() -> None:
    frozen = _freeze_dates(
        _make_request(date_mode="recent5", start_date=date(2024, 3, 1), end_date=date(2024, 9, 1)),
        _status(),
    )

    assert frozen.date_mode == "custom"
    assert frozen.start_date == date(2024, 3, 1)
    assert frozen.end_date == date(2024, 9, 1)


def test_strategy_request_must_be_replayable() -> None:
    """存不进去的配置不能等到追踪那天才炸。"""

    with pytest.raises(ValueError):
        _make_request(ranking_value="不存在的指标")
