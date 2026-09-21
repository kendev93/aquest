from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

from selector_app.market_data.store import DuckDbMarketDataStore


def test_backtest_history_round_trip_keeps_timestamps_and_payloads(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    started = datetime(2026, 9, 11, 8, 30, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=2.5)

    store.save_backtest_history(
        "single-1",
        backtest_type="single_stock",
        request={"market": "SH", "code": "600000"},
        result={
            "market": "SH",
            "code": "600000",
            "name": "浦发银行",
            "start_date": "2024-01-01",
            "end_date": "2024-01-10",
            "performance": {"total_return": 0.12, "sharpe": 1.4},
            "trades": [{"direction": "BUY"}],
        },
        started_at=started,
        finished_at=finished,
    )

    summaries = store.list_backtest_history("single_stock")
    detail = store.read_backtest_history("single-1")

    assert summaries[0]["history_id"] == "single-1"
    assert summaries[0]["backtest_type"] == "single_stock"
    assert summaries[0]["started_at"]
    assert summaries[0]["finished_at"]
    assert summaries[0]["duration_seconds"] == 2.5
    assert summaries[0]["summary"]["start_date"] == "2024-01-01"
    assert summaries[0]["summary"]["end_date"] == "2024-01-10"
    assert summaries[0]["summary"]["name"] == "浦发银行"
    assert summaries[0]["summary"]["performance"]["sharpe"] == 1.4
    assert detail is not None
    assert detail["request"] == {"market": "SH", "code": "600000"}
    assert detail["result"]["trades"] == [{"direction": "BUY"}]
    assert detail["started_at"] == summaries[0]["started_at"]
    assert store.delete_backtest_history("single-1") is True
    assert store.read_backtest_history("single-1") is None


def test_backtest_history_retains_latest_twenty_per_type(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for index in range(22):
        started = base + timedelta(days=index)
        store.save_backtest_history(
            f"portfolio-{index}",
            backtest_type="portfolio",
            request={"max_positions": 5},
            result={"performance": {"total_return": index / 100}},
            started_at=started,
            finished_at=started + timedelta(seconds=1),
        )

    history = store.list_backtest_history("portfolio")

    assert len(history) == 20
    assert [item["history_id"] for item in history] == [
        f"portfolio-{index}" for index in range(21, 1, -1)
    ]
    assert store.read_backtest_history("portfolio-0") is None


def test_backtest_history_summarizes_names_from_multi_instrument_results(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    store.save_backtest_history(
        "portfolio-names",
        backtest_type="portfolio",
        request={"universe": "all"},
        result={
            "universe": "all",
            "trades": [
                {"market": "SH", "code": "600000", "name": "浦发银行"},
                {"market": "SZ", "code": "000001", "name": "平安银行"},
            ],
        },
        started_at=timestamp,
        finished_at=timestamp + timedelta(seconds=1),
    )

    summary = store.list_backtest_history("portfolio")[0]["summary"]

    assert summary["instrument_names"] == [
        {"market": "SH", "code": "600000", "name": "浦发银行"},
        {"market": "SZ", "code": "000001", "name": "平安银行"},
    ]


def test_backtest_history_enriches_older_records_from_instrument_names(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2024-01-02"),
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "source": "local",
                    "price_basis": "raw",
                    "bar_status": "completed",
                }
            ]
        ),
    )
    store.update_instrument_names({("SH", "600000"): "浦发银行"})
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_backtest_history(
        "old-format",
        backtest_type="single_stock",
        request={"market": "SH", "code": "600000"},
        result={
            "market": "SH",
            "code": "600000",
            "trades": [{"market": "SH", "code": "600000", "direction": "BUY"}],
        },
        started_at=timestamp,
        finished_at=timestamp + timedelta(seconds=1),
    )

    history = store.list_backtest_history("single_stock")[0]
    detail = store.read_backtest_history("old-format")

    assert history["summary"]["name"] == "浦发银行"
    assert detail is not None
    assert detail["result"]["name"] == "浦发银行"
    assert detail["result"]["trades"][0]["name"] == "浦发银行"


def test_strategy_discovery_history_upgrade_backfills_missing_timestamps(tmp_path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE data_meta (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO data_meta VALUES ('schema_version', '7', CURRENT_TIMESTAMP)"
        )
        connection.execute(
            """
            CREATE TABLE strategy_discovery_history (
                history_id VARCHAR PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                request_json VARCHAR NOT NULL,
                search_metadata_json VARCHAR NOT NULL,
                validation_metadata_json VARCHAR NOT NULL,
                algorithm_metadata_json VARCHAR NOT NULL,
                data_fingerprint VARCHAR NOT NULL,
                results_by_sharpe_json VARCHAR NOT NULL,
                results_by_return_json VARCHAR NOT NULL,
                total_trials BIGINT NOT NULL,
                duration_seconds DOUBLE NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_discovery_history VALUES
            ('old-run', TIMESTAMP '2026-01-01 08:00:00', '{}', '{"evaluated_trials": 1}',
             '{}', '{}', '', '[]', '[]', 1, 12.5)
            """
        )

    store = DuckDbMarketDataStore(database)
    detail = store.read_strategy_discovery_history("old-run")

    assert detail is not None
    assert str(detail["started_at"]).startswith("2026-01-01 08:00:00")
    assert str(detail["finished_at"]).startswith("2026-01-01 08:00:12")


def _saved(store: DuckDbMarketDataStore, history_id: str, backtest_type: str, result) -> dict:
    started = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
    store.save_backtest_history(
        history_id,
        backtest_type=backtest_type,
        request={"universe": "all"},
        result=result,
        started_at=started,
        finished_at=started + timedelta(minutes=2),
    )
    return store.list_backtest_history(backtest_type)[0]["summary"]


def test_condition_research_summary_prefers_the_strongest_qualified_candidate(
    tmp_path,
) -> None:
    """列表要拿合格候选当门面：提升再高，没过约束也只是噪声。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    summary = _saved(
        store,
        "cond-1",
        "condition_research",
        {
            "universe": "sz",
            "start_date": "2021-01-01",
            "end_date": "2025-12-31",
            "search_sides": "buy",
            "processed": 300,
            "baseline": {"train": {"pooled_trades": {"closed_trades": 40, "win_rate": 0.7}}},
            "candidates": [
                {
                    "candidate_id": "noise",
                    "side": "buy",
                    "condition": {
                        "left_value": "technical.rsi14",
                        "operator": "gte",
                        "right_constant": 70.0,
                    },
                    "mining": {"lift": 2.0, "outcome_rate": 0.44},
                    "passed_constraints": False,
                },
                {
                    "candidate_id": "solid",
                    "side": "sell",
                    "condition": {
                        "left_value": "technical.rsi14",
                        "operator": "lte",
                        "right_constant": 30.0,
                    },
                    "mining": {
                        "lift": 1.42,
                        "outcome_rate": 0.61,
                        "baseline_rate": 0.43,
                        "support_count": 90,
                        "instrument_count": 27,
                    },
                    "passed_constraints": True,
                },
            ],
        },
    )

    assert summary["candidate_count"] == 2
    assert summary["qualified_count"] == 1
    assert summary["top_lift"] == 1.42
    assert summary["top_side"] == "sell"
    assert summary["top_outcome_rate"] == 0.61
    assert summary["top_instrument_count"] == 27
    assert summary["top_condition"]["operator"] == "lte"
    assert summary["top_condition"]["right_constant"] == 30.0
    assert summary["baseline_win_rate"] == 0.7


def test_swing_research_summary_reports_parameters_and_the_strongest_indicator(
    tmp_path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    summary = _saved(
        store,
        "swing-1",
        "swing_research",
        {
            "universe": "all",
            "stock_count": 1000,
            "scanned_stocks": 980,
            "reversal_multiple": 3.0,
            "min_bars": 5,
            "atr_window": 14,
            "horizon": 10,
            "permutations": 200,
            "value_count": 20,
            "significant_count": 1,
            "date_range": {"start": "2021-09-01", "end": "2026-09-11", "days": 1219},
            "labels": [{"id": "up_onset_ahead", "display_name": "近期将启动上涨"}],
            "scores": [
                {
                    "value_id": "technical.weak",
                    "display_name": "弱指标",
                    "ic_mean": 0.02,
                    "ic_threshold": 0.01,
                    "order": "high",
                    "significant": False,
                },
                {
                    "value_id": "technical.strong",
                    "display_name": "强指标",
                    "ic_mean": 0.24,
                    "ic_threshold": 0.017,
                    "order": "low",
                    "significant": True,
                },
            ],
            "condition_plan": {"tradeable_count": 3},
        },
    )

    assert summary["scanned_stocks"] == 980
    assert summary["reversal_multiple"] == 3.0
    assert summary["horizon"] == 10
    assert summary["significant_count"] == 1
    # 噪声门槛取所有指标里最严的那一个，而不是某个指标的。
    assert summary["noise_floor"] == 0.017
    assert summary["top_indicator"]["display_name"] == "强指标"
    assert summary["top_indicator"]["ic_mean"] == 0.24
    assert summary["tradeable_count"] == 3
    assert summary["label_names"] == ["近期将启动上涨"]
    assert summary["date_range"]["days"] == 1219


def test_plain_backtest_summary_is_untouched_by_research_fields(tmp_path) -> None:
    """摘要扩展是按类型分派的，回测历史不该多出一堆研究字段。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    summary = _saved(
        store,
        "portfolio-1",
        "portfolio",
        {
            "universe": "all",
            "performance": {"total_return": 0.1},
            "trades": [{"direction": "BUY"}],
        },
    )

    assert summary["performance"]["total_return"] == 0.1
    assert summary["trade_count"] == 1
    assert "top_indicator" not in summary
    assert "candidate_count" not in summary


def test_cross_type_history_keeps_a_quota_for_every_type(tmp_path) -> None:
    """热门类型不该把冷门类型挤出对比列表。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(25):
        started = base + timedelta(minutes=index)
        store.save_backtest_history(
            f"portfolio-{index}",
            backtest_type="portfolio",
            request={},
            result={"performance": {"total_return": index / 100}},
            started_at=started,
            finished_at=started + timedelta(seconds=1),
        )
    started = base + timedelta(hours=6)
    store.save_backtest_history(
        "factor-1",
        backtest_type="factor_test",
        request={},
        result={"date_range": {"start": "2024-01-01", "end": "2024-06-30", "days": 120}},
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )

    rows = store.list_backtest_history_all(limit_per_type=3)

    assert [row["history_id"] for row in rows][:3] == [
        "factor-1",
        "portfolio-24",
        "portfolio-23",
    ]
    assert sum(1 for row in rows if row["backtest_type"] == "portfolio") == 3
    assert sum(1 for row in rows if row["backtest_type"] == "factor_test") == 1


def test_read_backtest_histories_keeps_request_order_and_skips_missing(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for history_id in ("a", "b"):
        store.save_backtest_history(
            history_id,
            backtest_type="portfolio",
            request={"max_positions": 5},
            result={"performance": {"total_return": 0.1}},
            started_at=timestamp,
            finished_at=timestamp + timedelta(seconds=1),
        )

    records = store.read_backtest_histories(["b", "a", "missing", "b"])

    assert [record["history_id"] for record in records] == ["b", "a"]
    assert records[0]["request"] == {"max_positions": 5}
    assert store.read_backtest_histories([]) == []
