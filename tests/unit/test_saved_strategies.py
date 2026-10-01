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
    assert "tracking_state" not in store.list_strategies()[0]


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


def _save(
    store: DuckDbMarketDataStore,
    strategy_id: str = "strategy-1",
    name: str = "主力进场轮动",
) -> None:
    store.save_strategy(strategy_id, name=name, request=dict(_REQUEST), summary={})


def _point(trade_date: int, equity: float, **rest: object) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "equity": equity,
        "cash": 1000.0,
        "positions_count": 3,
        **rest,
    }


def test_tracking_start_seeds_the_hand_over_day(tmp_path) -> None:
    """上线日那一行是接力点：研究段到它为止，观察段从它开始。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)

    record = store.start_strategy_tracking(
        "strategy-1",
        launched_at=20260901,
        seed_points=[_point(20260901, 100000.0, positions_count=0, drawdown=0.0)],
    )

    assert record is not None
    assert record["tracking_status"] == "active"
    assert record["launched_at"] == "2026-09-01"
    assert record["last_tracked_date"] == 20260901
    assert store.list_tracking_points("strategy-1") == [
        {
            "trade_date": 20260901,
            "equity": 100000.0,
            "cash": 1000.0,
            "positions_count": 0,
            "benchmark_value": None,
            "drawdown": 0.0,
        }
    ]


def test_tracking_points_are_idempotent_across_reruns(tmp_path) -> None:
    """同一天重算几次都一样——这是幂等，也是「漏跑几天后一键补齐」的前提。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking("strategy-1", launched_at=20260901)

    store.save_tracking_points("strategy-1", [_point(20260902, 101000.0)])
    store.save_tracking_points("strategy-1", [_point(20260902, 102000.0)])

    points = store.list_tracking_points("strategy-1")
    assert [point["trade_date"] for point in points] == [20260902]
    assert points[0]["equity"] == 102000.0


def test_failed_rerun_keeps_the_old_date(tmp_path) -> None:
    """失败只能停在旧日期。静默改成"今天"会让用户以为数据已经算过了。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking("strategy-1", launched_at=20260901)
    store.finish_strategy_tracking("strategy-1", last_tracked_date=20260905)

    record = store.finish_strategy_tracking(
        "strategy-1", last_tracked_date=20260908, error="行情缺失"
    )

    assert record is not None
    assert record["last_tracked_date"] == 20260905
    assert record["last_error"] == "行情缺失"
    # 失败之后成功一次，错误必须被清掉，否则它会一直挂着吓人。
    recovered = store.finish_strategy_tracking("strategy-1", last_tracked_date=20260908)
    assert recovered is not None
    assert recovered["last_tracked_date"] == 20260908
    assert recovered["last_error"] == ""


def test_stop_tracking_keeps_observations(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking(
        "strategy-1",
        launched_at=20260901,
        seed_points=[_point(20260901, 100000.0)],
    )

    record = store.stop_strategy_tracking("strategy-1")

    assert record is not None
    assert record["tracking_status"] == "off"
    # 观察值是已经跑出来的事实，停了也不该抹掉。
    assert len(store.list_tracking_points("strategy-1")) == 1


def test_deleting_a_strategy_removes_its_observations(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking(
        "strategy-1", launched_at=20260901, seed_points=[_point(20260901, 1.0)]
    )

    assert store.delete_strategy("strategy-1") is True
    assert store.list_tracking_points("strategy-1") == []


def test_list_tracked_strategies_only_returns_online_ones(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store, "strategy-1", "上线策略")
    _save(store, "strategy-2", "仅保存")
    store.start_strategy_tracking("strategy-1", launched_at=20260901)

    assert [row["strategy_id"] for row in store.list_tracked_strategies()] == ["strategy-1"]


def test_tracking_table_is_part_of_user_assets() -> None:
    """观察到的东西是我攒下来的证据，它和策略本身一样丢不起。"""

    assert "strategy_tracking" in ASSET_TABLES
    assert "strategy_tracking_trades" in ASSET_TABLES
    assert "strategy_tracking_plan" in ASSET_TABLES


def _trade(trade_date: int, code: str, **rest: object) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "phase": "post",
        "direction": "BUY",
        "market": "SH",
        "code": code,
        "name": f"测试{code}",
        "size": 100.0,
        "price": 10.0,
        "pnl": 0.0,
        "reason": "买入条件",
        **rest,
    }


def test_tracking_trades_survive_a_round_trip_in_order(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.save_tracking_trades(
        "strategy-1",
        [
            _trade(20260820, "600000", phase="pre"),
            _trade(20260905, "600001", direction="SELL", pnl=120.5, reason="卖出条件"),
        ],
    )

    rows = store.list_tracking_trades("strategy-1")

    assert [(row["trade_date"], row["code"], row["phase"]) for row in rows] == [
        (20260820, "600000", "pre"),
        (20260905, "600001", "post"),
    ]
    assert rows[1]["direction"] == "SELL"
    assert rows[1]["pnl"] == 120.5
    assert rows[1]["reason"] == "卖出条件"


def test_tracking_trade_pages_filter_and_sort_with_fee_details(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.save_tracking_trades(
        "strategy-1",
        [
            _trade(20260901, "600000", phase="pre", signal_date=20260831),
            _trade(
                20260902,
                "600001",
                phase="post",
                direction="SELL",
                signal_date=20260901,
                commission=1.2,
                stamp_tax=3.0,
                slippage=0.5,
                cost_basis=1000.0,
                pnl=25.0,
            ),
            _trade(20260903, "600002", phase="post", signal_date=20260902),
        ],
    )

    page = store.page_tracking_trades(
        "strategy-1", phase="post", date_from=20260902, limit=1, offset=0
    )

    assert page["total"] == 2
    assert page["limit"] == 1
    assert page["offset"] == 0
    assert [row["code"] for row in page["rows"]] == ["600002"]
    assert page["rows"][0]["commission"] is None
    next_page = store.page_tracking_trades(
        "strategy-1", phase="post", date_from=20260902, limit=1, offset=1
    )
    assert next_page["rows"][0]["code"] == "600001"
    assert next_page["rows"][0]["signal_date"] == 20260901
    assert next_page["rows"][0]["commission"] == 1.2
    assert next_page["rows"][0]["stamp_tax"] == 3.0
    assert next_page["rows"][0]["slippage"] == 0.5
    assert next_page["rows"][0]["cost_basis"] == 1000.0


def test_tracking_trades_are_replaced_not_appended(tmp_path) -> None:
    """重算是全量重跑：上一次跑出来的成交不能留在库里当孤儿行。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.save_tracking_trades(
        "strategy-1", [_trade(20260820, "600000"), _trade(20260821, "600002")]
    )
    store.save_tracking_trades("strategy-1", [_trade(20260822, "600001")])

    assert [row["code"] for row in store.list_tracking_trades("strategy-1")] == ["600001"]


def test_deleting_a_strategy_removes_its_trades_and_plan(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.save_tracking_trades("strategy-1", [_trade(20260905, "600000")])
    store.save_tracking_plan("strategy-1", {"as_of": "2026-09-05", "buys": []})

    assert store.delete_strategy("strategy-1") is True
    assert store.list_tracking_trades("strategy-1") == []
    assert store.read_tracking_plan("strategy-1") is None


def test_the_next_plan_keeps_only_the_newest_version(tmp_path) -> None:
    """计划隔天就作废，留历史版本只会让人照着上周的单子下单。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.save_tracking_plan("strategy-1", {"as_of": "2026-09-05", "buys": []})
    store.save_tracking_plan("strategy-1", {"as_of": "2026-09-08", "buys": [{"code": "600000"}]})

    plan = store.read_tracking_plan("strategy-1")

    assert plan is not None
    assert plan["as_of"] == "2026-09-08"
    assert plan["buys"] == [{"code": "600000"}]


def test_going_offline_drops_the_pending_plan(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking("strategy-1", launched_at=20260901)
    store.save_tracking_trades("strategy-1", [_trade(20260905, "600000")])
    store.save_tracking_plan("strategy-1", {"as_of": "2026-09-05", "buys": []})

    store.stop_strategy_tracking("strategy-1")

    # 观察段和成交明细是攒下来的证据，下线不该动它们；过期的计划要清掉。
    assert store.read_tracking_plan("strategy-1") is None
    assert len(store.list_tracking_trades("strategy-1")) == 1


def test_stopping_keeps_the_latest_tracking_state_snapshot(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _save(store)
    store.start_strategy_tracking("strategy-1", launched_at=20260901)
    state = {
        "as_of": "2026-09-02",
        "equity": 1250.0,
        "cash": 250.0,
        "position_value": 1000.0,
        "positions_count": 1,
        "holdings": [{"code": "600000", "size": 100.0}],
    }
    store.save_tracking_points(
        "strategy-1",
        [_point(20260902, 1.25)],
        plan={"as_of": "2026-09-02", "buys": []},
        last_tracked_date=20260902,
        tracking_state={
            "current_state": state,
            "today_candidates": {"as_of": "2026-09-02", "total": 0, "rows": []},
        },
    )

    store.stop_strategy_tracking("strategy-1")

    record = store.read_strategy("strategy-1")
    assert record is not None
    assert record["tracking_state"]["current_state"] == state
    assert record["tracking_state"]["today_candidates"]["total"] == 0
    assert store.read_tracking_plan("strategy-1") is None
