from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aquest.market_data.benchmark import BENCHMARK_LABEL, BENCHMARK_SYMBOL
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_pool import simulation_archive, simulations
from aquest.strategy_pool.simulations import PoolSimulations
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore, dumps
from aquest.web.app import create_app


def _signal(code: str, score: float) -> dict:
    return {
        "market": "SH",
        "code": code,
        "name": code,
        "tracking_version_id": code,
        "postLaunchReturn": score,
        "annual_return": score,
        "days": 20,
    }


def _publish(jobs: DuckDbStrategyJobStore, day: str, buys: list[dict]) -> None:
    job = f"signal-{day}"
    jobs.create(
        job,
        operation="market_strategy_pool_tracking",
        payload={"mode": "signals", "as_of_date": day},
        description="扫描",
    )
    jobs.write_result(
        job,
        {
            "simulation_rank_version": 2,
            "results_by_objective": {
                "annual_return": {"buy_signals": buys, "sell_signals": []},
            },
        },
    )


def test_simulation_replays_signal_only_rank_and_replaces_only_after_filled_sale(
    tmp_path, monkeypatch
):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    monkeypatch.setattr(
        simulations,
        "shanghai_now",
        lambda: datetime(2026, 11, 26, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    pool = PoolSimulations(store)
    pool.initialize()
    DuckDbStrategyPoolStore(store).initialize()
    PoolTrackingStore(store).bootstrap()
    jobs = DuckDbStrategyJobStore(store)
    days = ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]
    codes = [f"60000{i}" for i in range(8)]
    for code in codes:
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(days),
                "open": [10.0] * 4,
                "high": [10.0] * 4,
                "low": [10.0, 10.0, 7.0 if code == codes[0] else 10.0, 10.0],
                "close": [10.0] * 4,
                "volume": [1000.0] * 4,
                "amount": [10000.0] * 4,
                "bar_status": ["completed"] * 4,
            }
        )
        store.replace_local_bars("SH", code, "stock", frame)
        with store.state_write_transaction() as con:
            con.execute(
                """INSERT INTO pool_tracking_versions
                (version_id, market, code, objective, source, signature, candidate_json,
                 created_at, is_current, updated_at) VALUES (?, 'SH', ?, 'annual_return',
                 'automatic', 'sig', ?, ?, TRUE, ?)""",
                [
                    code,
                    code,
                    dumps(
                        {
                            "config": {"stop_loss_pct": 0.2},
                            "buy_rule": {
                                "conditions": [
                                    {
                                        "left_value": "price.close",
                                        "operator": "gt",
                                        "right_constant": 0,
                                    }
                                ]
                            },
                            "sell_rule": {
                                "conditions": [
                                    {
                                        "left_value": "price.close",
                                        "operator": "lt",
                                        "right_constant": 0,
                                    }
                                ]
                            },
                        }
                    ),
                    days[0],
                    days[0],
                ],
            )
    for i, day in enumerate(days[:-1]):
        monkeypatch.setattr(
            simulation_archive,
            "shanghai_now",
            lambda day=day: datetime.fromisoformat(f"{day}T16:00:00").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        )
        order = codes[:5] if i == 0 else [codes[7], codes[1], codes[6]]
        _publish(jobs, day, [_signal(code, 1 - rank * 0.01) for rank, code in enumerate(order)])

    key = pool.create(
        {
            "name": "收益盘",
            "objective": "annual_return",
            "ranking": "annual_return",
            "direction": "desc",
            "initial_cash": 100_000.0,
            "min_days": 0,
            "start_date": days[0],
        }
    )
    store.replace_benchmark_bars(
        BENCHMARK_SYMBOL,
        [(date(2026, 11, 24), 4.0), (date(2026, 11, 26), 4.2)],
    )
    pool.advance()
    result = pool.read(key)[0]
    assert result["holding_count"] == 5
    assert result["benchmark"] == {
        "label": BENCHMARK_LABEL,
        "dates": ["2026-11-24", "2026-11-26"],
        "totals": [4.0, 4.2],
    }
    assert result["trades_total"] == 7
    assert result["trades"][0]["symbol"] == "SH600007"
    assert result["trades"][0]["rank"] == 1
    assert result["trades"][0]["signal_date"] == days[2]
    assert result["trades"][0]["date"] == days[3]
    assert result["trades"][1]["reason"] == "止损"
    assert result["trades"][1]["symbol"] == "SH600000"
    assert result["warning"] is None
    pool.advance()
    assert pool.read(key)[0] == result

    smaller = pool.create(
        {
            "name": "三只持仓",
            "objective": "annual_return",
            "ranking": "annual_return",
            "direction": "desc",
            "initial_cash": 100_000.0,
            "min_days": 0,
            "start_date": days[0],
            "max_positions": 3,
        }
    )
    pool.advance()
    smaller_result = pool.read(smaller)[0]
    assert smaller_result["holding_count"] == smaller_result["max_positions"] == 3
    assert smaller_result["trades_total"] == 5
    assert result["max_positions"] == 5

    # 首次拆状态库后，信号归档与账本仍然可读。
    counts = store.ensure_state_database()
    assert counts["pool_simulations"] == 2
    assert counts["pool_simulation_signals"] == 3
    assert pool.read(key)[0] == result


def test_simulation_http_rejects_start_without_archived_signal(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    app = create_app(market_data_store=store, auto_sync_enabled=False)
    with TestClient(app) as client:
        url = "/api/v1/strategy-pool/simulations"
        assert client.get(url).json() == {"data": []}
        assert client.get(f"{url}/signal-days").json() == {"data": []}
        assert client.get(f"{url}/missing").status_code == 404
        response = client.post(
            url,
            json={
                "name": "年化盘",
                "objective": "annual_return",
                "ranking": "postLaunchReturn",
                "start_date": str(date(2026, 11, 23)),
            },
        )
        assert response.status_code == 422


def test_simulation_http_delete_removes_account(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    app = create_app(market_data_store=store, auto_sync_enabled=False)
    with TestClient(app) as client:
        url = "/api/v1/strategy-pool/simulations"
        config = {
            "name": "待删盘",
            "objective": "annual_return",
            "ranking": "default",
            "direction": "desc",
            "min_days": 0,
            "start_date": "2026-11-26",
            "max_positions": 5,
            "initial_cash": 100_000.0,
        }
        state = {
            "cash": 100_000.0,
            "positions": {},
            "pending": {},
            "cooldown": {},
            "trades": [],
            "equity": [],
            "started": False,
            "paused": True,
            "cursor": None,
            "minimum_signal_date": "2026-11-26",
            "warning": None,
        }
        with store.state_write_transaction() as con:
            con.execute(
                "INSERT INTO pool_simulations VALUES ('sim-delete', ?, ?)",
                [dumps(config), dumps(state)],
            )
        assert client.get(f"{url}/sim-delete").json()["data"]["all_trades"] == []
        assert client.delete(f"{url}/missing").status_code == 404
        response = client.delete(f"{url}/sim-delete")
        assert response.status_code == 200
        assert response.json() == {"data": {"id": "sim-delete"}}
        assert client.get(f"{url}/sim-delete").status_code == 404
        assert client.get(url).json() == {"data": []}


def test_unfilled_exit_keeps_slot_and_same_day_exit_cannot_reenter():
    from aquest.portfolio_backtest.models import PortfolioBacktestConfig
    from aquest.strategy_pool.simulations import execute_orders

    config = PortfolioBacktestConfig(
        vipdoc_path="",
        universe="codes",
        codes=("SH:600000",),
        selected_signals=("technical.macd",),
        combine_mode="all",
        minimum_matches=None,
        ranking_value="price.close",
        stop_loss_pct=0.2,
    )
    old = _signal("600000", 0.9)
    next_stock = _signal("600001", 0.8)
    state = {
        "cash": 0.0,
        "started": True,
        "cooldown": {},
        "trades": [],
        "positions": {"SH600000": {**old, "size": 1000, "cost": 10000.0, "mark": 10.0}},
        "pending": {"SH600000": {"signal_date": "2026-11-23", "reason": "卖出条件"}},
    }
    execute_orders(
        state,
        [old, next_stock],
        "2026-11-23",
        "2026-11-24",
        lambda symbol: None if symbol == "SH600000" else (9.0, 9.0),
        lambda _: config,
    )
    assert list(state["positions"]) == ["SH600000"]
    assert state["trades"] == []
    execute_orders(
        state,
        [old, next_stock],
        "2026-11-24",
        "2026-11-25",
        lambda _: (9.0, 9.0),
        lambda _: config,
    )
    assert list(state["positions"]) == ["SH600001"]
    assert [trade["direction"] for trade in state["trades"]] == ["SELL", "BUY"]
    assert state["trades"][0]["signal_date"] == "2026-11-23"
    assert state["trades"][1]["rank"] == 2
    assert state["cash"] >= 0


def test_existing_signal_is_recovered_only_with_same_day_ranking(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = PoolSimulations(store)
    pool.initialize()
    PoolTrackingStore(store).bootstrap()
    jobs = DuckDbStrategyJobStore(store)
    day = "2026-11-26"
    job = "existing-scan"
    jobs.create(
        job,
        operation="market_strategy_pool_tracking",
        payload={"mode": "signals", "as_of_date": day},
        description="旧信号",
    )
    jobs.write_result(
        job,
        {
            "results_by_objective": {
                "annual_return": {"buy_signals": [_signal("600000", 0.5)], "sell_signals": []},
            }
        },
    )
    original_time = datetime(2026, 11, 26, 16, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    with store.state_write_transaction() as con:
        row = con.execute("SELECT state_json FROM strategy_jobs WHERE job_id = ?", [job]).fetchone()
        assert row is not None
        state = json.loads(row[0])
        state["finished_at"] = original_time
        con.execute("UPDATE strategy_jobs SET state_json = ? WHERE job_id = ?", [dumps(state), job])
        con.execute(
            """INSERT INTO pool_tracking_versions
               (version_id, market, code, objective, source, signature, candidate_json,
                created_at, is_current, updated_at, summary_json) VALUES
               ('600000', 'SH', '600000', 'annual_return', 'automatic', 'sig', '{}',
                ?, TRUE, ?, ?)""",
            [day, day, dumps({"as_of_date": "2026-11-27", "full_period": {"annual_return": 0.2}})],
        )
    jobs.initialize()
    assert pool.signal_days("annual_return", "composite") == []
    with store.state_write_transaction() as con:
        con.execute(
            "UPDATE pool_tracking_versions SET summary_json = ?",
            [dumps({"as_of_date": day, "full_period": {"annual_return": 0.1}})],
        )
    jobs.initialize()
    assert pool.signal_days("annual_return", "annual_return") == [day]
    with store.state_read_session() as con:
        archive = con.execute("SELECT archived_at FROM pool_simulation_signals").fetchone()
    assert archive == (datetime(2026, 11, 26, 16),)


def test_legacy_signal_day_backfills_only_for_default_order(tmp_path):
    """旧版完整扫描没有排序字段时，仍可按当日原始顺序回放。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = PoolSimulations(store)
    pool.initialize()
    DuckDbStrategyPoolStore(store).initialize()
    PoolTrackingStore(store).bootstrap()
    jobs = DuckDbStrategyJobStore(store)
    day = "2026-11-25"
    job = "legacy-scan"
    jobs.create(
        job,
        operation="market_strategy_pool_tracking",
        payload={"mode": "signals", "as_of_date": day},
        description="旧版信号",
    )
    jobs.write_result(
        job,
        {
            "results_by_objective": {
                "annual_return": {
                    "buy_signals": [
                        {
                            "market": "SH",
                            "code": "600000",
                            "name": "浦发银行",
                            "tracking_version_id": "v1",
                            "status": "buy",
                        }
                    ],
                    "sell_signals": [],
                },
            },
        },
    )
    finished = datetime(2026, 11, 25, 16, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    with store.state_write_transaction() as con:
        row = con.execute("SELECT state_json FROM strategy_jobs WHERE job_id = ?", [job]).fetchone()
        assert row is not None
        state = json.loads(row[0])
        state["finished_at"] = finished
        con.execute("UPDATE strategy_jobs SET state_json = ? WHERE job_id = ?", [dumps(state), job])
    jobs.initialize()
    assert pool.signal_days("annual_return", "default") == [day]
    assert pool.signal_days("annual_return", "composite") == []
    base_config = {
        "name": "旧信号盘",
        "objective": "annual_return",
        "direction": "desc",
        "initial_cash": 100_000.0,
        "min_days": 0,
        "start_date": day,
        "max_positions": 5,
    }
    assert pool.create({**base_config, "ranking": "default"})
    with pytest.raises(ValueError):
        pool.create({**base_config, "ranking": "composite"})
