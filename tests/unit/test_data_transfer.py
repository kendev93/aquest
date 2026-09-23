"""User-asset export/import: what travels, what does not, and idempotency."""

from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import SCHEMA_VERSION, MarketDataStoreError
from selector_app.market_data.store_history import ASSET_TABLES
from selector_app.market_data.transfer import (
    EXPORT_FORMAT,
    EXPORT_FORMAT_VERSION,
    MANIFEST_NAME,
    _json_default,
    export_user_assets,
    import_user_assets,
    read_manifest,
)


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


def _seed(store: DuckDbMarketDataStore, tag: str) -> None:
    store.replace_local_bars("SH", "600000", "stock", _bars())
    store.save_backtest_history(
        f"bt-{tag}",
        backtest_type="condition_research",
        request={"code": "600000", "tag": tag},
        result={"net_profit": 123.5, "tag": tag},
        started_at="2024-01-01T00:00:00",
        finished_at="2024-01-01T00:05:00",
    )


def test_asset_tables_never_include_market_or_cache_data() -> None:
    """行情和指标缓存必须留在外面：它们可以重新同步/重算，不该撑大备份。"""

    assert "daily_bars" not in ASSET_TABLES
    assert "indicator_cache" not in ASSET_TABLES
    assert "custom_indicator_cache" not in ASSET_TABLES
    assert "instruments" not in ASSET_TABLES
    assert set(ASSET_TABLES) == {
        "backtest_history",
        "screen_history",
        "saved_strategies",
        "strategy_tracking",
        "strategy_discovery_history",
        "strategy_discovery_replay_history",
        "screen_presets",
        "candidate_library",
        "stock_groups",
        "stock_group_criteria",
    }


def test_export_contains_only_asset_entries(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "src.duckdb")
    _seed(store, "alpha")

    summary = export_user_assets(store, tmp_path / "backup.zip")

    with zipfile.ZipFile(summary.path) as archive:
        names = set(archive.namelist())
    assert names == {MANIFEST_NAME, *(f"{table}.jsonl" for table in ASSET_TABLES)}
    assert summary.counts["backtest_history"] == 1
    assert summary.schema_version == SCHEMA_VERSION


def test_round_trip_moves_assets_and_leaves_market_data_behind(tmp_path: Path) -> None:
    source = DuckDbMarketDataStore(tmp_path / "src.duckdb")
    _seed(source, "alpha")
    archive = export_user_assets(source, tmp_path / "backup.zip").path

    target = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    summary = import_user_assets(target, archive)

    assert summary.imported_total == 1
    assert summary.skipped_total == 0
    assert len(target.list_backtest_history("condition_research")) == 1
    # 行情不在备份里，目标库仍然是空的
    assert target.read_bars("SH", "600000").empty
    assert source.read_bars("SH", "600000").shape[0] == 2


def test_importing_the_same_archive_twice_changes_nothing(tmp_path: Path) -> None:
    source = DuckDbMarketDataStore(tmp_path / "src.duckdb")
    _seed(source, "alpha")
    archive = export_user_assets(source, tmp_path / "backup.zip").path

    target = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    first = import_user_assets(target, archive)
    second = import_user_assets(target, archive)

    assert first.imported_total == 1
    assert second.imported_total == 0
    assert second.skipped_total == 1
    assert len(target.list_backtest_history("condition_research")) == 1


def test_import_merges_rather_than_replaces(tmp_path: Path) -> None:
    source = DuckDbMarketDataStore(tmp_path / "src.duckdb")
    _seed(source, "alpha")
    archive = export_user_assets(source, tmp_path / "backup.zip").path

    target = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    _seed(target, "beta")
    summary = import_user_assets(target, archive)

    rows = target.list_backtest_history("condition_research")
    assert summary.imported_total == 1
    assert {row["history_id"] for row in rows} == {"bt-alpha", "bt-beta"}


def test_empty_database_exports_an_empty_but_valid_archive(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "empty.duckdb")

    summary = export_user_assets(store, tmp_path / "backup.zip")

    assert set(summary.counts.values()) == {0}
    assert read_manifest(summary.path)["format"] == EXPORT_FORMAT


def test_export_does_not_leave_a_partial_file_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "src.duckdb")
    _seed(store, "alpha")

    def _boom(_table: str, **_kwargs: object):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(store, "iter_history_rows", _boom)
    target = tmp_path / "backup.zip"

    with pytest.raises(RuntimeError):
        export_user_assets(store, target)

    assert not target.exists()
    assert list(tmp_path.glob("*.partial")) == []


class TestManifest:
    def test_reports_counts_and_source(self, tmp_path: Path) -> None:
        store = DuckDbMarketDataStore(tmp_path / "src.duckdb")
        _seed(store, "alpha")
        archive = export_user_assets(store, tmp_path / "backup.zip").path

        manifest = read_manifest(archive)

        assert manifest["format"] == EXPORT_FORMAT
        assert manifest["format_version"] == EXPORT_FORMAT_VERSION
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["counts"]["backtest_history"] == 1
        assert manifest["source"]["bar_count"] == 2
        assert manifest["source"]["data_start"] == "2024-01-01"

    def test_rejects_a_zip_that_is_not_a_backup(self, tmp_path: Path) -> None:
        foreign = tmp_path / "foreign.zip"
        with zipfile.ZipFile(foreign, "w") as archive:
            archive.writestr("hello.txt", "not a backup")

        with pytest.raises(MarketDataStoreError, match="manifest"):
            read_manifest(foreign)

    def test_rejects_a_backup_from_a_newer_schema(self, tmp_path: Path) -> None:
        archive = tmp_path / "future.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format": EXPORT_FORMAT,
                        "format_version": EXPORT_FORMAT_VERSION,
                        "schema_version": SCHEMA_VERSION + 1,
                    }
                ),
            )

        with pytest.raises(MarketDataStoreError, match="更新的版本"):
            read_manifest(archive)

    def test_rejects_an_unknown_format_version(self, tmp_path: Path) -> None:
        archive = tmp_path / "odd.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                MANIFEST_NAME,
                json.dumps(
                    {
                        "format": EXPORT_FORMAT,
                        "format_version": EXPORT_FORMAT_VERSION + 1,
                        "schema_version": SCHEMA_VERSION,
                    }
                ),
            )

        with pytest.raises(MarketDataStoreError, match="格式版本不兼容"):
            read_manifest(archive)


def test_import_rejects_columns_this_schema_does_not_have(tmp_path: Path) -> None:
    archive = tmp_path / "alien.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            MANIFEST_NAME,
            json.dumps(
                {
                    "format": EXPORT_FORMAT,
                    "format_version": EXPORT_FORMAT_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "columns": {"backtest_history": ["history_id", "from_the_future"]},
                }
            ),
        )
        bundle.writestr("backtest_history.jsonl", "")

    store = DuckDbMarketDataStore(tmp_path / "dst.duckdb")
    with pytest.raises(MarketDataStoreError, match="不认识的字段"):
        import_user_assets(store, archive)


def test_import_rejects_a_missing_file(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "dst.duckdb")

    with pytest.raises(MarketDataStoreError, match="不存在"):
        import_user_assets(store, tmp_path / "nope.zip")


def test_export_rejects_non_asset_tables(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "src.duckdb")

    with pytest.raises(MarketDataStoreError, match="不支持的资产表"):
        store.history_table_columns("daily_bars")
    with pytest.raises(MarketDataStoreError, match="不支持的资产表"):
        list(store.iter_history_rows("daily_bars"))
    with pytest.raises(MarketDataStoreError, match="不支持的资产表"):
        store.insert_history_rows("daily_bars", [])
    with pytest.raises(MarketDataStoreError, match="不支持的资产表"):
        store.history_table_columns("t; DROP TABLE daily_bars")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05"),
        (date(2024, 1, 2), "2024-01-02"),
        (timedelta(seconds=90), 90.0),
        (Decimal("1.25"), 1.25),
        (b"\x00\x01", "AAE="),
    ],
)
def test_json_default_encodes_duckdb_types(value: object, expected: object) -> None:
    assert _json_default(value) == expected


def test_json_default_refuses_unknown_types() -> None:
    with pytest.raises(TypeError):
        _json_default(object())
