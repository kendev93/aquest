"""Repository operations for imported source-file tracking."""

from __future__ import annotations

from pathlib import Path

from .models import InstrumentType
from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now


class DuckDbSourceFileStore:
    """Track source metadata without exposing filesystem writes to callers."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def source_file(self, source_root: str | Path, relative_path: str) -> dict[str, object] | None:
        root = str(Path(source_root).expanduser().resolve())
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT source_root, relative_path, market, code, instrument_type, "
                "size, mtime_ns, sha256, status, last_seen_at, last_imported_at, error "
                "FROM source_files WHERE source_root = ? AND relative_path = ?",
                [root, relative_path],
            ).fetchone()
        if row is None:
            return None
        keys = [
            "source_root",
            "relative_path",
            "market",
            "code",
            "instrument_type",
            "size",
            "mtime_ns",
            "sha256",
            "status",
            "last_seen_at",
            "last_imported_at",
            "error",
        ]
        return dict(zip(keys, row, strict=True))

    def source_files(self, source_root: str | Path) -> list[dict[str, object]]:
        root = str(Path(source_root).expanduser().resolve())
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT source_root, relative_path, market, code, instrument_type, "
                "size, mtime_ns, sha256, status, last_seen_at, last_imported_at, error "
                "FROM source_files WHERE source_root = ?",
                [root],
            ).fetchall()
        keys = [
            "source_root",
            "relative_path",
            "market",
            "code",
            "instrument_type",
            "size",
            "mtime_ns",
            "sha256",
            "status",
            "last_seen_at",
            "last_imported_at",
            "error",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def record_source_file(
        self,
        *,
        source_root: str | Path,
        relative_path: str,
        market: str,
        code: str,
        instrument_type: InstrumentType | None,
        size: int,
        mtime_ns: int,
        sha256: str | None,
        status: str,
        error: str | None = None,
        imported: bool = False,
    ) -> None:
        self._store._require_writer()
        root = str(Path(source_root).expanduser().resolve())
        now = _now()
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_files (
                    source_root, relative_path, market, code, instrument_type, size, mtime_ns,
                    sha256, status, last_seen_at, last_imported_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_root, relative_path) DO UPDATE SET
                    market = excluded.market,
                    code = excluded.code,
                    instrument_type = excluded.instrument_type,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    sha256 = excluded.sha256,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    last_imported_at = CASE
                        WHEN excluded.last_imported_at IS NOT NULL THEN excluded.last_imported_at
                        ELSE source_files.last_imported_at
                    END,
                    error = excluded.error
                """,
                [
                    root,
                    relative_path,
                    market,
                    code,
                    instrument_type,
                    size,
                    mtime_ns,
                    sha256,
                    status,
                    now,
                    now if imported else None,
                    error,
                ],
            )

    def mark_source_missing(self, source_root: str | Path, relative_path: str) -> None:
        self._store._require_writer()
        root = str(Path(source_root).expanduser().resolve())
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute(
                "UPDATE source_files SET status = 'missing', last_seen_at = ?, error = ? "
                "WHERE source_root = ? AND relative_path = ?",
                [_now(), "源文件不存在", root, relative_path],
            )
