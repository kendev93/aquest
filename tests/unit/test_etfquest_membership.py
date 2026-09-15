from __future__ import annotations

import base64
import json
import socket
import zlib
from pathlib import Path

import duckdb
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from selector_app.membership import (
    ETFQuestMembershipClient,
    MembershipAccessError,
    MembershipUnavailableError,
)


def _public_key_pem(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _signed_license(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    machine_id: str = "MID-001",
    exp: str = "2099-01-01",
    level: str = "vip",
) -> str:
    compressed = zlib.compress(
        json.dumps(
            {"mid": machine_id, "exp": exp, "level": level},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = private_key.sign(compressed, ec.ECDSA(hashes.SHA256()))
    return f"{base64.b64encode(compressed).decode()}.{base64.b64encode(signature).decode()}"


def _snapshot(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    machine_id: str = "MID-001",
    exp: str = "2099-01-01",
    expire_at: str = "2099-01-01T00:00:00Z",
    is_vip: bool = True,
    can_access_core: bool = True,
    force_update: bool = False,
    version: int = 3,
) -> dict[str, object]:
    return {
        "version": version,
        "level": "vip" if is_vip else "free",
        "is_vip": is_vip,
        "expire_at": expire_at,
        "exp": exp,
        "message": "会员已到期" if expire_at.startswith("2000") else "VIP 有效",
        "synced_at": "2026-09-14T00:00:00+00:00",
        "license_key": _signed_license(private_key, machine_id=machine_id, exp=exp),
        "is_trial": False,
        "trial_started_at": "",
        "trial_expires_at": "",
        "entitlement_source": "manual",
        "entitlement_required": True,
        "can_access_core": can_access_core,
        "force_update": force_update,
        "min_supported_version": "",
        "update_message": "请升级 ETFQuest" if force_update else "",
        "update_download_url": "",
    }


def _write_identity_database(
    path: Path,
    *,
    machine_id: str = "MID-001",
    snapshot: dict[str, object] | None = None,
    sync_state: str = "synced",
    write_secret_key: bool = True,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    secret_key = Fernet.generate_key()
    secret_key_path = path.parent.parent / ".secret.key"
    if write_secret_key:
        secret_key_path.write_bytes(secret_key)

    settings = [
        ("machine_id", machine_id),
        ("machine_id_algorithm_version", "hardware_uuid_or_mac_md5_v1"),
        ("server_membership_sync_state", sync_state),
        # Plain summary fields are intentionally stale/tampered. Access must
        # come from the Fernet-protected snapshot, never from these values.
        ("server_membership_level", "vip"),
        ("server_membership_is_vip", "1"),
        ("server_membership_expire_at", "2000-01-01T00:00:00Z"),
    ]
    if snapshot is not None:
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        encrypted_snapshot = (
            Fernet(secret_key).encrypt(snapshot_json.encode("utf-8")).decode("ascii")
        )
        settings.append(("server_membership_snapshot", encrypted_snapshot))

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE system_settings ("
            "key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.executemany(
            "INSERT INTO system_settings VALUES (?, ?, CURRENT_TIMESTAMP)",
            settings,
        )
    return secret_key_path


def _client(
    database: Path, private_key: ec.EllipticCurvePrivateKey, monkeypatch
) -> ETFQuestMembershipClient:
    monkeypatch.setattr(
        "selector_app.membership.MEMBERSHIP_LICENSE_PUBLIC_KEY_PEM",
        _public_key_pem(private_key),
    )
    return ETFQuestMembershipClient(database, cache_seconds=0)


def test_membership_client_reuses_verified_local_snapshot_without_network(
    monkeypatch, tmp_path: Path
):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(database, snapshot=_snapshot(private_key))
    client = _client(database, private_key, monkeypatch)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail(
            "local membership verification must not open a network connection"
        ),
    )

    status = client.require_core_access()

    assert status.can_access_core is True
    assert status.level == "vip"
    assert status.expire_at == "2099-01-01T00:00:00Z"
    assert status.access_state == "vip"
    assert "license_key" not in status.to_dict()
    assert "machine_id" not in status.to_dict()


def test_membership_client_ignores_unprotected_expiry_summary(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(database, snapshot=_snapshot(private_key))
    client = _client(database, private_key, monkeypatch)

    assert client.require_core_access().can_access_core is True


def test_membership_client_rejects_missing_machine_identity(tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    _write_identity_database(database, machine_id="")

    with pytest.raises(MembershipUnavailableError, match="未保存机器码"):
        ETFQuestMembershipClient(database).check_access()


def test_membership_client_requires_etfquest_decryption_key(tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(database, snapshot=_snapshot(private_key), write_secret_key=False)

    with pytest.raises(MembershipUnavailableError, match=r"\.secret\.key"):
        ETFQuestMembershipClient(database).check_access()


def test_membership_client_fails_closed_for_snapshot_without_sync_state(
    monkeypatch,
    tmp_path: Path,
):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(database, snapshot=_snapshot(private_key), sync_state="pending")
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="尚未完成同步"):
        client.check_access()


def test_membership_client_rejects_old_snapshot_version(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(
        database,
        snapshot=_snapshot(private_key, version=2),
    )
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="v3"):
        client.check_access()


def test_membership_client_rejects_snapshot_signed_for_another_machine(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(
        database,
        machine_id="MID-OTHER",
        snapshot=_snapshot(private_key, machine_id="MID-001"),
    )
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="签名或设备校验"):
        client.check_access()


def test_membership_client_rejects_expired_snapshot(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(
        database,
        snapshot=_snapshot(
            private_key,
            exp="2000-01-01",
            expire_at="2000-01-01T00:00:00Z",
        ),
    )
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipAccessError, match="会员已到期"):
        client.require_core_access()


def test_membership_client_denies_snapshot_policy_and_forced_update(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(
        database,
        snapshot=_snapshot(private_key, can_access_core=False),
    )
    client = _client(database, private_key, monkeypatch)
    with pytest.raises(MembershipAccessError, match="VIP 有效"):
        client.require_core_access()

    force_update_database = tmp_path / "ETFQuest-force-update" / "database" / "market_data.db"
    _write_identity_database(
        force_update_database,
        snapshot=_snapshot(private_key, force_update=True),
    )
    client = _client(force_update_database, private_key, monkeypatch)
    with pytest.raises(MembershipAccessError, match="请升级 ETFQuest"):
        client.require_core_access()


def test_membership_client_rejects_tampered_encrypted_snapshot(monkeypatch, tmp_path: Path):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    _write_identity_database(database, snapshot=_snapshot(private_key))
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "UPDATE system_settings SET value = 'not-a-fernet-token' WHERE key = ?",
            ["server_membership_snapshot"],
        )
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="解密失败"):
        client.check_access()


def test_membership_client_rejects_unsigned_or_mismatched_license_snapshot(
    monkeypatch, tmp_path: Path
):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    snapshot = _snapshot(private_key)
    snapshot["license_key"] = "invalid.signature"
    _write_identity_database(database, snapshot=snapshot)
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="签名或设备校验"):
        client.check_access()


def test_membership_client_rejects_expiry_not_bound_to_server_signature(
    monkeypatch, tmp_path: Path
):
    database = tmp_path / "ETFQuest" / "database" / "market_data.db"
    private_key = ec.generate_private_key(ec.SECP256R1())
    snapshot = _snapshot(private_key)
    snapshot["exp"] = "2098-01-01"
    _write_identity_database(database, snapshot=snapshot)
    client = _client(database, private_key, monkeypatch)

    with pytest.raises(MembershipUnavailableError, match="签名或设备校验"):
        client.check_access()
