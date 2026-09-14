"""Read-only ETFQuest membership verification for shared deployments."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import duckdb

ETFQUEST_MACHINE_ID_ALGORITHM_VERSION = "hardware_uuid_or_mac_md5_v1"


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
    """Verify core access using credentials persisted by ETFQuest."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        server_url: str | None = None,
        timeout: float = 10.0,
        cache_seconds: float = 60.0,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.server_url = (
            str(
                server_url
                or os.getenv("SELECTOR_LICENSE_SERVER_URL")
                or "https://www.etfquest.online"
            )
            .strip()
            .rstrip("/")
        )
        self.timeout = max(1.0, float(timeout))
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cached_status: MembershipStatus | None = None
        self._cached_at = 0.0

    def _read_settings(self) -> tuple[str, str, str]:
        if not self.database_path.is_file():
            raise MembershipUnavailableError(
                "未找到 ETFQuest 行情数据库，请升级并启动最新版 ETFQuest"
            )
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                rows = connection.execute(
                    "SELECT key, value FROM system_settings "
                    "WHERE key IN ('machine_id', 'machine_id_algorithm_version', 'client_secret')"
                ).fetchall()
        except (duckdb.Error, OSError) as exc:
            raise MembershipUnavailableError(
                "无法读取 ETFQuest 授权信息，请关闭同步任务后重试"
            ) from exc
        settings = {str(key): str(value or "").strip() for key, value in rows}
        machine_id = settings.get("machine_id", "")
        algorithm_version = settings.get("machine_id_algorithm_version", "")
        secret = settings.get("client_secret", "")
        if not machine_id or not algorithm_version:
            raise MembershipUnavailableError(
                "当前 ETFQuest 版本未保存机器码，请升级并启动最新版 ETFQuest"
            )
        if algorithm_version != ETFQUEST_MACHINE_ID_ALGORITHM_VERSION:
            raise MembershipUnavailableError(
                "ETFQuest 机器码算法版本不兼容，请升级并启动最新版 ETFQuest"
            )
        if not secret:
            raise MembershipUnavailableError(
                "ETFQuest 尚未完成设备授权，请先联网启动最新版 ETFQuest"
            )
        return machine_id, algorithm_version, secret

    def check_access(self, *, force_refresh: bool = False) -> MembershipStatus:
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached_status is not None
            and now - self._cached_at < self.cache_seconds
        ):
            return self._cached_status
        machine_id, _algorithm_version, client_secret = self._read_settings()
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        signature_payload = f"{timestamp}{nonce}{machine_id}".encode()
        signature = hmac.new(
            client_secret.encode(),
            signature_payload,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            f"{self.server_url}/my/status",
            method="GET",
            headers={
                "X-Machine-ID": machine_id,
                "X-Timestamp": timestamp,
                "X-Nonce": nonce,
                "X-Signature": signature,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_payload = response.read()
            payload = json.loads(raw_payload.decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise MembershipUnavailableError("会员状态暂时无法验证，请检查网络后重试") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise MembershipUnavailableError("会员状态响应格式无效，请升级 ETFQuest 后重试")
        status = MembershipStatus(
            level=str(data.get("level") or "free"),
            is_vip=bool(data.get("is_vip")),
            can_access_core=bool(data.get("can_access_core")),
            access_state=str(data.get("access_state") or "unknown"),
            message=str(data.get("message") or "会员状态未知"),
            expire_at=str(data.get("expire_at")) if data.get("expire_at") else None,
            is_trial=bool(data.get("is_trial")),
        )
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
