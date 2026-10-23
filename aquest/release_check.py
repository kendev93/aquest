"""每日一次的「上游有没有发新版本」检查。

这个版本跑在用户自己的机器上，不替他自己升级：重启会把在跑的全市场任务标失败、行情库
又是独占写、schema 变更还得看发布说明，那几步该由人按下。这里只回答一个问题——上游
是否已经发布了更新的版本——然后把结论交给顶栏那条可关闭的提示。

判据取 GitHub Releases 的 ``latest``：每次打 ``v*`` tag 的发布流水线既推镜像也建 release
（见 ``.github/workflows/publish-image.yml``），所以它就是"最新已发布版本"，而且自动排除
预发布。匿名调用 60 次/小时/IP，一天一次用不完；失败或被限流后按小时级退避，不去撞限额。

出网失败、超时、仓库不可达、版本号格式不认识，一律按"不知道"处理：不显示提示，也不把
错误抛给用户看。离线部署用 ``AQUEST_UPDATE_CHECK=off`` 彻底关掉。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

#: 上游仓库；fork 部署可以用 AQUEST_RELEASE_REPOSITORY 指到自己的仓库。
DEFAULT_RELEASE_REPOSITORY = "kendev93/aquest"
#: 正常节奏：一天一次。版本发布频率远低于此，多查只会白占一次出网。
RELEASE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
#: 查不到时的退避下限：按分钟级重试只会一直撞 GitHub 的匿名限流。
RELEASE_CHECK_FAILURE_INTERVAL_SECONDS = 60 * 60
RELEASE_CHECK_TIMEOUT_SECONDS = 6.0
RELEASE_CHECK_DISABLED_VALUES = frozenset({"0", "false", "off"})
_RELEASE_URL_TEMPLATE = "https://api.github.com/repos/{repository}/releases/latest"
#: GitHub 匿名 API 强制要求 User-Agent，缺了直接 403。
_RELEASE_USER_AGENT = "aquest-release-check/1.0"
_VERSION_PATTERN = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class ReleaseInfo:
    """上游最新发布版本。"""

    tag: str
    version: str
    url: str
    published_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "tag": self.tag,
            "version": self.version,
            "url": self.url,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class ReleaseStatus:
    """给前端的一份结论：当前版本、上游版本、要不要提示。

    ``latest`` 为空代表"没查到或判不出来"，前端据此什么都不显示——失败不是消息，
    一条"检查失败"的红点在离线部署里只会每天提醒用户他的网络不通。
    """

    current: str
    repository: str
    enabled: bool
    latest: ReleaseInfo | None = None
    checked_at: str | None = None
    error: str | None = None

    @property
    def update_available(self) -> bool:
        if self.latest is None or not self.enabled:
            return False
        return is_newer_release(self.latest.version, self.current)

    def to_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "repository": self.repository,
            "enabled": self.enabled,
            "update_available": self.update_available,
            "latest": self.latest.to_dict() if self.latest is not None else None,
            "checked_at": self.checked_at,
            "error": self.error,
        }


def parse_release_version(value: str | None) -> tuple[int, ...] | None:
    """把 ``v0.1.7`` 这种写法解析成可比较的整数元组；解析不出就返回 None。

    只取开头的数字段，``-rc1`` 之类后缀不参与比较（GitHub 的 ``latest`` 本来也排除
    预发布）。返回 None 的情况一律按"判不出"处理：宁可不提示，也不因为格式不认识而
    天天提醒用户升级一个可能更旧的版本。
    """

    if not value:
        return None
    match = _VERSION_PATTERN.match(value)
    if match is None:
        return None
    parts = match.group(1).split(".")
    try:
        return tuple(int(item) for item in parts)
    except ValueError:
        return None


def is_newer_release(candidate: str | None, current: str | None) -> bool:
    """上游版本是否比本机跑的更新（按数字段逐位比，短的一方右侧补 0）。"""

    left = parse_release_version(candidate)
    right = parse_release_version(current)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return padded_left > padded_right


def fetch_latest_release(
    repository: str,
    *,
    timeout: float = RELEASE_CHECK_TIMEOUT_SECONDS,
) -> ReleaseInfo | None:
    """取最新发布版本；返回 None 表示这不是一个可比较的 release。"""

    url = _RELEASE_URL_TEMPLATE.format(repository=repository)
    request = Request(  # noqa: S310 - 固定 https 的 GitHub API 地址，没有用户可控的 scheme
        url,
        headers={
            "User-Agent": _RELEASE_USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None
    html_url = str(payload.get("html_url") or f"https://github.com/{repository}/releases/latest")
    published = payload.get("published_at")
    return ReleaseInfo(
        tag=tag,
        version=tag,
        url=html_url,
        published_at=str(published) if published else None,
    )


class ReleaseChecker:
    """带缓存的版本检查：一天一次，失败按小时退避，全程不阻塞请求。"""

    def __init__(
        self,
        *,
        current_version: str,
        repository: str = DEFAULT_RELEASE_REPOSITORY,
        enabled: bool = True,
        fetch: Callable[[str], ReleaseInfo | None] = fetch_latest_release,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = RELEASE_CHECK_INTERVAL_SECONDS,
        failure_interval_seconds: float = RELEASE_CHECK_FAILURE_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("版本检查间隔必须大于 0")
        self._current_version = current_version
        self._repository = repository
        self._enabled = enabled
        self._fetch = fetch
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval_seconds = interval_seconds
        self._failure_interval_seconds = failure_interval_seconds
        self._lock = threading.Lock()
        self._latest: ReleaseInfo | None = None
        self._checked_at: datetime | None = None
        self._error: str | None = None
        self._checked_once = False

    @property
    def enabled(self) -> bool:
        """关掉时连后台循环都不该起来：不检查也不出网。"""

        return self._enabled

    def status(self) -> ReleaseStatus:
        """只回缓存：一次页面加载不该为出网等待，检查由后台任务负责。"""

        with self._lock:
            return ReleaseStatus(
                current=self._current_version,
                repository=self._repository,
                enabled=self._enabled,
                latest=self._latest,
                checked_at=self._checked_at.isoformat() if self._checked_at else None,
                error=self._error,
            )

    def check(self, *, force: bool = False) -> ReleaseStatus:
        """到点就查一次上游；返回查完（或直接返回缓存）的状态。"""

        if not self._enabled:
            return self.status()
        with self._lock:
            due = (
                force
                or self._checked_at is None
                or (self._clock() - self._checked_at).total_seconds() >= self._next_interval()
            )
            if not due:
                return self._locked_status()
        try:
            latest = self._fetch(self._repository)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            # 断网、被限流、返回体不是 JSON 都只说明"这次不知道"，按失败退避。
            logger.debug("上游版本检查失败：%s", str(exc)[:200])
            self._record(None, f"{type(exc).__name__}: {str(exc)[:180]}")
        except Exception as exc:  # noqa: BLE001 - 版本检查绝不允许拖垮调度循环
            logger.exception("上游版本检查出现未预期错误")
            self._record(None, f"{type(exc).__name__}: {str(exc)[:180]}")
        else:
            self._record(latest, None)
        with self._lock:
            return self._locked_status()

    def next_check_delay_seconds(self) -> float:
        """下一次检查该隔多久：没查到结果时缩短到退避间隔。"""

        return (
            self._failure_interval_seconds
            if self._error or not self._checked_once
            else self._interval_seconds
        )

    async def run(self) -> None:
        """后台循环：启动即查一次，之后按节奏复查。"""

        while True:
            try:
                await asyncio.to_thread(self.check)
            except Exception:  # noqa: BLE001 - 调度循环不能因一次检查退出
                logger.exception("上游版本检查调度失败")
            await asyncio.sleep(max(1.0, self.next_check_delay_seconds()))

    def _next_interval(self) -> float:
        return self._failure_interval_seconds if self._error else self._interval_seconds

    def _record(self, latest: ReleaseInfo | None, error: str | None) -> None:
        with self._lock:
            self._latest = latest
            self._error = error
            self._checked_at = self._clock()
            self._checked_once = True

    def _locked_status(self) -> ReleaseStatus:
        return ReleaseStatus(
            current=self._current_version,
            repository=self._repository,
            enabled=self._enabled,
            latest=self._latest,
            checked_at=self._checked_at.isoformat() if self._checked_at else None,
            error=self._error,
        )


def release_check_enabled(value: str | None = None) -> bool:
    """``AQUEST_UPDATE_CHECK`` 关掉就不再有任何出网请求（离线/内网部署）。"""

    raw = value if value is not None else os.getenv("AQUEST_UPDATE_CHECK")
    if raw is None:
        return True
    return raw.strip().lower() not in RELEASE_CHECK_DISABLED_VALUES


def release_repository(value: str | None = None) -> str:
    raw = value if value is not None else os.getenv("AQUEST_RELEASE_REPOSITORY")
    repository = (raw or DEFAULT_RELEASE_REPOSITORY).strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        logger.warning(
            "AQUEST_RELEASE_REPOSITORY 不是 owner/name 形式，改用默认仓库：%s", repository
        )
        return DEFAULT_RELEASE_REPOSITORY
    return repository


__all__ = [
    "DEFAULT_RELEASE_REPOSITORY",
    "ReleaseChecker",
    "ReleaseInfo",
    "ReleaseStatus",
    "fetch_latest_release",
    "is_newer_release",
    "parse_release_version",
    "release_check_enabled",
    "release_repository",
]
