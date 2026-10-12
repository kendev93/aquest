"""Safe maintenance for the application-owned DuckDB database."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import duckdb

logger = logging.getLogger(__name__)

BACKUP_RETENTION_DAYS = 3
BACKUP_RETENTION_DAYS_ENV = "AQUEST_DB_BACKUP_RETENTION_DAYS"
COMPACTION_GROWTH_RATIO = 0.5
COMPACTION_MIN_SIZE_BYTES = 512 * 1024 * 1024
INITIAL_COMPACTION_FREE_RATIO = 0.2
MAINTENANCE_RECORD_VERSION = 1

MaintenanceStatus = Literal["skipped", "baseline", "completed", "failed"]


class MaintenanceJobRunner(Protocol):
    def has_active_jobs(self) -> bool: ...


@dataclass(frozen=True)
class MaintenanceResult:
    """Result of one cleanup/compaction check, safe to expose in logs or APIs."""

    status: MaintenanceStatus
    reason: str
    before_size_bytes: int | None
    after_size_bytes: int | None
    trigger: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float
    record_path: str
    backup_path: str | None = None
    deleted_backups: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "before_size_bytes": self.before_size_bytes,
            "after_size_bytes": self.after_size_bytes,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "record_path": self.record_path,
            "backup_path": self.backup_path,
            "deleted_backups": list(self.deleted_backups),
            "error": self.error,
        }


@dataclass(frozen=True)
class _DatabaseSnapshot:
    table_counts: Mapping[str, int]
    schema_version: str | None
    free_ratio: float


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _sql_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _record_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.stem}.maintenance.json")


def _backup_pattern(database_path: Path) -> str:
    return f"{database_path.stem}.bak-*{database_path.suffix}"


def cleanup_database_backups(
    database_path: str | Path,
    *,
    retention_days: int = BACKUP_RETENTION_DAYS,
    now: datetime | None = None,
) -> tuple[Path, ...]:
    """Keep at most the newest backup, and remove it after the retention window."""

    if isinstance(retention_days, bool) or retention_days < 0:
        raise ValueError("数据库备份保留天数不能为负数")
    path = Path(database_path).expanduser()
    candidates: list[tuple[Path, int]] = []
    for backup in path.parent.glob(_backup_pattern(path)):
        try:
            if backup.is_file():
                candidates.append((backup, backup.stat().st_mtime_ns))
        except OSError:
            continue
    if not candidates:
        return ()
    newest, newest_mtime_ns = max(candidates, key=lambda item: (item[1], item[0].name))
    cutoff = _utc_now(now).timestamp() - retention_days * 24 * 60 * 60
    keep_newest = newest if newest_mtime_ns / 1_000_000_000 >= cutoff else None
    removed: list[Path] = []
    for backup, _mtime_ns in candidates:
        if backup == keep_newest:
            continue
        try:
            backup.unlink()
            removed.append(backup)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("清理过期 DuckDB 备份失败: %s", backup, exc_info=True)
    return tuple(sorted(removed, key=lambda item: item.name))


def create_database_backup(
    database_path: str | Path,
    *,
    retention_days: int = BACKUP_RETENTION_DAYS,
    now: datetime | None = None,
) -> Path:
    """Create a backup first, then clean only expired sibling backups."""

    path = Path(database_path).expanduser()
    target = _backup_target(path, now)
    shutil.copy2(path, target)
    created_timestamp = _utc_now(now).timestamp()
    os.utime(target, (created_timestamp, created_timestamp))
    cleanup_database_backups(path, retention_days=retention_days, now=now)
    return target


def _backup_target(database_path: Path, now: datetime | None) -> Path:
    timestamp = _utc_now(now).strftime("%Y%m%d%H%M%S%f")
    return database_path.with_name(f"{database_path.stem}.bak-{timestamp}{database_path.suffix}")


def swap_in_compacted_database(
    database_path: str | Path,
    temp_path: str | Path,
    *,
    validate: Callable[[Path], object],
    retention_days: int = BACKUP_RETENTION_DAYS,
    now: datetime | None = None,
) -> Path | None:
    """Replace the database with an already-validated compacted copy.

    The previous file is kept by *hard link* instead of a second full copy, so
    compaction peaks at two database sizes rather than three: the link costs no
    data blocks, and the swap then moves the live name onto the new inode while
    the backup name keeps the previous one alive.  The mtime is only stamped
    after the swap, because until then the two names share an inode.

    ``validate`` runs against the swapped-in file; if it raises, the previous
    file is copied back before the error propagates.

    Returns the retained backup path, or ``None`` when ``retention_days`` is 0,
    which keeps the backup only for the duration of the swap.
    """

    path = Path(database_path).expanduser()
    temp = Path(temp_path)
    backup_path = _backup_target(path, now)
    try:
        os.link(path, backup_path)
    except OSError:
        logger.info("文件系统不支持硬链接，回退为整库拷贝备份: %s", path)
        shutil.copy2(path, backup_path)

    os.replace(temp, path)
    try:
        validate(path)
    except (OSError, duckdb.Error, RuntimeError, ValueError):
        if backup_path.is_file():
            shutil.copy2(backup_path, path)
        raise

    if retention_days <= 0:
        backup_path.unlink(missing_ok=True)
        return None

    created_timestamp = _utc_now(now).timestamp()
    try:
        os.utime(backup_path, (created_timestamp, created_timestamp))
    except OSError:
        logger.warning("备份时间戳写入失败: %s", backup_path, exc_info=True)
    cleanup_database_backups(path, retention_days=retention_days, now=now)
    return backup_path


def resolve_backup_retention_days(raw: str | None = None) -> int:
    """Read the backup retention window.

    Zero is a valid value and means "keep a backup only for the duration of the
    compaction that created it" — the right default for single-user Docker
    installs, where a retained copy would otherwise double a multi-gigabyte
    database on disk for days.
    """

    value = raw if raw is not None else os.getenv(BACKUP_RETENTION_DAYS_ENV)
    text = str(value or "").strip()
    if not text:
        return BACKUP_RETENTION_DAYS
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{BACKUP_RETENTION_DAYS_ENV} 必须是整数天数") from exc
    if parsed < 0:
        raise ValueError(f"{BACKUP_RETENTION_DAYS_ENV} 不能为负数")
    return parsed


def should_compact(
    current_size_bytes: int,
    baseline_size_bytes: int | None,
    *,
    growth_ratio: float = COMPACTION_GROWTH_RATIO,
) -> bool:
    """Return whether the file grew at least ``growth_ratio`` since compaction."""

    if baseline_size_bytes is None or baseline_size_bytes <= 0:
        return False
    if growth_ratio < 0:
        raise ValueError("数据库整理增长阈值不能小于 0")
    return current_size_bytes >= baseline_size_bytes * (1.0 + growth_ratio)


class DatabaseMaintenanceService:
    """Clean backups and compact DuckDB through a validated fresh copy."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_days: int = BACKUP_RETENTION_DAYS,
        growth_ratio: float = COMPACTION_GROWTH_RATIO,
        minimum_size_bytes: int = COMPACTION_MIN_SIZE_BYTES,
        initial_free_ratio: float = INITIAL_COMPACTION_FREE_RATIO,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(retention_days, bool) or retention_days < 0:
            raise ValueError("数据库备份保留天数不能为负数")
        if growth_ratio < 0:
            raise ValueError("数据库整理增长阈值不能小于 0")
        if isinstance(minimum_size_bytes, bool) or minimum_size_bytes < 0:
            raise ValueError("数据库整理最小文件大小不能小于 0")
        if not 0 <= initial_free_ratio <= 1:
            raise ValueError("数据库初次整理空闲空间比例必须在 0 到 1 之间")
        self.database_path = Path(database_path).expanduser()
        self.retention_days = retention_days
        self.growth_ratio = float(growth_ratio)
        self.minimum_size_bytes = minimum_size_bytes
        self.initial_free_ratio = float(initial_free_ratio)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def record_path(self) -> Path:
        return _record_path(self.database_path)

    def cleanup_backups(self, *, now: datetime | None = None) -> tuple[Path, ...]:
        return cleanup_database_backups(
            self.database_path,
            retention_days=self.retention_days,
            now=now or self._clock(),
        )

    def run_if_needed(self) -> MaintenanceResult:
        """Run one safe maintenance check; failures never replace the source DB."""

        from .store_base import _WRITE_LOCK

        with _WRITE_LOCK:
            return self._run_if_needed_locked()

    def _run_if_needed_locked(self) -> MaintenanceResult:
        """Perform the check while serializing against application writers."""

        now = _utc_now(self._clock())
        deleted = self.cleanup_backups(now=now)
        if not self.database_path.is_file():
            return self._result(
                status="skipped",
                reason="database_missing",
                deleted_backups=deleted,
            )

        try:
            before_size = self.database_path.stat().st_size
            snapshot = self._database_snapshot(self.database_path)
        except (OSError, duckdb.Error, RuntimeError, ValueError) as exc:
            return self._failure(None, None, None, deleted, exc)

        record = self._load_record()
        record = self._recover_running_record(record, now)
        baseline = _positive_int(record.get("last_successful_after_size_bytes"))
        trigger: str | None = None
        if baseline is None:
            if (
                before_size >= self.minimum_size_bytes
                and snapshot.free_ratio >= self.initial_free_ratio
            ):
                trigger = "initial_fragmentation"
            else:
                self._write_record(
                    {
                        **record,
                        "version": MAINTENANCE_RECORD_VERSION,
                        "status": "baseline",
                        "baseline_size_bytes": before_size,
                        "last_checked_at": now.isoformat(),
                        "last_successful_after_size_bytes": None,
                    }
                )
                return self._result(
                    status="baseline",
                    reason="baseline_recorded",
                    before_size_bytes=before_size,
                    after_size_bytes=before_size,
                    deleted_backups=deleted,
                )
        elif should_compact(before_size, baseline, growth_ratio=self.growth_ratio):
            trigger = "size_growth_50_percent"

        if trigger is None:
            self._write_record(
                {
                    **record,
                    "version": MAINTENANCE_RECORD_VERSION,
                    "status": "checked",
                    "last_checked_at": now.isoformat(),
                    "current_size_bytes": before_size,
                    "last_successful_after_size_bytes": baseline,
                }
            )
            return self._result(
                status="skipped",
                reason="growth_threshold_not_reached",
                before_size_bytes=before_size,
                after_size_bytes=before_size,
                deleted_backups=deleted,
            )
        return self._compact(
            before_size_bytes=before_size,
            trigger=trigger,
            record=record,
            deleted_backups=deleted,
            now=now,
        )

    def _compact(
        self,
        *,
        before_size_bytes: int,
        trigger: str,
        record: Mapping[str, object],
        deleted_backups: tuple[Path, ...],
        now: datetime,
    ) -> MaintenanceResult:
        from .store_base import _WRITE_LOCK

        started = time.perf_counter()
        started_at = now.isoformat()
        try:
            available_bytes = shutil.disk_usage(self.database_path.parent).free
        except OSError as exc:
            return self._failure(
                before_size_bytes,
                None,
                trigger,
                deleted_backups,
                exc,
                started_at=started_at,
                finished_at=_utc_now(self._clock()).isoformat(),
                duration_seconds=max(0.0, time.perf_counter() - started),
            )
        if available_bytes < before_size_bytes * 2:
            # 压缩本身只需要再放下一份临时副本，但备份在文件系统不支持硬链接时
            # 会退化成整库拷贝，所以守卫按最坏情况留两份库大小的余量。
            finished_at = _utc_now(self._clock())
            self._write_record(
                {
                    **record,
                    "version": MAINTENANCE_RECORD_VERSION,
                    "status": "skipped",
                    "reason": "insufficient_free_space",
                    "last_checked_at": finished_at.isoformat(),
                    "before_size_bytes": before_size_bytes,
                    "last_successful_after_size_bytes": _positive_int(
                        record.get("last_successful_after_size_bytes")
                    ),
                    "trigger": trigger,
                    "available_free_space_bytes": available_bytes,
                }
            )
            return self._result(
                status="skipped",
                reason="insufficient_free_space",
                before_size_bytes=before_size_bytes,
                after_size_bytes=before_size_bytes,
                trigger=trigger,
                started_at=started_at,
                finished_at=finished_at.isoformat(),
                deleted_backups=deleted_backups,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )
        temp_path = self._temporary_path(now)
        backup_path: Path | None = None
        try:
            self._write_record(
                {
                    **record,
                    "version": MAINTENANCE_RECORD_VERSION,
                    "status": "running",
                    "started_at": started_at,
                    "before_size_bytes": before_size_bytes,
                    "trigger": trigger,
                    "temp_path": str(temp_path),
                }
            )
            with _WRITE_LOCK:
                source_snapshot = self._database_snapshot(self.database_path)
                self._copy_database(self.database_path, temp_path)
                copied_snapshot = self._database_snapshot(temp_path)
                if (
                    copied_snapshot.table_counts != source_snapshot.table_counts
                    or copied_snapshot.schema_version != source_snapshot.schema_version
                ):
                    raise RuntimeError("临时 DuckDB 校验失败：表行数或 schema 不一致")
                temp_size = temp_path.stat().st_size
                if temp_size >= before_size_bytes:
                    finished_at = _utc_now(self._clock())
                    self._write_record(
                        {
                            **record,
                            "version": MAINTENANCE_RECORD_VERSION,
                            "status": "completed",
                            "reason": "no_size_gain",
                            "last_checked_at": finished_at.isoformat(),
                            "last_compacted_at": finished_at.isoformat(),
                            "before_size_bytes": before_size_bytes,
                            "after_size_bytes": before_size_bytes,
                            "last_successful_after_size_bytes": before_size_bytes,
                            "trigger": trigger,
                            "duration_seconds": max(0.0, time.perf_counter() - started),
                        }
                    )
                    return self._result(
                        status="completed",
                        reason="no_size_gain",
                        before_size_bytes=before_size_bytes,
                        after_size_bytes=before_size_bytes,
                        trigger=trigger,
                        started_at=started_at,
                        finished_at=finished_at.isoformat(),
                        backup_path=None,
                        deleted_backups=deleted_backups,
                        duration_seconds=max(0.0, time.perf_counter() - started),
                    )
                backup_path = swap_in_compacted_database(
                    self.database_path,
                    temp_path,
                    validate=self._database_snapshot,
                    retention_days=self.retention_days,
                    now=now,
                )
                after_size = self.database_path.stat().st_size
            finished_at = _utc_now(self._clock())
            duration = max(0.0, time.perf_counter() - started)
            self._write_record(
                {
                    **record,
                    "version": MAINTENANCE_RECORD_VERSION,
                    "status": "completed",
                    "reason": "compacted",
                    "last_checked_at": finished_at.isoformat(),
                    "last_compacted_at": finished_at.isoformat(),
                    "before_size_bytes": before_size_bytes,
                    "after_size_bytes": after_size,
                    "last_successful_after_size_bytes": after_size,
                    "backup_path": str(backup_path) if backup_path else None,
                    "trigger": trigger,
                    "duration_seconds": duration,
                    "temp_path": None,
                }
            )
            return self._result(
                status="completed",
                reason="compacted",
                before_size_bytes=before_size_bytes,
                after_size_bytes=after_size,
                trigger=trigger,
                started_at=started_at,
                finished_at=finished_at.isoformat(),
                backup_path=str(backup_path) if backup_path else None,
                deleted_backups=deleted_backups,
                duration_seconds=duration,
            )
        except Exception as exc:  # noqa: BLE001 - maintenance must not take the app down
            finished_at = _utc_now(self._clock())
            duration = max(0.0, time.perf_counter() - started)
            self._write_record(
                {
                    **record,
                    "version": MAINTENANCE_RECORD_VERSION,
                    "status": "failed",
                    "reason": "maintenance_failed",
                    "last_checked_at": finished_at.isoformat(),
                    "failed_at": finished_at.isoformat(),
                    "before_size_bytes": before_size_bytes,
                    "last_successful_after_size_bytes": _positive_int(
                        record.get("last_successful_after_size_bytes")
                    ),
                    "trigger": trigger,
                    "duration_seconds": duration,
                    "error": str(exc)[:500],
                    "temp_path": None,
                }
            )
            logger.exception("DuckDB 整理失败: %s", self.database_path)
            return self._failure(
                before_size_bytes,
                None,
                trigger,
                deleted_backups,
                exc,
                started_at=started_at,
                finished_at=finished_at.isoformat(),
                duration_seconds=duration,
                backup_path=backup_path,
            )
        finally:
            try:
                if temp_path.is_file():
                    temp_path.unlink()
            except OSError:
                logger.warning("清理 DuckDB 临时整理文件失败: %s", temp_path, exc_info=True)

    def _copy_database(self, source_path: Path, target_path: Path) -> None:
        """Copy every schema object through DuckDB's database copy operation."""

        with duckdb.connect(str(source_path), read_only=True) as connection:
            databases = [
                str(row[0])
                for row in connection.execute(
                    "SELECT database_name FROM duckdb_databases()"
                ).fetchall()
            ]
            source_name = next(
                name for name in databases if name not in {"system", "temp", "compact"}
            )
            connection.execute(f"ATTACH {_sql_literal(target_path)} AS compact (READ_ONLY FALSE)")
            try:
                connection.execute(f"COPY FROM DATABASE {_sql_identifier(source_name)} TO compact")
            finally:
                connection.execute("DETACH compact")

    def _database_snapshot(self, path: Path) -> _DatabaseSnapshot:
        with duckdb.connect(str(path), read_only=True) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for table in tables:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {_sql_identifier(table)}"
                ).fetchone()
                counts[table] = int(row[0]) if row else 0
            schema_row = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'schema_version'"
            ).fetchone()
            size_row = connection.execute("PRAGMA database_size").fetchone()
        total_blocks = int(size_row[3]) if size_row and size_row[3] is not None else 0
        free_blocks = int(size_row[5]) if size_row and size_row[5] is not None else 0
        free_ratio = free_blocks / total_blocks if total_blocks > 0 else 0.0
        return _DatabaseSnapshot(
            table_counts=counts,
            schema_version=str(schema_row[0]) if schema_row else None,
            free_ratio=free_ratio,
        )

    def _temporary_path(self, now: datetime) -> Path:
        timestamp = now.strftime("%Y%m%d%H%M%S%f")
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f"{self.database_path.stem}.compact-{timestamp}-",
            suffix=self.database_path.suffix,
            dir=self.database_path.parent,
        )
        os.close(descriptor)
        path = Path(raw_path)
        path.unlink()
        return path

    def _load_record(self) -> dict[str, object]:
        try:
            if self.database_path.is_file():
                from .store_base import _WRITE_LOCK

                with (
                    _WRITE_LOCK,
                    duckdb.connect(str(self.database_path), read_only=False) as connection,
                ):
                    row = connection.execute(
                        "SELECT value FROM data_meta WHERE key = 'maintenance_state'"
                    ).fetchone()
                if row is not None:
                    payload = json.loads(str(row[0]))
                    return dict(payload) if isinstance(payload, Mapping) else {}
        except (OSError, duckdb.Error, json.JSONDecodeError):
            logger.warning("读取 DuckDB 维护状态失败: %s", self.database_path, exc_info=True)

        if not self.record_path.is_file():
            return {}
        try:
            legacy_payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(legacy_payload, Mapping):
            return {}
        record = dict(legacy_payload)
        self._write_record(record)
        logger.info("已将旧 DuckDB 维护状态迁入 data_meta")
        return record

    def _recover_running_record(
        self,
        record: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        if record.get("status") != "running":
            return record
        raw_temp = record.get("temp_path")
        if isinstance(raw_temp, str):
            temp_path = Path(raw_temp).expanduser()
            if (
                temp_path.parent == self.database_path.parent
                and temp_path.name.startswith(f"{self.database_path.stem}.compact-")
                and temp_path.suffix == self.database_path.suffix
            ):
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("清理上次未完成的 DuckDB 临时文件失败: %s", temp_path)
        return {
            **record,
            "status": "recovered",
            "recovered_at": now.isoformat(),
            "temp_path": None,
        }

    def _write_record(self, payload: Mapping[str, object]) -> None:
        serialized = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        updated_at = _utc_now(self._clock()).replace(tzinfo=None)
        from .store_base import _WRITE_LOCK

        with _WRITE_LOCK, duckdb.connect(str(self.database_path), read_only=False) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS data_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO data_meta (key, value, updated_at)
                VALUES ('maintenance_state', ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [serialized, updated_at],
            )

    def _result(
        self,
        *,
        status: MaintenanceStatus,
        reason: str,
        before_size_bytes: int | None = None,
        after_size_bytes: int | None = None,
        trigger: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_seconds: float = 0.0,
        backup_path: str | None = None,
        deleted_backups: tuple[Path, ...] = (),
        error: str | None = None,
    ) -> MaintenanceResult:
        return MaintenanceResult(
            status=status,
            reason=reason,
            before_size_bytes=before_size_bytes,
            after_size_bytes=after_size_bytes,
            trigger=trigger,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, float(duration_seconds)),
            record_path=f"{self.database_path}#data_meta.maintenance_state",
            backup_path=backup_path,
            deleted_backups=tuple(str(path) for path in deleted_backups),
            error=error,
        )

    def _failure(
        self,
        before_size_bytes: int | None,
        after_size_bytes: int | None,
        trigger: str | None,
        deleted_backups: tuple[Path, ...],
        error: Exception,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_seconds: float = 0.0,
        backup_path: Path | None = None,
    ) -> MaintenanceResult:
        return self._result(
            status="failed",
            reason="maintenance_failed",
            before_size_bytes=before_size_bytes,
            after_size_bytes=after_size_bytes,
            trigger=trigger,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            backup_path=str(backup_path) if backup_path else None,
            deleted_backups=deleted_backups,
            error=str(error)[:500],
        )


class DatabaseMaintenanceScheduler:
    """Check database maintenance only when local and remote jobs are idle."""

    def __init__(
        self,
        service: DatabaseMaintenanceService,
        runner: MaintenanceJobRunner,
        *,
        interval_seconds: float = 15 * 60,
        additional_busy_check: Callable[[], bool] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("数据库整理检查间隔必须大于 0")
        self._service = service
        self._runner = runner
        self._interval_seconds = float(interval_seconds)
        self._additional_busy_check = additional_busy_check

    async def check_once(self) -> MaintenanceResult | None:
        """Check in a worker thread so a large rewrite never blocks the event loop."""

        if self._runner.has_active_jobs() or (
            self._additional_busy_check is not None and self._additional_busy_check()
        ):
            return None
        return await asyncio.to_thread(self._service.run_if_needed)

    async def run(self) -> None:
        """Keep checking until the application lifespan cancels this task."""

        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                result = await self.check_once()
                if result is not None and result.status in {"completed", "failed"}:
                    logger.info(
                        "DuckDB 整理检查完成: status=%s reason=%s before=%s after=%s",
                        result.status,
                        result.reason,
                        result.before_size_bytes,
                        result.after_size_bytes,
                    )
            except Exception:  # noqa: BLE001 - maintenance must not stop the scheduler
                logger.exception("DuckDB 定期整理检查失败")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    "BACKUP_RETENTION_DAYS",
    "COMPACTION_GROWTH_RATIO",
    "COMPACTION_MIN_SIZE_BYTES",
    "DatabaseMaintenanceService",
    "DatabaseMaintenanceScheduler",
    "MaintenanceResult",
    "cleanup_database_backups",
    "create_database_backup",
    "should_compact",
]
