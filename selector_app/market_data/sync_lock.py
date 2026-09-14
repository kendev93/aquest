"""Cross-process lease used by every writer of ETFQuest market data."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOCK_FILE_NAME = "market_data.sync.lock"
DEFAULT_LEASE_SECONDS = 180.0


class MarketDataSyncBusyError(RuntimeError):
    """Raised when another application owns the shared market-data lease."""

    def __init__(self, message: str, *, metadata: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.metadata = dict(metadata or {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata_is_stale(metadata: Mapping[str, object], lease_seconds: float) -> bool:
    raw = metadata.get("heartbeat_at")
    try:
        heartbeat = float(str(raw))
    except (TypeError, ValueError):
        return True
    return time.time() - heartbeat > lease_seconds


def _read_metadata(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


@dataclass
class SyncLockLease:
    """A held lock lease whose metadata can be refreshed during long work."""

    _lock_path: Path
    _token: str
    _metadata: dict[str, object]

    def snapshot(self) -> dict[str, object]:
        return dict(self._metadata)

    def heartbeat(self, *, phase: str | None = None, **updates: object) -> None:
        if phase is not None:
            updates["phase"] = phase
        updates["heartbeat_at"] = time.time()
        updates["heartbeat_iso"] = _now_iso()
        current = _read_metadata(self._lock_path)
        if current is None or current.get("token") != self._token:
            raise MarketDataSyncBusyError("共享行情同步锁已被其它进程接管", metadata=current)
        self._metadata = {**current, **updates}
        _write_metadata(self._lock_path, self._metadata)

    def release(self) -> None:
        current = _read_metadata(self._lock_path)
        if current is not None and current.get("token") != self._token:
            return
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> SyncLockLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _write_metadata(path: Path, metadata: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(metadata), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class MarketDataSyncLock:
    """Acquire an atomic, filesystem-backed lease beside a market database."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if not str(owner).strip():
            raise ValueError("同步锁 owner 不能为空")
        if lease_seconds <= 0:
            raise ValueError("同步锁租约时间必须大于 0")
        self.database_path = Path(database_path).expanduser()
        self.lock_path = self.database_path.with_name(LOCK_FILE_NAME)
        self.owner = str(owner).strip()
        self.lease_seconds = float(lease_seconds)

    def current(self) -> dict[str, object] | None:
        if not self.lock_path.exists():
            return None
        metadata = _read_metadata(self.lock_path)
        if metadata is not None and not _metadata_is_stale(metadata, self.lease_seconds):
            return metadata
        if metadata is None:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                return None
            if age <= self.lease_seconds:
                return {"owner": "unknown", "phase": "starting"}
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return metadata
        return None

    @contextmanager
    def acquire(self, *, job_id: str, phase: str) -> Iterator[SyncLockLease]:
        normalized_job_id = str(job_id).strip()
        normalized_phase = str(phase).strip()
        if not normalized_job_id or not normalized_phase:
            raise ValueError("同步锁 job_id 和 phase 不能为空")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        metadata: dict[str, object] = {
            "token": token,
            "owner": self.owner,
            "job_id": normalized_job_id,
            "phase": normalized_phase,
            "started_at": _now_iso(),
            "heartbeat_at": time.time(),
            "heartbeat_iso": _now_iso(),
            "pid": os.getpid(),
        }
        for _attempt in range(2):
            current = self.current()
            if current is not None:
                raise MarketDataSyncBusyError(
                    "其他行情同步任务正在运行，请等待当前任务完成",
                    metadata=current,
                )
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                lease = SyncLockLease(self.lock_path, token, metadata)
                try:
                    yield lease
                finally:
                    lease.release()
                return
            except BaseException:
                current = _read_metadata(self.lock_path)
                if current is None or current.get("token") == token:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                raise
        current = self.current()
        raise MarketDataSyncBusyError(
            "其他行情同步任务正在运行，请等待当前任务完成",
            metadata=current,
        )


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "LOCK_FILE_NAME",
    "MarketDataSyncBusyError",
    "MarketDataSyncLock",
    "SyncLockLease",
]
