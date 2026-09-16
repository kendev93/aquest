from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.transfer import MANIFEST_NAME
from selector_app.web.app import create_app


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-01-02"],
            "open": [10.0, 10.5],
            "high": [10.5, 11.0],
            "low": [9.5, 10.0],
            "close": [10.2, 10.8],
            "volume": [1000.0, 1100.0],
            "amount": [10200.0, 11880.0],
            "bar_status": ["completed", "completed"],
        }
    )


def _store_with_asset(tmp_path: Path, tag: str) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / f"{tag}.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    store.save_backtest_history(
        f"bt-{tag}",
        backtest_type="condition_research",
        request={"code": "600000"},
        result={"net_profit": 1.0},
        started_at="2024-01-01T00:00:00",
        finished_at="2024-01-01T00:05:00",
    )
    return store


def test_manifest_reports_what_an_export_would_contain(tmp_path: Path) -> None:
    store = _store_with_asset(tmp_path, "src")
    app = create_app(market_data_store=store, auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/data-transfer/manifest")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["counts"]["backtest_history"] == 1
    assert data["total"] == 1


def test_export_streams_a_zip_that_can_be_imported_back(tmp_path: Path) -> None:
    source = _store_with_asset(tmp_path, "src")
    app = create_app(market_data_store=source, auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/data-transfer/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert MANIFEST_NAME in archive.namelist()
        assert "backtest_history.jsonl" in archive.namelist()
        assert "daily_bars.jsonl" not in archive.namelist()


def test_import_merges_an_uploaded_archive(tmp_path: Path) -> None:
    source = _store_with_asset(tmp_path, "src")
    exporter = create_app(market_data_store=source, auto_sync_enabled=False)
    with TestClient(exporter) as client:
        archive = client.get("/api/v1/data-transfer/export").content

    target = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    importer = create_app(market_data_store=target, auto_sync_enabled=False)
    with TestClient(importer) as client:
        first = client.post(
            "/api/v1/data-transfer/import",
            files={"file": ("backup.zip", archive, "application/zip")},
        )
        second = client.post(
            "/api/v1/data-transfer/import",
            files={"file": ("backup.zip", archive, "application/zip")},
        )

    assert first.status_code == 200
    assert first.json()["data"]["imported_total"] == 1
    assert second.status_code == 200
    assert second.json()["data"]["imported_total"] == 0
    assert second.json()["data"]["skipped_total"] == 1
    assert len(target.list_backtest_history("condition_research")) == 1
    # 行情没有被搬过来
    assert target.read_bars("SH", "600000").empty


def test_import_rejects_a_file_that_is_not_a_backup(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    app = create_app(market_data_store=store, auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/data-transfer/import",
            files={"file": ("random.zip", b"definitely not a zip", "application/zip")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "data_import_rejected"
