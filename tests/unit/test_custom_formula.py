from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.formulas.custom import (
    FormulaParseError,
    evaluate_custom_formula,
    parse_formula,
)


def bars(length: int = 80) -> pd.DataFrame:
    close = np.arange(1.0, length + 1)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
        }
    )


def test_parser_extracts_explicit_parameters_and_named_outputs() -> None:
    parsed = parse_formula("N:=3; A:=EMA(C,N); BREAKOUT:CROSS(C,REF(A,1)),COLORRED;")

    assert [(parameter.name, parameter.default) for parameter in parsed.parameters] == [("N", 3.0)]
    assert [signal.id for signal in parsed.signals] == ["custom.breakout"]
    assert parsed.signals[0].display_name == "BREAKOUT"


def test_parameter_override_changes_formula_result_without_future_data() -> None:
    parsed = parse_formula("N:=2; LEVEL:REF(C,N);")
    original = evaluate_custom_formula(parsed, bars(), {"N": 2})
    overridden = evaluate_custom_formula(parsed, bars(), {"N": 5})

    assert original.sufficient_data is True
    assert original.last_indicator_values["LEVEL"] == 78.0
    assert overridden.last_indicator_values["LEVEL"] == 75.0
    assert (
        np.allclose(original.values["LEVEL"][:-1], overridden.values["LEVEL"][:-1], equal_nan=True)
        is False
    )


def test_formula_outputs_are_selectable_boolean_signals() -> None:
    parsed = parse_formula("N:=2; BREAKOUT:CROSS(C,REF(C,N));")
    result = evaluate_custom_formula(parsed, bars())

    assert result.signals["custom.breakout"].any()
    assert result.signals["custom.breakout"].dtype == bool


def test_custom_formula_supports_dynamic_ref_periods() -> None:
    parsed = parse_formula("W1:C=HHV(C,3); W2:=BARSLAST(W1); LEVEL:REF(C,W2);")
    result = evaluate_custom_formula(parsed, bars())

    assert "custom.w1" in {signal.id for signal in parsed.signals}
    assert result.last_indicator_values["LEVEL"] == 80.0


def test_custom_formula_has_warmup_and_safe_zero_division() -> None:
    parsed = parse_formula("N:=5; ZERO:C/(C-C)+REF(C,N);")
    result = evaluate_custom_formula(parsed, bars(3))

    assert result.sufficient_data is False
    assert result.last_signal_state["custom.zero"] is False
    assert not np.isinf(result.values["ZERO"]).any()


def test_the_three_supplied_formula_shapes_are_parseable() -> None:
    indicator_one = """
    VAR1:=REF((LOW+OPEN+CLOSE+HIGH)/4,1);
    VAR2:=SMA(ABS(LOW-VAR1),13,1)/SMA(MAX(LOW-VAR1,0),10,1);
    VAR3:=EMA(VAR2,10); VAR4:=LLV(LOW,33);
    VAR5:=EMA(IF(LOW<=VAR4,VAR3,0),3);
    主力进场:IF(VAR5>REF(VAR5,1),VAR5,0);
    """
    indicator_two = """
    短线成本:=EMA(CLOSE,17); A:=(3*C+L+O+H)/6;
    X:=(20*A+19*REF(A,1)+18*REF(A,2)+17*REF(A,3)+16*REF(A,4)+15*REF(A,5)+14*REF(A,6)+13*REF(A,7)+12*REF(A,8)+11*REF(A,9)+10*REF(A,10)+9*REF(A,11)+8*REF(A,12)+7*REF(A,13)+6*REF(A,14)+5*REF(A,15)+4*REF(A,16)+3*REF(A,17)+2*REF(A,18)+REF(A,20))/210;
    中线成本:=EMA(X,13); VAR1:=1; VAR2:=LLV(LOW,10); VAR3:=HHV(HIGH,25); N1:=5; N4:=34;
    CQ:=100*(C-LLV(L,N4))/(HHV(C,N4)-LLV(L,N4));
    W1:=C=HHV(C,20); W2:=BARSLAST(W1); W3:=IF(W2>0,REF(C,W2),REF(C,W2)); W4:=CROSS(C,REF(W3,1));
    中期:=CQ;
    持股:=3*SMA((CLOSE-LLV(LOW,27))/(HHV(HIGH,27)-LLV(LOW,27))*100,5,1)
    -2*SMA(SMA((CLOSE-LLV(LOW,27))/(HHV(HIGH,27)-LLV(LOW,27))*100,5,1),3,1);
    """
    indicator_three = """
    N:=5;
    VAR1:4*SMA((CLOSE-LLV(LOW,N))/(HHV(HIGH,N)-LLV(LOW,N))*100,5,1)
    -3*SMA(SMA((CLOSE-LLV(LOW,N))/(HHV(HIGH,N)-LLV(LOW,N))*100,5,1),3.2,1);
    VAR2:8; 准备拉升:IF(CROSS(VAR1,VAR2),80,0); 压住庄家:IF(VAR1<=8,25,0);
    VARO5:=LLV(LOW,27); VARO6:=HHV(HIGH,34);
    VARO7:=EMA((CLOSE-VARO5)/(VARO6-VARO5)*4,4)*25;
    建仓区:IF(VARO7<10,80,100);
    """

    for source in (indicator_one, indicator_two, indicator_three):
        parsed = parse_formula(source)
        result = evaluate_custom_formula(parsed, bars())
        assert result.data_length == 80
        assert result.values


@pytest.mark.parametrize(
    "formula",
    [
        "X:__import__('os').system('touch /tmp/pwned');",
        "X:(lambda: 1)();",
        "X:open('secret.txt');",
        "X:[1, 2, 3];",
    ],
)
def test_unsafe_expression_is_rejected_without_execution(formula: str) -> None:
    with pytest.raises(FormulaParseError):
        parse_formula(formula)


def test_formula_size_and_statement_limits_are_enforced() -> None:
    with pytest.raises(FormulaParseError, match="长度"):
        parse_formula("X:1;" + " " * 20_001)
    with pytest.raises(FormulaParseError, match="语句"):
        parse_formula(";".join(f"X{i}:1" for i in range(101)))


@pytest.mark.parametrize(
    "formula",
    [
        "LEVEL:MA(C,0);",
        "LEVEL:EMA(C,2.5);",
        "LEVEL:LLV(C,-1);",
        "LEVEL:COUNT(C>0,1.5);",
    ],
)
def test_rolling_function_periods_must_be_positive_integers(formula: str) -> None:
    parsed = parse_formula(formula)

    with pytest.raises(ValueError, match="周期必须是正整数"):
        evaluate_custom_formula(parsed, bars())


def test_std_avedev_and_sqrt_are_available_to_formulas() -> None:
    """这三个函数是让用户能自己写出布林带和 CCI 的最小集合。

    期望值用 pandas 原语独立复算，不抄 ``tdx_math`` 的实现。
    """

    frame = bars(60)
    close = frame["close"]

    std_result = evaluate_custom_formula(parse_formula("WIDTH:STD(C,20);"), frame, {})
    assert np.allclose(std_result.values["WIDTH"], close.rolling(20).std(), equal_nan=True)

    # 典型价的 14 日平均绝对偏差，即 CCI 的分母。
    typical = (frame["high"] + frame["low"] + close) / 3
    deviation_result = evaluate_custom_formula(parse_formula("MD:AVEDEV((H+L+C)/3,14);"), frame, {})
    assert np.allclose(
        deviation_result.values["MD"],
        typical.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True),
        equal_nan=True,
    )

    sqrt_result = evaluate_custom_formula(parse_formula("ROOT:SQRT(C);"), frame, {})
    assert np.allclose(sqrt_result.values["ROOT"], np.sqrt(close), equal_nan=True)


def test_sqrt_of_a_negative_expression_is_nan_not_a_warning() -> None:
    result = evaluate_custom_formula(parse_formula("ROOT:SQRT(C-C-1);"), bars(10), {})

    assert np.isnan(result.values["ROOT"]).all()


def test_std_and_avedev_keep_the_price_scale_flag() -> None:
    """STD/AVEDEV/SQRT 都保留输入的量纲，不能变成可挖掘特征。"""

    def price_scale(source: str) -> set[str]:
        return set(parse_formula(source).price_scale_names)

    tail = " SELL:CROSS(REF(C,1),C);"
    assert price_scale(f"SCORE:STD(C,20);{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:AVEDEV(C,20);{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:SQRT(C);{tail}") == {"SCORE"}
    # 除以轨线之后才是无量纲的。
    assert price_scale(f"SCORE:(C-MA(C,20))/STD(C,20);{tail}") == set()


@pytest.mark.parametrize(
    "formula",
    [
        "LEVEL:STD(C,0);",
        "LEVEL:AVEDEV(C,2.5);",
    ],
)
def test_new_rolling_functions_reject_invalid_periods(formula: str) -> None:
    parsed = parse_formula(formula)

    with pytest.raises(ValueError, match="周期必须是正整数"):
        evaluate_custom_formula(parsed, bars())


def test_price_scale_outputs_are_detected_from_the_expression() -> None:
    """原始价格量纲的输出不能被当成跨股票可比的挖掘特征。

    回归背景：``SCORE:C;`` 会被挖成 ``custom.score <= 7.8`` 这样的绝对阈值，
    实质是“股价低于 7.8 元”，在不同价位的股票之间没有可比性。
    """

    def price_scale(source: str) -> set[str]:
        return set(parse_formula(source).price_scale_names)

    tail = " SELL:CROSS(REF(C,1),C);"
    # 价格/成交量本身，以及任何保持量纲的变换。
    assert price_scale(f"SCORE:C;{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:MA(C,20);{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:(O+H)/2;{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:ABS(C-MA(C,5));{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:IF(C>10,C,0);{tail}") == {"SCORE"}
    assert price_scale(f"SCORE:V;{tail}") == {"SCORE"}
    # 中间变量要把量纲继续传递下去。
    assert price_scale(f"A:C; SCORE:A*2;{tail}") == {"A", "SCORE"}

    # 比值类输出是无量纲的，必须保持可挖掘。
    assert price_scale(f"SCORE:C/MA(C,20);{tail}") == set()
    assert price_scale(f"SCORE:(C-MA(C,20))/MA(C,20);{tail}") == set()
    assert price_scale(f"N:=20; SCORE:C/MA(C,N);{tail}") == set()
    assert price_scale(f"SCORE:COUNT(C>REF(C,1),5);{tail}") == set()
    assert price_scale(f"SCORE:CROSS(C,MA(C,5));{tail}") == set()


def test_custom_formula_metadata_flags_price_scale_outputs() -> None:
    metadata = parse_formula("SCORE:C; RATIO:C/MA(C,20); SELL:CROSS(REF(C,1),C);").metadata()

    flagged = {value["id"]: value["price_scale"] for value in metadata["values"]}

    assert flagged["custom.score"] is True
    assert flagged["custom.ratio"] is False
