"""全市场策略池的研究范围（板块 / ST 过滤）与研究范围偏好存储。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_pool.service import (
    POOL_BOARDS,
    MarketStrategyPoolService,
    _boards_from_payload,
    _symbol_board,
)
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.web.app import create_app


def _bars() -> pd.DataFrame:
    close = np.full(6, 10.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=6, freq="70D"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(6, 5_000.0),
            "amount": close * 5_000,
            "bar_status": "completed",
        }
    )


SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("SH", "600000", "浦发银行"),
    ("SH", "688001", "华兴源创"),
    ("SH", "600002", "ST样本"),
    ("SZ", "000001", "平安银行"),
    ("SZ", "300001", "特锐德"),
    ("SZ", "301002", "崧盛股份"),
)


def _service(tmp_path) -> MarketStrategyPoolService:
    store = DuckDbMarketDataStore(tmp_path / "universe.duckdb")
    for market, code, name in SYMBOLS:
        store.replace_local_bars(market, code, "stock", _bars(), name=name)
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()
    return MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=SimpleNamespace(),
        pool_store=pool_store,
    )


def test_symbol_board_classification() -> None:
    assert _symbol_board("SH", "600000") == "main"
    assert _symbol_board("SH", "601318") == "main"
    assert _symbol_board("SH", "688001") == "star"
    assert _symbol_board("SH", "689009") == "star"
    assert _symbol_board("SZ", "000001") == "main"
    assert _symbol_board("SZ", "002594") == "main"
    assert _symbol_board("SZ", "300001") == "chinext"
    assert _symbol_board("SZ", "301002") == "chinext"
    assert _symbol_board("SZ", "302001") == "chinext"
    # 三个板块穷尽本地 A 股；未知市场兜底归主板而不是悄悄丢掉。
    assert _symbol_board("BJ", "830001") == "main"
    assert set(POOL_BOARDS) == {"main", "chinext", "star"}


def test_boards_from_payload_validation() -> None:
    assert _boards_from_payload({}) is None
    assert _boards_from_payload({"boards": None}) is None
    assert _boards_from_payload({"boards": ["main", "chinext"]}) == ["main", "chinext"]
    with pytest.raises(ValueError, match="至少要保留一个板块"):
        _boards_from_payload({"boards": []})
    with pytest.raises(ValueError, match="无效"):
        _boards_from_payload({"boards": ["main", "bse"]})
    with pytest.raises(ValueError, match="列表"):
        _boards_from_payload({"boards": "main"})


def test_research_universe_skips_filters_for_single_symbol(tmp_path) -> None:
    service = _service(tmp_path)
    payload = {"boards": ["main"], "exclude_st": True}
    assert service._research_universe(payload, ("SH", "600000")) == (None, False)
    assert service._research_universe(payload, None) == (["main"], True)
    assert service._research_universe({}, None) == (None, False)


def test_list_refs_filters_boards_and_st(tmp_path) -> None:
    service = _service(tmp_path)

    everything = service._list_refs(None)
    assert {(ref.market, ref.code) for ref in everything} == {
        (market, code) for market, code, _ in SYMBOLS
    }

    no_star = service._list_refs(None, boards=["main", "chinext"])
    assert {(ref.market, ref.code) for ref in no_star} == {
        ("SH", "600000"),
        ("SH", "600002"),
        ("SZ", "000001"),
        ("SZ", "300001"),
        ("SZ", "301002"),
    }

    star_only = service._list_refs(None, boards=["star"])
    assert [(ref.market, ref.code) for ref in star_only] == [("SH", "688001")]

    no_st = service._list_refs(None, exclude_st=True)
    assert ("SH", "600002") not in {(ref.market, ref.code) for ref in no_st}
    assert len(no_st) == 5

    main_no_st = service._list_refs(None, boards=["main", "chinext"], exclude_st=True)
    assert {(ref.market, ref.code) for ref in main_no_st} == {
        ("SH", "600000"),
        ("SZ", "000001"),
        ("SZ", "300001"),
        ("SZ", "301002"),
    }

    # 单股重跑在真实路径上不携带范围过滤（_research_universe 已豁免），显式指名的
    # 票即使属于被排除的板块/ST 也总能拿到。
    single = service._list_refs(("SH", "600002"))
    assert [(ref.market, ref.code) for ref in single] == [("SH", "600002")]


def test_research_settings_store_roundtrip(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "settings.duckdb")
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()

    assert pool_store.read_research_settings() is None

    saved = pool_store.save_research_settings(boards=["chinext", "main"], exclude_st=True)
    assert saved == {"boards": ["chinext", "main"], "exclude_st": True}

    read = pool_store.read_research_settings()
    assert read is not None
    assert read["boards"] == ["chinext", "main"]
    assert read["exclude_st"] is True

    # 保存值会整体覆盖，而不是和上一次合并。
    pool_store.save_research_settings(boards=["star"], exclude_st=False)
    again = pool_store.read_research_settings()
    assert again == {"boards": ["star"], "exclude_st": False}


def test_research_settings_api_defaults_save_and_validation(tmp_path) -> None:
    app = create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "api.duckdb"),
        strategy_worker_token="token",
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        default = client.get("/api/v1/strategy-pool/research-settings")
        assert default.status_code == 200
        assert default.json()["data"] == {
            "boards": ["main", "chinext", "star"],
            "exclude_st": False,
        }

        saved = client.put(
            "/api/v1/strategy-pool/research-settings",
            json={"boards": ["main", "chinext"], "exclude_st": True},
        )
        assert saved.status_code == 200
        assert saved.json()["data"] == {"boards": ["chinext", "main"], "exclude_st": True}

        reread = client.get("/api/v1/strategy-pool/research-settings")
        assert reread.json()["data"] == {"boards": ["chinext", "main"], "exclude_st": True}

        empty = client.put(
            "/api/v1/strategy-pool/research-settings",
            json={"boards": [], "exclude_st": False},
        )
        assert empty.status_code == 422

        invalid = client.put(
            "/api/v1/strategy-pool/research-settings",
            json={"boards": ["bse"], "exclude_st": False},
        )
        assert invalid.status_code == 422

        # 保存失败请求不会破坏已有偏好。
        final = client.get("/api/v1/strategy-pool/research-settings")
        assert final.json()["data"] == {"boards": ["chinext", "main"], "exclude_st": True}


def test_pool_job_request_accepts_universe_fields(tmp_path) -> None:
    # full 模式提交前会校验本地存在已完成日线，先放一根。
    store = DuckDbMarketDataStore(tmp_path / "payload.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    app = create_app(
        market_data_store=store,
        strategy_worker_token="token",
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/strategy-pool/jobs",
            json={
                "mode": "full",
                "risk_trials": 2_000,
                "boards": ["main", "chinext"],
                "exclude_st": True,
            },
        )
        assert created.status_code == 202
        job_id = created.json()["data"]["job_id"]

        stored = app.state.strategy_job_store.read_request(job_id)
        assert stored is not None
        payload = stored["payload"]
        assert payload["boards"] == ["main", "chinext"]
        assert payload["exclude_st"] is True

        client.post(f"/api/v1/strategy-pool/jobs/{job_id}/cancel")
