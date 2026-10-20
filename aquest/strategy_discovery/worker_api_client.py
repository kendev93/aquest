"""Authenticated HTTP client used by the isolated strategy worker."""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_CANCEL_POLL_SECONDS = 1.5
_CANCEL_FAILURE_BACKOFF_SECONDS = 3.0
_CANCEL_REQUEST_TIMEOUT_SECONDS = 3.0
_RETRYABLE_WORKER_STATUSES = frozenset({500, 502, 503, 504})
_WORKER_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0)
logger = logging.getLogger(__name__)
JsonRequester = Callable[
    [str, Mapping[str, object], Mapping[str, str], float], Mapping[str, object]
]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class TransientWorkerApiError(RuntimeError):
    """A retryable worker API failure that the running task may ride through."""


class HttpStrategyJobClient:
    """Implement the worker's job-store contract through the backend API."""

    def __init__(
        self,
        base_url: str,
        token_file: str | Path,
        worker_id: str,
        *,
        timeout_seconds: float = 10.0,
        requester: JsonRequester | None = None,
        operations: list[str] | None = None,
    ) -> None:
        parsed_url = urlsplit(base_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("策略 worker API 地址无效")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError("策略 worker API 地址不能包含凭据")
        self.worker_id = _validate_worker_id(worker_id)
        self.operations = operations
        token_path = Path(token_file).expanduser()
        try:
            self._token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("策略 worker token 文件不可读") from exc
        if not self._token:
            raise ValueError("策略 worker token 不能为空")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("策略 worker API 超时时间必须大于 0")
        self._base_url = base_url.strip().rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._requester = requester or _request_json
        self._lock = RLock()
        self._job_id: str | None = None
        self._request: dict[str, object] | None = None
        self._state: dict[str, object] | None = None
        self._result: dict[str, object] | None = None
        self._cancel_requested = False
        self._next_cancel_check_at = 0.0
        self._cancel_check_in_flight = False
        self._last_cancel_warning_at = 0.0

    @classmethod
    def from_env(
        cls, *, operations: list[str] | None = None, suffix: str = ""
    ) -> HttpStrategyJobClient:
        base_url = os.getenv("AQUEST_STRATEGY_WORKER_API_URL", "http://backend:8123")
        token_file = os.getenv("AQUEST_STRATEGY_WORKER_TOKEN_FILE", "")
        if not token_file:
            raise ValueError("未配置策略 worker token 文件")
        worker_id = os.getenv("AQUEST_STRATEGY_WORKER_ID") or socket.gethostname()
        return cls(base_url, token_file, worker_id + suffix, operations=operations)

    def close(self) -> None:
        return None

    def __enter__(self) -> HttpStrategyJobClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def recover_stale_running(self, *, stale_after_seconds: float) -> list[str]:
        """Stale-job recovery belongs to the backend's single-writer process."""

        if stale_after_seconds <= 0:
            raise ValueError("策略任务 stale 时间必须大于 0")
        return []

    def claim_next(self) -> str | None:
        response = self._post(
            "/internal/strategy-worker/jobs/claim",
            {
                "worker_id": self.worker_id,
                **({"operations": self.operations} if self.operations else {}),
            },
        )
        raw_job = response.get("job")
        if raw_job is None:
            with self._lock:
                self._clear_job()
            return None
        if not isinstance(raw_job, Mapping):
            raise RuntimeError("策略 worker API 返回了无效任务")
        job_id = raw_job.get("job_id")
        request = raw_job.get("request")
        state = raw_job.get("state")
        if (
            not isinstance(job_id, str)
            or not isinstance(request, Mapping)
            or not isinstance(state, Mapping)
        ):
            raise RuntimeError("策略 worker API 返回了不完整任务")
        with self._lock:
            self._job_id = job_id
            self._request = dict(request)
            self._state = dict(state)
            self._result = None
            self._cancel_requested = bool(state.get("cancel_requested"))
            self._next_cancel_check_at = time.monotonic() + _CANCEL_POLL_SECONDS
            self._cancel_check_in_flight = False
        return job_id

    def read_request(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            if job_id != self._job_id or self._request is None:
                return None
            return dict(self._request)

    def read_state(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            if job_id != self._job_id or self._state is None:
                return None
            return dict(self._state)

    def read_result(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            if job_id != self._job_id or self._result is None:
                return None
            return dict(self._result)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            if job_id != self._job_id:
                return False
            now = time.monotonic()
            if self._cancel_check_in_flight or now < self._next_cancel_check_at:
                return self._cancel_requested
            self._cancel_check_in_flight = True
            self._next_cancel_check_at = now + _CANCEL_POLL_SECONDS

        try:
            response = self._post(
                f"/internal/strategy-worker/jobs/{job_id}/heartbeat",
                {"worker_id": self.worker_id},
                timeout_seconds=min(self._timeout_seconds, _CANCEL_REQUEST_TIMEOUT_SECONDS),
                max_attempts=2,
            )
            data = _mapping(response.get("data"), "心跳")
        except TransientWorkerApiError as exc:
            with self._lock:
                self._cancel_check_in_flight = False
                if job_id == self._job_id:
                    self._next_cancel_check_at = time.monotonic() + _CANCEL_FAILURE_BACKOFF_SECONDS
                    cancel_requested = self._cancel_requested
                else:
                    cancel_requested = False
                should_log = time.monotonic() - self._last_cancel_warning_at >= 30.0
                if should_log:
                    self._last_cancel_warning_at = time.monotonic()
            if should_log:
                logger.warning(
                    "策略 worker 暂时无法读取取消状态，沿用最近状态并继续重试: %s",
                    exc,
                )
            return cancel_requested
        except Exception:
            with self._lock:
                self._cancel_check_in_flight = False
                self._next_cancel_check_at = time.monotonic() + _CANCEL_POLL_SECONDS
            raise

        state = data.get("state")
        with self._lock:
            self._cancel_check_in_flight = False
            if job_id == self._job_id:
                if isinstance(state, Mapping):
                    self._state = dict(state)
                self._cancel_requested = (
                    self._cancel_requested or data.get("cancel_requested") is True
                )
                self._next_cancel_check_at = time.monotonic() + _CANCEL_POLL_SECONDS
            return self._cancel_requested

    def update(
        self,
        job_id: str,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> None:
        body = {**dict(fields or {}), **updates}
        status = body.pop("status", None)
        if status == "cancelled":
            endpoint = "cancelled"
            payload: dict[str, object] = {"worker_id": self.worker_id}
        elif status == "failed":
            endpoint = "fail"
            payload = {
                "worker_id": self.worker_id,
                "error": str(body.pop("error", "策略 worker 执行失败"))[:500],
                "error_code": str(body.pop("error_code", "worker_failed"))[:80],
            }
        elif status in {None, "running"}:
            endpoint = "progress"
            body.pop("heartbeat_at", None)
            payload = {"worker_id": self.worker_id, **body}
        else:
            raise ValueError("策略 worker 状态更新无效")
        progress_update = endpoint == "progress"
        response = self._post(
            f"/internal/strategy-worker/jobs/{job_id}/{endpoint}",
            payload,
            timeout_seconds=min(self._timeout_seconds, 2.5) if progress_update else None,
            max_attempts=2 if progress_update else None,
        )
        data = _mapping(response.get("data"), "状态更新")
        self._store_server_state(job_id, data)

    def write_result(self, job_id: str, result: Mapping[str, object]) -> None:
        response = self._post(
            f"/internal/strategy-worker/jobs/{job_id}/complete",
            {"worker_id": self.worker_id, "result": dict(result)},
        )
        data = _mapping(response.get("data"), "任务结果")
        self._store_server_state(job_id, data)
        with self._lock:
            self._result = dict(result)

    def write_pool_tracking(self, job_id: str, snapshot: Mapping[str, object]) -> dict[str, object]:
        response = self._post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-tracking",
            {"worker_id": self.worker_id, "entry": dict(snapshot)},
        )
        return dict(_mapping(response.get("data"), "跟踪保存结果"))

    def write_pool_tracking_many(
        self, job_id: str, snapshots: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        """一次上报多只跟踪快照，后端合成一个事务落库。"""

        response = self._post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-tracking/batch",
            {"worker_id": self.worker_id, "entries": [dict(item) for item in snapshots]},
        )
        data = _mapping(response.get("data"), "批量跟踪保存结果")
        saved = data.get("saved")
        if not isinstance(saved, list) or len(saved) != len(snapshots):
            raise ValueError("批量跟踪保存结果与上报条数不一致")
        return [{"saved": bool(flag)} for flag in saved]

    def write_pool_entry(self, job_id: str, entry: Mapping[str, object]) -> None:
        self._post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-entry",
            {"worker_id": self.worker_id, "entry": dict(entry)},
            timeout_seconds=min(self._timeout_seconds, 5.0),
        )

    def _store_server_state(self, job_id: str, data: Mapping[str, object]) -> None:
        state = data.get("state")
        with self._lock:
            if job_id != self._job_id:
                return
            if isinstance(state, Mapping):
                self._state = dict(state)
            self._cancel_requested = data.get("cancel_requested") is True
            self._next_cancel_check_at = time.monotonic() + _CANCEL_POLL_SECONDS

    def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, object]:
        retryable_update = (
            path.endswith("/heartbeat")
            or path.endswith("/progress")
            or path.endswith("/pool-entry")
        )
        attempt_limit = max_attempts or len(_WORKER_RETRY_DELAYS_SECONDS) + 1
        if attempt_limit < 1:
            raise ValueError("策略 worker API 请求次数必须大于 0")
        selected_timeout = timeout_seconds or self._timeout_seconds
        for attempt in range(attempt_limit):
            retry_delay = _WORKER_RETRY_DELAYS_SECONDS[
                min(attempt, len(_WORKER_RETRY_DELAYS_SECONDS) - 1)
            ]
            try:
                decoded = self._requester(
                    f"{self._base_url}{path}",
                    payload,
                    {
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                    selected_timeout,
                )
            except HTTPError as exc:
                if retryable_update and exc.code in _RETRYABLE_WORKER_STATUSES:
                    if attempt + 1 < attempt_limit:
                        time.sleep(retry_delay)
                        continue
                    raise TransientWorkerApiError(
                        f"策略 worker API 暂时失败（HTTP {exc.code}）"
                    ) from exc
                raise RuntimeError("策略 worker API 请求失败") from exc
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if retryable_update:
                    if attempt + 1 < attempt_limit:
                        time.sleep(retry_delay)
                        continue
                    raise TransientWorkerApiError("策略 worker API 响应超时或暂不可用") from exc
                raise RuntimeError("策略 worker API 请求失败") from exc
            if not isinstance(decoded, Mapping):
                raise RuntimeError("策略 worker API 响应格式无效")
            return {str(key): value for key, value in decoded.items()}
        raise TransientWorkerApiError("策略 worker API 暂时不可用")

    def _clear_job(self) -> None:
        self._job_id = None
        self._request = None
        self._state = None
        self._result = None
        self._cancel_requested = False
        self._next_cancel_check_at = 0.0
        self._cancel_check_in_flight = False


def _request_json(
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Mapping[str, object]:
    request = Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    opener = build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout_seconds) as response:
        decoded = json.loads(response.read())
    if not isinstance(decoded, Mapping):
        raise ValueError("策略 worker API 响应格式无效")
    return decoded


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"策略 worker API {label}响应格式无效")
    return value


def _validate_worker_id(value: str) -> str:
    selected = str(value).strip()
    if not selected or len(selected) > 128:
        raise ValueError("策略 worker ID 无效")
    return selected


__all__ = ["HttpStrategyJobClient", "TransientWorkerApiError"]
