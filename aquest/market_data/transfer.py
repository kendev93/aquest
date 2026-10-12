"""Export and import of the records a user cannot regenerate.

The market database holds two very different kinds of data: bars and indicator
caches, which can always be re-synced or recomputed, and the user's own research
history, which cannot.  Both live in the same DuckDB file, so copying the file
is a wasteful way to protect the second kind — measured on a full installation
the assets are ~13 MB against a ~4 GB database.

This module therefore exports only the asset tables, as one zip a user can keep
anywhere, and can merge them back into another installation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from .store_base import SCHEMA_VERSION, MarketDataStoreError
from .store_history import ASSET_TABLES

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "aquest-backup"
EXPORT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
_TABLE_ENTRY_SUFFIX = ".jsonl"
_STREAM_BATCH = 200


class _AssetStore(Protocol):
    def status(self) -> Any: ...

    def history_table_columns(self, table: str) -> tuple[str, ...]: ...

    def iter_history_rows(
        self, table: str, *, batch_size: int = ...
    ) -> Iterator[dict[str, object]]: ...

    def insert_history_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, object]],
        *,
        batch_size: int = ...,
    ) -> tuple[int, int]: ...


@dataclass(frozen=True)
class ExportSummary:
    path: Path
    counts: dict[str, int]
    bytes_written: int
    schema_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "counts": dict(self.counts),
            "bytes_written": self.bytes_written,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ImportSummary:
    imported: dict[str, int]
    skipped: dict[str, int]

    @property
    def imported_total(self) -> int:
        return sum(self.imported.values())

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "imported": dict(self.imported),
            "skipped": dict(self.skipped),
            "imported_total": self.imported_total,
            "skipped_total": self.skipped_total,
        }


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    raise TypeError(f"无法序列化的值: {type(value).__name__}")


def _coerce_manifest_value(value: object) -> object:
    """Normalize a status field for the manifest.

    ``_json_default`` only handles types ``json.dumps`` cannot encode, so plain
    JSON values (including ``str``) must pass straight through.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _json_default(value)


def default_export_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"aquest-backup-{stamp}.zip"


def _resolve_destination(destination: str | Path, now: datetime | None) -> Path:
    target = Path(destination).expanduser()
    if target.is_dir() or str(destination).endswith(os.sep):
        return target / default_export_name(now)
    return target


def export_user_assets(
    store: _AssetStore,
    destination: str | Path,
    *,
    now: datetime | None = None,
) -> ExportSummary:
    """Write every user-asset table into one zip archive.

    The manifest is appended last on purpose: it carries the per-table row
    counts, which are only known once the table has been streamed.  Readers
    look the manifest up by name, so entry order does not matter.
    """

    target = _resolve_destination(destination, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial")

    counts: dict[str, int] = {}
    columns_by_table: dict[str, list[str]] = {}
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for table in ASSET_TABLES:
                columns = store.history_table_columns(table)
                columns_by_table[table] = list(columns)
                count = 0
                with archive.open(f"{table}{_TABLE_ENTRY_SUFFIX}", "w") as handle:
                    for row in store.iter_history_rows(table, batch_size=_STREAM_BATCH):
                        line = json.dumps(
                            row, ensure_ascii=False, separators=(",", ":"), default=_json_default
                        )
                        handle.write(line.encode("utf-8"))
                        handle.write(b"\n")
                        count += 1
                counts[table] = count

            manifest = _build_manifest(store, counts, columns_by_table, now)
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default),
            )
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    return ExportSummary(
        path=target,
        counts=counts,
        bytes_written=target.stat().st_size,
        schema_version=SCHEMA_VERSION,
    )


def _build_manifest(
    store: _AssetStore,
    counts: Mapping[str, int],
    columns_by_table: Mapping[str, list[str]],
    now: datetime | None,
) -> dict[str, object]:
    created = now or datetime.now()
    manifest: dict[str, object] = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": created.isoformat(),
        "schema_version": SCHEMA_VERSION,
        "counts": dict(counts),
        "columns": {table: list(columns) for table, columns in columns_by_table.items()},
    }
    try:
        status = store.status()
    except Exception:  # pragma: no cover - 备份不应因状态读取失败而中断
        logger.warning("导出时无法读取行情库状态", exc_info=True)
        return manifest
    manifest["source"] = {
        "bar_count": getattr(status, "bar_count", None),
        "instrument_count": getattr(status, "instrument_count", None),
        "data_start": _coerce_manifest_value(getattr(status, "data_start", None)),
        "data_end": _coerce_manifest_value(getattr(status, "data_end", None)),
    }
    return manifest


def read_manifest(source: str | Path) -> dict[str, object]:
    """Read and validate a backup manifest without importing anything."""

    try:
        with zipfile.ZipFile(Path(source).expanduser()) as archive:
            try:
                raw = archive.read(MANIFEST_NAME)
            except KeyError as exc:
                raise MarketDataStoreError("备份文件缺少 manifest.json") from exc
    except zipfile.BadZipFile as exc:
        raise MarketDataStoreError("备份文件不是有效的 zip 归档") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketDataStoreError("备份 manifest.json 内容无效") from exc
    if not isinstance(manifest, dict):
        raise MarketDataStoreError("备份 manifest.json 内容无效")
    _validate_manifest(manifest)
    return cast(dict[str, object], manifest)


def _validate_manifest(manifest: Mapping[str, object]) -> None:
    if manifest.get("format") != EXPORT_FORMAT:
        raise MarketDataStoreError("不是A股策略研究的备份文件")
    version = manifest.get("format_version")
    if version != EXPORT_FORMAT_VERSION:
        raise MarketDataStoreError(f"备份格式版本不兼容: {version}")
    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, int) and not isinstance(schema_version, bool):
        if schema_version > SCHEMA_VERSION:
            raise MarketDataStoreError(
                f"备份来自更新的版本（schema {schema_version} > {SCHEMA_VERSION}），请先升级本程序"
            )


def _iter_jsonl(archive: zipfile.ZipFile, entry: str) -> Iterator[dict[str, object]]:
    with archive.open(entry) as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MarketDataStoreError(f"备份条目 {entry} 内容损坏") from exc
            if not isinstance(record, dict):
                raise MarketDataStoreError(f"备份条目 {entry} 记录格式无效")
            yield cast(dict[str, object], record)


def import_user_assets(store: _AssetStore, source: str | Path) -> ImportSummary:
    """Merge a backup into this installation.

    Rows whose key already exists are skipped, so importing the same archive
    twice changes nothing.  Nothing is ever deleted or overwritten.
    """

    archive_path = Path(source).expanduser()
    if not archive_path.is_file():
        raise MarketDataStoreError(f"备份文件不存在: {archive_path}")
    manifest = read_manifest(archive_path)
    declared_columns = manifest.get("columns")
    declared = declared_columns if isinstance(declared_columns, dict) else {}

    imported: dict[str, int] = {}
    skipped: dict[str, int] = {}
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        for table in ASSET_TABLES:
            entry = f"{table}{_TABLE_ENTRY_SUFFIX}"
            if entry not in names:
                continue
            _ensure_columns_compatible(store, table, declared.get(table))
            rows, attempted = store.insert_history_rows(
                table, _iter_jsonl(archive, entry), batch_size=_STREAM_BATCH
            )
            imported[table] = rows
            skipped[table] = max(0, attempted - rows)
    return ImportSummary(imported=imported, skipped=skipped)


def _ensure_columns_compatible(store: _AssetStore, table: str, declared: object) -> None:
    """Fail early when a backup carries columns this schema does not have."""

    if not isinstance(declared, list):
        return
    available = set(store.history_table_columns(table))
    unknown = sorted(str(name) for name in declared if str(name) not in available)
    if unknown:
        raise MarketDataStoreError(
            f"备份中的 {table} 含有当前版本不认识的字段: {', '.join(unknown)}"
        )


__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "ExportSummary",
    "ImportSummary",
    "MANIFEST_NAME",
    "default_export_name",
    "export_user_assets",
    "import_user_assets",
    "read_manifest",
]
