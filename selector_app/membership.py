"""Read-only ETFQuest membership verification for shared deployments."""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

import duckdb
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ETFQUEST_MACHINE_ID_ALGORITHM_VERSION = "hardware_uuid_or_mac_md5_v1"
MEMBERSHIP_LICENSE_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESy/6+4517q5uajxIDQZ/hKQfZOV0
ceitcGn/d09Jk6wcOn59Myk4uPZVZKEIb4zN9yTsGEaFLgV4eKjlwlM7Hg==
-----END PUBLIC KEY-----
"""

_SYSTEM_SETTING_KEYS = (
    "machine_id",
    "machine_id_algorithm_version",
    "server_membership_snapshot",
    "server_membership_sync_state",
)
_SNAPSHOT_V3_FIELDS = frozenset(
    {
        "is_trial",
        "trial_started_at",
        "trial_expires_at",
        "entitlement_source",
        "entitlement_required",
        "can_access_core",
        "force_update",
        "min_supported_version",
        "update_message",
    }
)
_ENTITLEMENT_SOURCES = frozenset({"trial", "manual", "bonus", "legacy"})
_MEMBERSHIP_LEVELS = frozenset({"free", "pro", "vip", "trial", "beta"})
_MAX_SNAPSHOT_TOKEN_CHARS = 16_384
_MAX_SNAPSHOT_PAYLOAD_BYTES = 4_096
_MAX_LICENSE_PAYLOAD_BYTES = 4_096
_MAX_LICENSE_SIGNATURE_BYTES = 1_024


class _V3MembershipSnapshot(TypedDict):
    version: int
    level: str
    is_vip: bool
    expire_at: str
    exp: str
    message: str
    synced_at: str
    license_key: str
    is_trial: bool
    trial_started_at: str
    trial_expires_at: str
    entitlement_source: str
    entitlement_required: bool
    can_access_core: bool
    force_update: bool
    min_supported_version: str
    update_message: str


class MembershipUnavailableError(RuntimeError):
    """Membership cannot be verified from the local ETFQuest installation."""


class MembershipAccessError(RuntimeError):
    """The server returned a valid but non-entitled membership state."""


@dataclass(frozen=True)
class MembershipStatus:
    level: str
    is_vip: bool
    can_access_core: bool
    access_state: str
    message: str
    expire_at: str | None = None
    is_trial: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "is_vip": self.is_vip,
            "can_access_core": self.can_access_core,
            "access_state": self.access_state,
            "message": self.message,
            "expire_at": self.expire_at,
            "is_trial": self.is_trial,
        }


class ETFQuestMembershipClient:
    """Verify access from ETFQuest's machine-bound, encrypted membership snapshot."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        secret_key_path: str | Path | None = None,
        cache_seconds: float = 60.0,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        configured_key_path = secret_key_path or os.getenv("SELECTOR_ETFQUEST_SECRET_KEY_PATH")
        self.secret_key_path = (
            Path(configured_key_path).expanduser()
            if configured_key_path
            else self.database_path.parent.parent / ".secret.key"
        )
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cached_status: MembershipStatus | None = None
        self._cached_at = 0.0

    def _read_settings(self) -> dict[str, str]:
        if not self.database_path.is_file():
            raise MembershipUnavailableError(
                "未找到 ETFQuest 行情数据库，请升级并启动最新版 ETFQuest"
            )
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                placeholders = ", ".join("?" for _ in _SYSTEM_SETTING_KEYS)
                rows = connection.execute(
                    f"SELECT key, value FROM system_settings WHERE key IN ({placeholders})",
                    list(_SYSTEM_SETTING_KEYS),
                ).fetchall()
        except (duckdb.Error, OSError) as exc:
            raise MembershipUnavailableError(
                "无法读取 ETFQuest 授权信息，请关闭同步任务后重试"
            ) from exc
        settings = {str(key): str(value or "").strip() for key, value in rows}
        machine_id = settings.get("machine_id", "")
        algorithm_version = settings.get("machine_id_algorithm_version", "")
        if not machine_id or not algorithm_version:
            raise MembershipUnavailableError(
                "当前 ETFQuest 版本未保存机器码，请升级并启动最新版 ETFQuest"
            )
        if algorithm_version != ETFQUEST_MACHINE_ID_ALGORITHM_VERSION:
            raise MembershipUnavailableError(
                "ETFQuest 机器码算法版本不兼容，请升级并启动最新版 ETFQuest"
            )
        if settings.get("server_membership_sync_state", "").lower() != "synced":
            raise MembershipUnavailableError(
                "ETFQuest 本地会员状态尚未完成同步，请先在 ETFQuest 中联网刷新授权"
            )
        if not settings.get("server_membership_snapshot"):
            raise MembershipUnavailableError(
                "未找到 ETFQuest 的受保护会员快照，请先在 ETFQuest 中联网刷新授权"
            )
        return settings

    def _decrypt_snapshot(self, token: str) -> dict[str, object]:
        if len(token) > _MAX_SNAPSHOT_TOKEN_CHARS:
            raise MembershipUnavailableError("ETFQuest 本地会员快照过大，无法安全读取")
        try:
            if self.secret_key_path.stat().st_size > 256:
                raise MembershipUnavailableError("ETFQuest .secret.key 文件大小异常")
            key = self.secret_key_path.read_bytes().strip()
        except MembershipUnavailableError:
            raise
        except OSError as exc:
            raise MembershipUnavailableError(
                "无法读取 ETFQuest 的 .secret.key；请用共享启动脚本只读挂载授权密钥"
            ) from exc
        try:
            raw = Fernet(key).decrypt(token.encode("ascii"))
        except (InvalidToken, ValueError, TypeError, UnicodeEncodeError) as exc:
            raise MembershipUnavailableError(
                "ETFQuest 本地会员快照解密失败，请确认挂载的是同一安装目录的 .secret.key"
            ) from exc
        if len(raw) > _MAX_SNAPSHOT_PAYLOAD_BYTES:
            raise MembershipUnavailableError("ETFQuest 本地会员快照内容过大，无法安全读取")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MembershipUnavailableError("ETFQuest 本地会员快照格式无效") from exc
        if not isinstance(payload, dict):
            raise MembershipUnavailableError("ETFQuest 本地会员快照格式无效")
        return payload

    @staticmethod
    def _verify_server_license_key(license_key: str, machine_id: str) -> dict[str, object] | None:
        if not license_key or not machine_id or license_key.count(".") != 1:
            return None
        payload_part, signature_part = license_key.split(".", 1)
        try:
            compressed = base64.b64decode(payload_part, validate=True)
            signature = base64.b64decode(signature_part, validate=True)
            if (
                len(compressed) > _MAX_LICENSE_PAYLOAD_BYTES
                or len(signature) > _MAX_LICENSE_SIGNATURE_BYTES
            ):
                return None
            public_key = serialization.load_pem_public_key(MEMBERSHIP_LICENSE_PUBLIC_KEY_PEM)
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                return None
            public_key.verify(signature, compressed, ec.ECDSA(hashes.SHA256()))
            decompressor = zlib.decompressobj()
            raw_payload = decompressor.decompress(compressed, _MAX_LICENSE_PAYLOAD_BYTES + 1)
            if (
                len(raw_payload) > _MAX_LICENSE_PAYLOAD_BYTES
                or decompressor.unconsumed_tail
                or decompressor.unused_data
                or not decompressor.eof
            ):
                return None
            payload = json.loads(raw_payload.decode("utf-8"))
        except (
            binascii.Error,
            InvalidSignature,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            zlib.error,
        ):
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("mid") or "").strip() != machine_id:
            return None
        if not isinstance(payload.get("exp"), str) or not payload["exp"].strip():
            return None
        if not isinstance(payload.get("level"), str) or not payload["level"].strip():
            return None
        return payload

    @staticmethod
    def _is_expired(expire_at: str) -> bool:
        raw = expire_at.strip()
        if not raw or raw.lower() in {
            "永久有效",
            "permanent",
            "forever",
            "-",
            "none",
            "null",
            "待同步",
        }:
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            parsed = (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )
            return parsed < datetime.now(timezone.utc)
        except ValueError:
            try:
                parsed_date = date.fromisoformat(raw[:10])
            except ValueError:
                # ETFQuest treats unknown display dates as non-expired; the
                # signed exp claim is still checked before core access is granted.
                return False
            return parsed_date < datetime.now(timezone.utc).date()

    def _snapshot_status(self, settings: dict[str, str]) -> MembershipStatus:
        payload = self._decrypt_snapshot(settings["server_membership_snapshot"])
        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version != 3:
            raise MembershipUnavailableError(
                "ETFQuest 本地会员快照不是可离线验证的 v3 格式，请先联网刷新授权"
            )
        if not _SNAPSHOT_V3_FIELDS.issubset(payload):
            raise MembershipUnavailableError("ETFQuest 本地会员快照缺少授权策略字段")
        if not all(
            isinstance(payload.get(field), bool)
            for field in (
                "is_vip",
                "is_trial",
                "entitlement_required",
                "can_access_core",
                "force_update",
            )
        ):
            raise MembershipUnavailableError("ETFQuest 本地会员快照中的授权策略字段无效")

        string_fields = (
            "level",
            "expire_at",
            "exp",
            "message",
            "synced_at",
            "license_key",
            "trial_started_at",
            "trial_expires_at",
            "entitlement_source",
            "min_supported_version",
            "update_message",
        )
        if not all(isinstance(payload.get(field), str) for field in string_fields):
            raise MembershipUnavailableError("ETFQuest 本地会员快照包含无效字段")
        if payload["entitlement_source"] not in _ENTITLEMENT_SOURCES:
            raise MembershipUnavailableError("ETFQuest 本地会员快照授权来源无效")
        snapshot = cast(_V3MembershipSnapshot, payload)

        level = snapshot["level"].strip().lower()
        if level not in _MEMBERSHIP_LEVELS:
            level = "vip" if snapshot["is_vip"] else "free"
        is_vip = snapshot["is_vip"]
        expire_at = snapshot["expire_at"].strip()
        expired = self._is_expired(expire_at)

        if is_vip:
            signed = self._verify_server_license_key(
                snapshot["license_key"], settings["machine_id"]
            )
            if signed is None or signed.get("exp") != snapshot["exp"]:
                raise MembershipUnavailableError("ETFQuest 本地会员签名或设备校验失败")

        message = snapshot["message"].strip()
        force_update = snapshot["force_update"]
        can_access_core = (
            snapshot["can_access_core"]
            and not expired
            and not force_update
            and is_vip
            and level != "free"
        )
        access_state = (
            "force_update"
            if force_update
            else "expired"
            if expired
            else "trial"
            if snapshot["is_trial"] and is_vip
            else "vip"
            if is_vip
            else "registered"
        )
        if force_update:
            message = (
                snapshot["update_message"].strip() or "当前 ETFQuest 版本需要升级后才能继续使用"
            )
        elif expired and not message:
            message = "会员资格已过期，请前往会员中心续期或重新激活"
        elif not is_vip and not message:
            message = "未开通有效会员授权"
        elif not can_access_core and not message:
            message = "当前授权状态不允许使用核心功能"
        return MembershipStatus(
            level="free" if expired else level,
            is_vip=is_vip and not expired,
            can_access_core=can_access_core,
            access_state=access_state,
            message=message or "已同步服务端会员状态",
            expire_at=expire_at or None,
            is_trial=snapshot["is_trial"] and not expired and not force_update,
        )

    def check_access(self, *, force_refresh: bool = False) -> MembershipStatus:
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached_status is not None
            and now - self._cached_at < self.cache_seconds
        ):
            return self._cached_status
        settings = self._read_settings()
        status = self._snapshot_status(settings)
        self._cached_status = status
        self._cached_at = time.monotonic()
        return status

    def require_core_access(self) -> MembershipStatus:
        status = self.check_access()
        if not status.can_access_core:
            raise MembershipAccessError(status.message or "会员资格已过期，请续期后继续使用")
        return status


__all__ = [
    "ETFQuestMembershipClient",
    "ETFQUEST_MACHINE_ID_ALGORITHM_VERSION",
    "MembershipAccessError",
    "MembershipStatus",
    "MembershipUnavailableError",
]
