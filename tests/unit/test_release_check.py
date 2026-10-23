"""上游版本检查的判据与节奏。

这个检查唯一的风险不是"没查到"，而是"查错了天天提醒升级"或者"每天把用户的面板点在
一条红色错误上"。所以下面的断言都钉在这两条：判不出就不提示，失败就静默退避。
"""

import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest

from aquest.release_check import (
    ReleaseChecker,
    ReleaseInfo,
    fetch_latest_release,
    is_newer_release,
    parse_release_version,
    release_check_enabled,
    release_repository,
)

UTC = timezone.utc


class _Counter:
    """假的上游查询：记下每次调用，返回预先准备好的结果或抛错。"""

    def __init__(self, result: ReleaseInfo | None = None, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def __call__(self, repository: str) -> ReleaseInfo | None:
        self.calls.append(repository)
        if self._error is not None:
            raise self._error
        return self._result

    @property
    def count(self) -> int:
        return len(self.calls)


def _release(tag: str) -> ReleaseInfo:
    return ReleaseInfo(
        tag=tag,
        version=tag,
        url=f"https://github.com/kendev93/aquest/releases/tag/{tag}",
        published_at="2026-10-24T02:00:00Z",
    )


def test_only_a_higher_numbered_release_counts_as_newer() -> None:
    assert is_newer_release("v0.1.8", "0.1.7") is True
    assert is_newer_release("v0.2", "0.1.9") is True
    assert is_newer_release("v0.1.7", "0.1.7") is False
    assert is_newer_release("v0.1.6", "0.1.7") is False
    # 位数补齐：0.2 == 0.2.0，别让"多写了一个 .0"变成升级。
    assert is_newer_release("v0.2.0", "0.2") is False
    # 认不出的格式一律不提示：宁可不提醒，也不指向一个可能更旧的版本。
    assert is_newer_release("nightly-2026-10-24", "0.1.7") is False
    assert is_newer_release("", "0.1.7") is False
    assert is_newer_release("v1.0.0", "") is False
    assert parse_release_version("v1.2.3-rc1") == (1, 2, 3)


def test_checker_queries_once_per_interval_and_rechecks_after_it_elapses() -> None:
    now = [datetime(2026, 10, 24, 2, 0, tzinfo=UTC)]
    counter = _Counter(_release("v0.1.8"))
    checker = ReleaseChecker(
        current_version="0.1.7",
        fetch=counter,
        clock=lambda: now[0],
        interval_seconds=3600,
    )

    status = checker.check()
    assert status.update_available is True
    assert status.latest is not None and status.latest.tag == "v0.1.8"
    assert counter.count == 1

    # 没到节奏就不许多打一次：顶栏每次挂载都会读状态。
    checker.check()
    checker.check()
    assert counter.count == 1

    now[0] += timedelta(minutes=59)
    checker.check()
    assert counter.count == 1
    now[0] += timedelta(minutes=1)
    checker.check()
    assert counter.count == 2


def test_status_reads_cache_only_and_never_touches_the_network() -> None:
    counter = _Counter(_release("v0.1.8"))
    checker = ReleaseChecker(current_version="0.1.7", fetch=counter)

    assert checker.status().latest is None
    assert checker.status().update_available is False
    assert counter.count == 0


def test_a_failed_check_is_silent_and_backs_off() -> None:
    """出网失败只让提示消失，不把它变成一条用户每天要看的错误。"""

    now = [datetime(2026, 10, 24, 2, 0, tzinfo=UTC)]
    counter = _Counter(error=URLError("network unreachable"))
    checker = ReleaseChecker(
        current_version="0.1.7",
        fetch=counter,
        clock=lambda: now[0],
        interval_seconds=3600,
        failure_interval_seconds=600,
    )

    status = checker.check()
    assert status.latest is None
    assert status.update_available is False
    assert status.error and "network unreachable" in status.error
    assert counter.count == 1
    # 失败后改用更短的节奏重试（而不是死等一天），但节奏没到就不许多敲一次限流。
    assert checker.next_check_delay_seconds() == 600
    assert checker.check().error is not None
    assert counter.count == 1

    now[0] += timedelta(minutes=10)
    assert checker.check().error is not None
    assert counter.count == 2

    # 恢复之后：错误被清掉，节奏回到正常的一天一次。
    recovered_counter = _Counter(_release("v9.9.9"))
    recovered = ReleaseChecker(
        current_version="0.1.7",
        fetch=recovered_counter,
        clock=lambda: now[0],
        interval_seconds=3600,
        failure_interval_seconds=600,
    )
    assert recovered.check().latest is not None
    assert recovered.check().error is None
    assert recovered.next_check_delay_seconds() == 3600


def test_disabled_checker_never_fetches_and_says_so() -> None:
    counter = _Counter(_release("v0.1.8"))
    checker = ReleaseChecker(current_version="0.1.7", enabled=False, fetch=counter)

    assert checker.enabled is False
    assert checker.check().to_dict() == checker.status().to_dict()
    assert counter.count == 0
    assert checker.status().enabled is False


def _explode(_repository: str) -> ReleaseInfo:
    raise RuntimeError("unexpected")


def test_unexpected_fetch_failure_cannot_break_the_scheduler() -> None:
    """检查器绝不能因为一次意外异常让后台循环退出——它只是想知道有没有新版本。"""

    status = ReleaseChecker(current_version="0.1.7", fetch=_explode).check()
    assert status.latest is None
    assert "RuntimeError" in (status.error or "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("off", False), ("OFF", False), ("0", False), ("false", False), (None, True), ("1", True)],
)
def test_env_switch(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool) -> None:
    if raw is None:
        monkeypatch.delenv("AQUEST_UPDATE_CHECK", raising=False)
    else:
        monkeypatch.setenv("AQUEST_UPDATE_CHECK", raw)
    assert release_check_enabled() is expected


def test_repository_override_and_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AQUEST_RELEASE_REPOSITORY", "acme/aquest-fork")
    assert release_repository() == "acme/aquest-fork"
    # 不是 owner/name 的形式（比如塞了 URL 或空格）不拿去拼请求地址。
    monkeypatch.setenv("AQUEST_RELEASE_REPOSITORY", "https://evil.example/x")
    assert release_repository() == "kendev93/aquest"


class _Response(io.BytesIO):
    def __enter__(self) -> io.BytesIO:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_fetch_sends_the_user_agent_github_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float) -> _Response:
        del timeout
        # urllib 会把请求头规范化成首字母大写，断言里统一按小写取。
        seen["headers"] = {str(key).lower(): value for key, value in dict(request.headers).items()}  # type: ignore[attr-defined]
        seen["url"] = request.full_url  # type: ignore[attr-defined]
        payload = {
            "tag_name": "v0.2.0",
            "html_url": "https://github.com/kendev93/aquest/releases/tag/v0.2.0",
            "published_at": "2026-11-01T00:00:00Z",
        }
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr("aquest.release_check.urlopen", fake_urlopen)
    info = fetch_latest_release("kendev93/aquest")

    assert info is not None and info.tag == "v0.2.0"
    assert seen["url"] == "https://api.github.com/repos/kendev93/aquest/releases/latest"
    # 匿名 API 没有 User-Agent 直接 403，这条是检查能不能用的前提。
    assert str(seen["headers"].get("user-agent", "")).startswith("aquest-release-check")
    assert info.published_at == "2026-11-01T00:00:00Z"


def test_fetch_without_a_tag_is_not_an_available_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库不存在或被限流时 GitHub 回的是没有 tag_name 的对象：按"没有可比较的版本"处理。"""

    def fake_urlopen(_request: object, timeout: float) -> _Response:
        del timeout
        return _Response(b'{"message": "Not Found"}')

    monkeypatch.setattr("aquest.release_check.urlopen", fake_urlopen)

    assert fetch_latest_release("nobody/nowhere") is None
