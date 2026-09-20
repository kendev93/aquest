from __future__ import annotations

import subprocess

import pandas as pd
import pytest

from selector_app.adapters import baostock_catalog as catalog_module
from selector_app.adapters.baostock_catalog import (
    BaostockUnavailable,
    fetch_all_securities,
)

# baostock 的行是 code / tradeStatus / code_name，制表符分隔。
_SAMPLE = "\n".join(
    [
        "sh.600000\t1\t浦发银行",
        "sz.000001\t1\t平安银行",
        "sz.300750\t1\t宁德时代",
        "sh.688001\t1\t科创样本",
        # 北交所：行情库与回测契约只有两市，必须被挡掉。
        "bj.830001\t1\t北交样本",
        # 深市 B 股留在列表里，由下游的板块过滤处理（与东财目录行为一致）。
        "sz.200001\t1\t深市 B 股",
        "",
        "sh.600001",  # 缺列，直接跳过
        "sh.6000001\t1\t超长代码",  # 非 6 位，跳过
    ]
)


def test_parse_keeps_only_six_digit_sh_sz_codes() -> None:
    frame = fetch_all_securities(runner=lambda _day, _timeout: _SAMPLE)

    assert list(frame.columns) == ["代码", "名称", "市场"]
    assert frame["代码"].tolist() == ["600000", "000001", "300750", "688001", "200001"]
    assert frame["名称"].tolist() == ["浦发银行", "平安银行", "宁德时代", "科创样本", "深市 B 股"]
    assert frame["市场"].tolist() == ["SH", "SZ", "SZ", "SH", "SZ"]


def test_parse_keeps_the_exchange_so_equal_codes_stay_distinct() -> None:
    """sh.000001（上证指数）和 sz.000001（平安银行）代码数字完全相同。

    丢掉交易所前缀后二者无法区分：下游按代码猜市场会把指数判成深市，再按
    ``(市场, 代码)`` 去重时两只里的一只会被静默丢掉。
    """

    frame = fetch_all_securities(
        runner=lambda _day, _timeout: "sh.000001\t1\t上证综合指数\nsz.000001\t1\t平安银行\n"
    )

    assert frame["代码"].tolist() == ["000001", "000001"]
    assert frame["市场"].tolist() == ["SH", "SZ"]


def test_parse_turns_a_blank_name_into_none() -> None:
    frame = fetch_all_securities(runner=lambda _day, _timeout: "sh.600000\t1\t")

    assert frame["名称"].tolist() == [None]


def test_an_empty_catalog_is_an_error_not_a_silent_success() -> None:
    """空目录如果被当成成功，同步会「干干净净地同步零只股票」。"""

    with pytest.raises(BaostockUnavailable, match="目录为空"):
        fetch_all_securities(runner=lambda _day, _timeout: "   \n\n")


def test_the_trade_date_is_passed_through_to_the_backend() -> None:
    seen: list[str] = []

    def runner(day: str, _timeout: float) -> str:
        seen.append(day)
        return _SAMPLE

    from datetime import date

    fetch_all_securities(trade_date=date(2026, 9, 18), runner=runner)
    assert seen == ["2026-09-18"]


def test_a_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="大于 0"):
        fetch_all_securities(timeout=0, runner=lambda _day, _timeout: _SAMPLE)


def test_a_hung_query_becomes_a_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """baostock 的 socket 没有超时，服务端不响应就是永久挂起。

    子进程被 kill 之后必须翻译成可重试的错误，而不是让调用方一起卡死。
    """

    def raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="python", timeout=90)

    monkeypatch.setattr(catalog_module, "baostock_available", lambda: True)
    monkeypatch.setattr(catalog_module.subprocess, "run", raise_timeout)

    with pytest.raises(BaostockUnavailable, match="无响应"):
        fetch_all_securities()


def test_a_failed_child_process_reports_its_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(
        args=["python"],
        returncode=3,
        stdout="",
        stderr="query failed: 没有权限",
    )
    monkeypatch.setattr(catalog_module, "baostock_available", lambda: True)
    monkeypatch.setattr(catalog_module.subprocess, "run", lambda *_a, **_k: completed)

    with pytest.raises(BaostockUnavailable, match="没有权限"):
        fetch_all_securities()


def test_a_missing_dependency_is_reported_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("未安装 baostock 时不应启动子进程")

    monkeypatch.setattr(catalog_module, "baostock_available", lambda: False)
    monkeypatch.setattr(catalog_module.subprocess, "run", refuse)

    with pytest.raises(BaostockUnavailable, match="未安装可选依赖"):
        fetch_all_securities()


def test_catalog_rows_feed_the_shared_selection_helper() -> None:
    """产物必须与东财目录同构，否则无法复用那套分类/板块/范围过滤。"""

    frame = fetch_all_securities(runner=lambda _day, _timeout: _SAMPLE)
    assert isinstance(frame, pd.DataFrame)
    assert frame["代码"].map(str).str.fullmatch(r"\d{6}").all()
