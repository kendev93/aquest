from __future__ import annotations

import pytest

from aquest.market_data.risk_warning import (
    is_risk_warning_name,
    is_risk_warning_symbol,
)


@pytest.mark.parametrize(
    "name",
    [
        "ST云网",
        "*ST萃华",
        "S*ST前锋",
        "SST华新",
        "ST 棕榈",
        "长江退",
        "退市美吉",
    ],
)
def test_risk_warning_names_are_detected(name: str) -> None:
    assert is_risk_warning_name(name) is True


@pytest.mark.parametrize("name", ["贵州茅台", "平安银行", "宁德时代", "中远海控"])
def test_normal_names_pass(name: str) -> None:
    assert is_risk_warning_name(name) is False


def test_missing_name_is_never_treated_as_risk_warning() -> None:
    """拿不到名称时放行。

    悄悄丢掉一只股票比放进来更糟：放进来还有指标规则兜底，丢掉了用户根本
    不知道样本少了一只。
    """

    assert is_risk_warning_name(None) is False
    assert is_risk_warning_name("") is False
    assert is_risk_warning_name("   ") is False


def test_symbol_helper_reads_the_name_attribute() -> None:
    class _Ref:
        def __init__(self, name: str | None) -> None:
            self.name = name

    assert is_risk_warning_symbol(_Ref("*ST准油")) is True
    assert is_risk_warning_symbol(_Ref("闻泰科技")) is False
    assert is_risk_warning_symbol(_Ref(None)) is False
    assert is_risk_warning_symbol(None) is False
