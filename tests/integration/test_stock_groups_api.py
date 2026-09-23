"""股票组的 HTTP 层：存一批标的、观察它们入组之后的表现。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

DAYS = 10
TODAY = date.today()
START = TODAY - timedelta(days=DAYS - 1)


def _bars(closes: list[float], *, start_index: int = 0) -> pd.DataFrame:
    """close 序列；start_index > 0 表示这只票晚几天才出现（新股）。"""

    count = len(closes) - start_index
    values = np.asarray(closes[start_index:], dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range(START + timedelta(days=start_index), periods=count, freq="D"),
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": np.full(count, 1000.0),
            "amount": values * 1000.0,
            "bar_status": "completed",
        }
    )


def _seed(store: DuckDbMarketDataStore) -> None:
    # A: 10 -> 20，+100%
    store.replace_local_bars("SH", "600001", "stock", _bars(list(np.linspace(10, 20, DAYS))))
    # B: 10 -> 5，-50%
    store.replace_local_bars("SH", "600002", "stock", _bars(list(np.linspace(10, 5, DAYS))))
    # C: 第 5 天才出现，入组首日无价 -> 不计入曲线
    store.replace_local_bars(
        "SZ", "000001", "stock", _bars(list(np.linspace(8, 8, DAYS)), start_index=5)
    )
    # D: 全程持平，只进全市场对照
    store.replace_local_bars("SZ", "000002", "stock", _bars([10.0] * DAYS))


def _store(tmp_path: Path) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    return store


def test_crud_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        created = client.post(
            "/api/v1/stock-groups",
            json={
                "name": "探针组",
                "note": "从选股结果存的",
                "items": [
                    {"market": "SH", "code": "600001", "name": "A"},
                    {"market": "SH", "code": "600002", "name": "B"},
                ],
                "source": "screen",
                "source_history_id": "h1",
            },
        )
        assert created.status_code == 201
        group_id = created.json()["data"]["group_id"]
        assert created.json()["data"]["source_label"] == "选股结果"

        listed = client.get("/api/v1/stock-groups")
        assert listed.status_code == 200
        assert [item["group_id"] for item in listed.json()["data"]] == [group_id]
        # 列表不带成员，只带数量：一次把几十组的成员全查出来没必要。
        assert listed.json()["data"][0]["item_count"] == 2

        read = client.get(f"/api/v1/stock-groups/{group_id}")
        assert read.status_code == 200
        assert read.json()["data"]["items"][0]["code"] == "600001"

        renamed = client.patch(
            f"/api/v1/stock-groups/{group_id}", json={"name": "改过的名字"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["data"]["name"] == "改过的名字"

        trimmed = client.post(
            f"/api/v1/stock-groups/{group_id}/members/remove",
            json={"members": [{"market": "SH", "code": "600002"}]},
        )
        assert trimmed.status_code == 200
        assert [item["code"] for item in trimmed.json()["data"]["items"]] == ["600001"]

        deleted = client.delete(f"/api/v1/stock-groups/{group_id}")
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True
        assert client.get(f"/api/v1/stock-groups/{group_id}").status_code == 404


def test_duplicate_members_are_collapsed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.post(
            "/api/v1/stock-groups",
            json={
                "name": "重复组",
                "items": [
                    {"market": "SH", "code": "600001"},
                    {"market": "sh", "code": "600001"},
                ],
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["item_count"] == 1


def test_create_rejects_an_empty_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.post("/api/v1/stock-groups", json={"name": "空组", "items": []})

    assert response.status_code == 422


def test_performance_compares_the_group_with_the_whole_market(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # 观察起点是入组那天，所以要让组「入组」在数据开始的那天，而不是此刻——
    # 此刻建的组只有一天数据，看不出涨跌。
    store.save_stock_group(
        "g1",
        name="探针组",
        items=[
            {"market": "SH", "code": "600001", "name": "A"},
            {"market": "SH", "code": "600002", "name": "B"},
            {"market": "SZ", "code": "000001", "name": "C"},
        ],
        created_at=datetime.combine(START, datetime.min.time()),
    )

    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.get("/api/v1/stock-groups/g1/performance")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["start_date"] == START.isoformat()
    assert payload["trading_days"] == DAYS

    group_curve = payload["group_curve"]
    market_curve = payload["market_curve"]
    # A 翻倍、B 腰斩，等权 -> +25%
    assert group_curve["instrument_count"] == 2
    assert group_curve["performance"]["total_return"] == 0.25
    # 全市场 = A/B/D 各买一点，C 首日无价不计入 -> +16.67%
    assert market_curve["instrument_count"] == 3
    assert market_curve["performance"]["total_return"] == pytest.approx(0.1666666, abs=1e-5)
    assert payload["excess_return"] == pytest.approx(0.0833333, abs=1e-5)

    # 起点无价的 C 必须被点名，不能静默丢掉。
    assert [item["code"] for item in payload["excluded"]] == ["000001"]
    assert any("没有收盘价" in note for note in payload["notes"])

    spread = payload["spread"]
    assert spread["counted"] == 2 and spread["advanced"] == 1 and spread["declined"] == 1
    assert spread["best"]["code"] == "600001"


def test_criteria_snapshot_survives_the_group(tmp_path: Path) -> None:
    """筛选条件存的是快照，不是回指历史——历史只留 20 条，组还在它就该还在。"""

    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        created = client.post(
            "/api/v1/stock-groups",
            json={
                "name": "带条件的组",
                "items": [{"market": "SH", "code": "600001"}],
                "source": "screen",
                "criteria": {
                    "summary": "MACD 金叉 + 收盘价 > MA20",
                    "payload": {"mode": "preset", "signals": ["macd_cross"]},
                },
            },
        )
        assert created.status_code == 201
        group_id = created.json()["data"]["group_id"]
        assert created.json()["data"]["criteria"]["summary"] == "MACD 金叉 + 收盘价 > MA20"

        # 列表也要带：折叠态要显示「这组是怎么选出来的」。
        listed = client.get("/api/v1/stock-groups")
        assert listed.json()["data"][0]["criteria"]["payload"]["signals"] == ["macd_cross"]

        # 手建的组没有筛选条件，是 null 而不是空对象——两者在 UI 上含义不同。
        manual = client.post(
            "/api/v1/stock-groups",
            json={"name": "手建组", "items": [{"market": "SH", "code": "600002"}]},
        )
        assert manual.status_code == 201
        manual_id = manual.json()["data"]["group_id"]
        assert manual.json()["data"]["criteria"] is None

        # 删组要连快照一起删，否则备份里会留一条谁也读不到的孤儿记录。
        assert client.delete(f"/api/v1/stock-groups/{group_id}").status_code == 200
        assert client.get(f"/api/v1/stock-groups/{manual_id}").status_code == 200
        assert client.get(f"/api/v1/stock-groups/{manual_id}").json()["data"]["criteria"] is None


def test_criteria_without_a_summary_is_rejected(tmp_path: Path) -> None:
    """给了 criteria 却没摘要必须报错：那会让折叠态空一栏，看着像忘了存。"""

    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.post(
            "/api/v1/stock-groups",
            json={
                "name": "缺摘要",
                "items": [{"market": "SH", "code": "600001"}],
                "criteria": {"payload": {"mode": "preset"}},
            },
        )

    assert response.status_code == 422


def test_summary_uses_one_window_for_every_group(tmp_path: Path) -> None:
    """折叠态的『近 N 个交易日』对所有组同一个窗口，所以组之间可以直接比。"""

    store = _store(tmp_path)
    store.save_stock_group(
        "g1",
        name="全涨组",
        items=[{"market": "SH", "code": "600001"}],
        created_at=datetime.combine(START, datetime.min.time()),
    )
    store.save_stock_group(
        "g2",
        name="全跌组",
        items=[{"market": "SH", "code": "600002"}],
        created_at=datetime.combine(START, datetime.min.time()),
    )

    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.get("/api/v1/stock-groups/summary", params={"windows": "3,5"})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["windows"] == [3, 5]
    assert payload["labels"]["3"] == "近 3 个交易日"
    # 10 个交易日的数据，要 3 日和 5 日窗口都拿得到。
    assert payload["as_of"] == TODAY.isoformat()

    by_group = {item["group_id"]: item["windows"] for item in payload["groups"]}
    assert set(by_group) == {"g1", "g2"}
    # 两个组共用同一份行情、同一个窗口：g1 涨、g2 跌，方向相反且都与全市场不同。
    rising = by_group["g1"][0]
    falling = by_group["g2"][0]
    assert rising["trading_days"] == 3
    assert rising["group_return"] > 0
    assert falling["group_return"] < 0
    assert rising["excess_return"] > falling["excess_return"]
    assert rising["counted"] == 1 and rising["complete"] is True


def test_summary_marks_a_window_short_of_data(tmp_path: Path) -> None:
    """窗口比库里的行情还长时，数字照给但必须标出实际只有几天。"""

    store = _store(tmp_path)
    store.save_stock_group(
        "g1",
        name="探针组",
        items=[{"market": "SH", "code": "600001"}],
        created_at=datetime.combine(START, datetime.min.time()),
    )

    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.get("/api/v1/stock-groups/summary", params={"windows": "60"})

    assert response.status_code == 200
    window = response.json()["data"]["groups"][0]["windows"][0]
    # 库里只有 10 天，60 日窗口凑不满：不能假装它是 60 天的收益。
    assert window["trading_days"] == DAYS
    assert window["complete"] is False


def test_summary_without_groups_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.get("/api/v1/stock-groups/summary")

    assert response.status_code == 200
    assert response.json()["data"]["groups"] == []
    assert response.json()["data"]["windows"] == [20, 60]


def test_performance_of_a_missing_group_is_404(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        assert client.get("/api/v1/stock-groups/nope/performance").status_code == 404


def test_performance_without_market_data_says_so(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "empty.duckdb")
    store.save_stock_group(
        "g1",
        name="空库组",
        items=[{"market": "SH", "code": "600001"}],
        created_at=datetime.combine(START, datetime.min.time()),
    )

    with TestClient(
        create_app(market_data_store=store, auto_sync_enabled=False)
    ) as client:
        response = client.get("/api/v1/stock-groups/g1/performance")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["group_curve"] is None
    assert any("没有行情" in note for note in payload["notes"])
