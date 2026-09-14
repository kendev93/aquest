from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from selector_app.membership import (
    ETFQuestMembershipClient,
    MembershipAccessError,
    MembershipUnavailableError,
)


def _write_identity_database(
    path: Path,
    *,
    machine_id: str = "MID-001",
    secret: str = "secret",
) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE system_settings ("
            "key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.executemany(
            "INSERT INTO system_settings VALUES (?, ?, CURRENT_TIMESTAMP)",
            [
                ("machine_id", machine_id),
                ("machine_id_algorithm_version", "hardware_uuid_or_mac_md5_v1"),
                ("client_secret", secret),
            ],
        )


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_membership_client_signs_request_and_redacts_credentials(monkeypatch, tmp_path: Path):
    database = tmp_path / "market_data.db"
    _write_identity_database(database)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return _Response(
            {
                "data": {
                    "level": "trial",
                    "is_vip": True,
                    "can_access_core": True,
                    "access_state": "trial",
                    "message": "试用期有效",
                    "expire_at": "2099-01-01T00:00:00+00:00",
                }
            }
        )

    monkeypatch.setattr("selector_app.membership.urllib.request.urlopen", fake_urlopen)
    client = ETFQuestMembershipClient(database, server_url="https://license.test")

    status = client.check_access()

    assert status.can_access_core is True
    assert status.level == "trial"
    assert captured["url"] == "https://license.test/my/status"
    assert captured["headers"]["X-machine-id"] == "MID-001"
    assert "client_secret" not in str(status.to_dict())
    assert "license_key" not in str(status.to_dict())
    assert "machine_id" not in status.to_dict()


def test_membership_client_rejects_missing_machine_identity(tmp_path: Path):
    database = tmp_path / "market_data.db"
    _write_identity_database(database, machine_id="")

    with pytest.raises(MembershipUnavailableError, match="升级并启动最新版 ETFQuest"):
        ETFQuestMembershipClient(database).check_access()


def test_membership_client_rejects_expired_core_access(monkeypatch, tmp_path: Path):
    database = tmp_path / "market_data.db"
    _write_identity_database(database)
    monkeypatch.setattr(
        "selector_app.membership.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "data": {
                    "level": "free",
                    "is_vip": False,
                    "can_access_core": False,
                    "message": "会员已到期",
                }
            }
        ),
    )

    client = ETFQuestMembershipClient(database)
    with pytest.raises(MembershipAccessError, match="会员已到期"):
        client.require_core_access()
