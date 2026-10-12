from __future__ import annotations

import json
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest

from aquest.strategy_discovery.worker_api_client import HttpStrategyJobClient


def test_http_strategy_job_client_claims_updates_cancels_and_completes(tmp_path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret-token\n", encoding="utf-8")
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    job = {
        "job_id": "job-1",
        "request": {"job_id": "job-1", "operation": "discovery", "payload": {}},
        "state": {"job_id": "job-1", "status": "running", "metadata": {}},
    }

    def handler(
        url: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        _timeout: float,
    ) -> Mapping[str, object]:
        assert headers["Authorization"] == "Bearer secret-token"
        path = urlsplit(url).path
        body = dict(payload)
        calls.append(("POST", path, body))
        if path.endswith("/claim"):
            return {"job": job}
        if path.endswith("/heartbeat"):
            return {"data": {"state": job["state"], "cancel_requested": True}}
        if path.endswith("/progress"):
            return {
                "data": {
                    "state": {**job["state"], **body},
                    "cancel_requested": False,
                }
            }
        if path.endswith("/complete"):
            return {"data": {"state": {**job["state"], "status": "completed"}}}
        raise AssertionError(f"unexpected URL: {url}")

    with HttpStrategyJobClient(
        "http://backend:8123",
        token_file,
        "worker-a",
        requester=handler,
    ) as client:
        assert client.claim_next() == "job-1"
        assert client.read_request("job-1")["operation"] == "discovery"
        assert client.read_state("job-1")["status"] == "running"
        client.update("job-1", phase="searching", progress=0.5)
        time.sleep(1.51)
        assert client.is_cancel_requested("job-1") is True
        client.write_result("job-1", {"history_id": "job-1"})
        assert client.read_state("job-1")["status"] == "completed"

    assert [call[1].rsplit("/", 1)[-1] for call in calls] == [
        "claim",
        "progress",
        "heartbeat",
        "complete",
    ]
    assert calls[-1][2] == {"worker_id": "worker-a", "result": {"history_id": "job-1"}}


def test_http_strategy_job_client_rejects_missing_or_weak_configuration(tmp_path) -> None:
    token_file = tmp_path / "empty.token"
    token_file.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="token"):
        HttpStrategyJobClient("http://backend:8123", token_file, "worker-a")
    with pytest.raises(ValueError, match="worker"):
        HttpStrategyJobClient("http://backend:8123", tmp_path / "missing", "")


def test_http_strategy_job_client_marks_worker_failure(tmp_path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret-token", encoding="utf-8")
    captured: list[dict[str, object]] = []

    def handler(
        _url: str,
        payload: Mapping[str, object],
        _headers: Mapping[str, str],
        _timeout: float,
    ) -> Mapping[str, object]:
        captured.append(dict(payload))
        return {"data": {"state": {"job_id": "job-1", "status": "failed"}}}

    with HttpStrategyJobClient(
        "http://backend:8123",
        token_file,
        "worker-a",
        requester=handler,
    ) as client:
        client.update(
            "job-1",
            status="failed",
            error="worker crashed",
            error_code="worker_failed",
        )

    assert captured == [
        {"worker_id": "worker-a", "error": "worker crashed", "error_code": "worker_failed"}
    ]


def test_default_worker_transport_posts_authenticated_json(tmp_path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret-token", encoding="utf-8")
    received: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received.append(
                {
                    "authorization": self.headers.get("authorization", ""),
                    "path": self.path,
                    "body": body,
                }
            )
            response = b'{"job":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with HttpStrategyJobClient(
            f"http://127.0.0.1:{server.server_port}",
            token_file,
            "worker-a",
        ) as client:
            assert client.claim_next() is None
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert received[0]["authorization"] == "Bearer secret-token"
    assert urlsplit(received[0]["path"]).path == "/internal/strategy-worker/jobs/claim"
    assert json.loads(received[0]["body"]) == {"worker_id": "worker-a"}


def test_cancel_poll_uses_cached_state_after_transient_api_timeouts(tmp_path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret-token", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "request": {"job_id": "job-1", "operation": "discovery", "payload": {}},
        "state": {"job_id": "job-1", "status": "running", "metadata": {}},
    }
    heartbeat_attempts = 0
    should_fail = False

    def handler(
        url: str,
        _payload: Mapping[str, object],
        _headers: Mapping[str, str],
        _timeout: float,
    ) -> Mapping[str, object]:
        nonlocal heartbeat_attempts
        path = urlsplit(url).path
        if path.endswith("/claim"):
            return {"job": job}
        if path.endswith("/heartbeat"):
            heartbeat_attempts += 1
            if should_fail:
                raise TimeoutError("backend response timed out")
            return {"data": {"state": job["state"], "cancel_requested": True}}
        raise AssertionError(f"unexpected URL: {url}")

    with HttpStrategyJobClient(
        "http://backend:8123",
        token_file,
        "worker-a",
        requester=handler,
    ) as client:
        assert client.claim_next() == "job-1"
        client._next_cancel_check_at = 0
        assert client.is_cancel_requested("job-1") is True

        should_fail = True
        client._next_cancel_check_at = 0
        assert client.is_cancel_requested("job-1") is True
        attempts_after_failure = heartbeat_attempts
        assert attempts_after_failure > 1

        # An outage should be throttled instead of triggering another retry storm.
        assert client.is_cancel_requested("job-1") is True
        assert heartbeat_attempts == attempts_after_failure


def test_cancel_poll_does_not_hold_client_lock_during_network_io(tmp_path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("secret-token", encoding="utf-8")
    job = {
        "job_id": "job-1",
        "request": {"job_id": "job-1", "operation": "discovery", "payload": {}},
        "state": {"job_id": "job-1", "status": "running", "metadata": {}},
    }
    request_started = Event()
    release_request = Event()
    state_updated = Event()
    request_results: list[bool] = []

    def handler(
        url: str,
        _payload: Mapping[str, object],
        _headers: Mapping[str, str],
        _timeout: float,
    ) -> Mapping[str, object]:
        path = urlsplit(url).path
        if path.endswith("/claim"):
            return {"job": job}
        if path.endswith("/heartbeat"):
            request_started.set()
            if not release_request.wait(timeout=3):
                raise TimeoutError("test request release timed out")
            return {"data": {"state": job["state"], "cancel_requested": False}}
        raise AssertionError(f"unexpected URL: {url}")

    with HttpStrategyJobClient(
        "http://backend:8123",
        token_file,
        "worker-a",
        requester=handler,
    ) as client:
        assert client.claim_next() == "job-1"
        client._next_cancel_check_at = 0
        poller = Thread(
            target=lambda: request_results.append(client.is_cancel_requested("job-1")),
            daemon=True,
        )
        poller.start()
        assert request_started.wait(timeout=1)

        def update_state() -> None:
            client._store_server_state("job-1", {"cancel_requested": True})
            state_updated.set()

        updater = Thread(
            target=update_state,
            daemon=True,
        )
        updater.start()
        try:
            assert state_updated.wait(timeout=0.2)
        finally:
            release_request.set()
            poller.join(timeout=2)
            updater.join(timeout=2)

    assert request_results == [True]
