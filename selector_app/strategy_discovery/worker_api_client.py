"""Authenticated HTTP client used by the isolated strategy worker."""

from __future__ import annotations

import json
import math
import os
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_CANCEL_POLL_SECONDS = 0.5
_RETRYABLE_WORKER_STATUSES = frozenset({500, 502, 503, 504})
_WORKER_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5)
JsonRequester = Callable[
    [str, Mapping[str, object], Mapping[str, str], float], Mapping[str, object]
]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


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
    ) -> None:
        parsed_url = urlsplit(base_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("策略 worker API 地址无效")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError("策略 worker API 地址不能包含凭据")
        self.worker_id = _validate_worker_id(worker_id)
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
        self._last_cancel_check = 0.0

    @classmethod
    def from_env(cls) -> HttpStrategyJobClient:
        base_url = os.getenv("SELECTOR_STRATEGY_WORKER_API_URL", "http://backend:8123")
        token_file = os.getenv("SELECTOR_STRATEGY_WORKER_TOKEN_FILE", "")
        if not token_file:
            raise ValueError("未配置策略 worker token 文件")
        worker_id = os.getenv("SELECTOR_STRATEGY_WORKER_ID") or socket.gethostname()
        return cls(base_url, token_file, worker_id)

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
            {"worker_id": self.worker_id},
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
            self._last_cancel_check = time.monotonic()
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
            if time.monotonic() - self._last_cancel_check < _CANCEL_POLL_SECONDS:
                return self._cancel_requested
            response = self._post(
                f"/internal/strategy-worker/jobs/{job_id}/heartbeat",
                {"worker_id": self.worker_id},
            )
            data = _mapping(response.get("data"), "心跳")
            state = data.get("state")
            if isinstance(state, Mapping):
                self._state = dict(state)
            self._cancel_requested = data.get("cancel_requested") is True
            self._last_cancel_check = time.monotonic()
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
        response = self._post(f"/internal/strategy-worker/jobs/{job_id}/{endpoint}", payload)
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

    def write_pool_entry(self, job_id: str, entry: Mapping[str, object]) -> None:
        self._post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-entry",
            {"worker_id": self.worker_id, "entry": dict(entry)},
        )

    def _store_server_state(self, job_id: str, data: Mapping[str, object]) -> None:
        state = data.get("state")
        with self._lock:
            if job_id != self._job_id:
                return
            if isinstance(state, Mapping):
                self._state = dict(state)
            self._cancel_requested = data.get("cancel_requested") is True
            self._last_cancel_check = time.monotonic()

    def _post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        retryable_update = (
            path.endswith("/heartbeat")
            or path.endswith("/progress")
            or path.endswith("/pool-entry")
        )
        for attempt in range(len(_WORKER_RETRY_DELAYS_SECONDS) + 1):
            try:
                decoded = self._requester(
                    f"{self._base_url}{path}",
                    payload,
                    {
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                    self._timeout_seconds,
                )
            except HTTPError as exc:
                if (
                    retryable_update
                    and exc.code in _RETRYABLE_WORKER_STATUSES
                    and attempt < len(_WORKER_RETRY_DELAYS_SECONDS)
                ):
                    time.sleep(_WORKER_RETRY_DELAYS_SECONDS[attempt])
                    continue
                raise RuntimeError("策略 worker API 请求失败") from exc
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if retryable_update and attempt < len(_WORKER_RETRY_DELAYS_SECONDS):
                    time.sleep(_WORKER_RETRY_DELAYS_SECONDS[attempt])
                    continue
                raise RuntimeError("策略 worker API 请求失败") from exc
            if not isinstance(decoded, Mapping):
                raise RuntimeError("策略 worker API 响应格式无效")
            return {str(key): value for key, value in decoded.items()}
        raise RuntimeError("策略 worker API 请求失败")

    def _clear_job(self) -> None:
        self._job_id = None
        self._request = None
        self._state = None
        self._result = None
        self._cancel_requested = False
        self._last_cancel_check = 0.0


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


__all__ = ["HttpStrategyJobClient"]
