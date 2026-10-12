from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from aquest.adapters.local_day_adapter import StockRef
from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.formulas.types import FormulaResult
from aquest.screening.engine import ScreenEngine, evaluate_rule_group
from aquest.screening.models import ScanConfig, ScreenCondition
from aquest.screening.rules import RuleGroup
from aquest.web.schemas import FormulaScreenRequest


def _bars(closes: list[float]) -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(len(close), 1000.0),
            "amount": close * 1000,
        }
    )


class _Adapter:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.ref = StockRef("SH", "600000", Path("/tmp/sh600000.day"))
        self.frame = frame

    def list_stock_refs(self, vipdoc_path: str, universe: str, universe_file: str | None = None):
        return [self.ref]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frame.copy(deep=True)


def _config(
    *conditions: ScreenCondition, formula_text: str | None = "A:=C; B:=MA(C,2); SIGNAL:C>0;"
) -> ScanConfig:
    return ScanConfig(
        selected_signals=(),
        conditions=conditions,
        combine_mode="all",
        minimum_matches=None,
        universe="all",
        universe_file=None,
        vipdoc_path="/tmp/vipdoc",
        workers=1,
        period="daily",
        formula_text=formula_text,
    )


def test_engine_compares_numeric_outputs_and_can_compare_two_outputs() -> None:
    condition = ScreenCondition(
        left_value="custom.a",
        operator="gt",
        right_value="custom.b",
    )

    report = ScreenEngine(_Adapter(_bars([1, 2, 3, 4]))).scan(_config(condition))

    assert report.errors == 0
    assert report.total_signals == 1
    assert report.results[0].matched_signals == ("condition_1",)


def test_engine_reads_numeric_outputs_from_a_preset_indicator() -> None:
    condition = ScreenCondition(
        left_value="indicator_two.var1",
        operator="eq",
        right_constant=1,
    )

    report = ScreenEngine(_Adapter(_bars([1, 2, 3, 4] * 40))).scan(
        _config(condition, formula_text=None)
    )

    assert report.errors == 0
    assert report.total_signals == 1
    assert report.results[0].matched_signals == ("condition_1",)


def test_registry_exposes_kline_prices_as_condition_operands() -> None:
    frame = _bars([1, 2, 3, 4])
    result = FORMULA_REGISTRY.formula("price").calculate(frame)

    assert FORMULA_REGISTRY.has_value("price.close")
    assert FORMULA_REGISTRY.has_value("price.open")
    assert result.values["close"].tolist() == [1.0, 2.0, 3.0, 4.0]
    assert result.values["high"].tolist() == [1.5, 2.5, 3.5, 4.5]
    assert result.values["low"].tolist() == [0.5, 1.5, 2.5, 3.5]


def test_engine_compares_a_price_with_an_indicator() -> None:
    condition = ScreenCondition(
        left_value="price.close",
        operator="cross_up",
        right_value="technical.ma5",
    )

    report = ScreenEngine(_Adapter(_bars([1, 1, 1, 1, 1, 10]))).scan(
        _config(condition, formula_text=None)
    )

    assert report.errors == 0
    assert report.total_signals == 1
    assert report.results[0].matched_signals == ("condition_1",)
    assert report.results[0].indicator_values["price.close"] == 10.0


@pytest.mark.parametrize(
    ("operator", "closes", "threshold"),
    [
        ("cross_up", [1, 2, 3, 4], 3),
        ("cross_down", [4, 3, 2, 1], 2),
    ],
)
def test_engine_evaluates_crossing_conditions(
    operator: str, closes: list[float], threshold: float
) -> None:
    condition = ScreenCondition(
        left_value="custom.a",
        operator=operator,
        right_constant=threshold,
    )

    report = ScreenEngine(_Adapter(_bars(closes))).scan(_config(condition))

    assert report.errors == 0
    assert report.total_signals == 1
    assert report.results[0].matched_signals == ("condition_1",)


def test_rule_group_ands_required_conditions_with_or_generated_conditions() -> None:
    required = ScreenCondition(
        left_value="technical.ma5",
        operator="gt",
        right_constant=10.0,
    )
    generated_one = ScreenCondition(
        left_value="technical.ma20",
        operator="gt",
        right_constant=20.0,
    )
    generated_two = ScreenCondition(
        left_value="technical.ma20",
        operator="lt",
        right_constant=5.0,
    )
    rule = RuleGroup(
        conditions=(generated_one, generated_two),
        combine_mode="any",
        required_conditions=(required,),
    )
    formula_results = {
        "technical": FormulaResult.build(
            formula_id="technical",
            display_name="technical",
            minimum_bars=1,
            recommended_bars=1,
            data_length=4,
            values={
                "ma5": [11.0, 9.0, 11.0, 11.0],
                "ma20": [21.0, 21.0, 4.0, 4.0],
            },
            signals={},
        )
    }

    mask = evaluate_rule_group(rule, formula_results, FORMULA_REGISTRY)

    assert mask.tolist() == [True, False, True, True]


def test_formula_screen_request_accepts_conditions_without_selected_signals() -> None:
    request = FormulaScreenRequest.model_validate(
        {
            "selected_signals": [],
            "conditions": [
                {
                    "left_value": "custom.a",
                    "operator": "gte",
                    "right_constant": 3,
                }
            ],
            "combine_mode": "all",
            "universe": "all",
            "workers": 1,
            "period": "daily",
            "formula_text": "A:=C; SIGNAL:C>0;",
        }
    )

    config = request.to_config()

    assert config.selected_signals == ()
    assert config.conditions[0].left_value == "custom.a"
    assert config.conditions[0].right_constant == 3


def test_formula_screen_request_accepts_a_preset_indicator_condition() -> None:
    request = FormulaScreenRequest.model_validate(
        {
            "selected_signals": [],
            "conditions": [
                {
                    "left_value": "indicator_two.var1",
                    "operator": "eq",
                    "right_constant": 1,
                }
            ],
            "combine_mode": "all",
            "universe": "all",
            "workers": 1,
            "period": "daily",
        }
    )

    assert request.to_config().conditions[0].left_value == "indicator_two.var1"


def test_preset_metadata_exposes_only_the_requested_indicator_outputs() -> None:
    expected = {
        "indicator_one": [
            "indicator_one.wash",
            "indicator_one.main_force_raise",
            "indicator_one.distribution",
            "indicator_one.main_force_entry",
            "indicator_one.var3",
            "indicator_one.z",
        ],
        "indicator_two": [
            "indicator_two.short_line",
            "indicator_two.mid_line",
            "indicator_two.bottom_line",
            "indicator_two.saturation",
            "indicator_two.holding",
            "indicator_two.warning_line",
        ],
        "indicator_three": [
            "indicator_three.var1",
            "indicator_three.var2",
            "indicator_three.stochastic_base",
            "indicator_three.varo7",
            "indicator_three.prepare_rally",
            "indicator_three.suppress_main",
        ],
    }
    metadata = {item["id"]: item for item in FORMULA_REGISTRY.metadata()}

    assert [metadata[formula_id]["display_name"] for formula_id in expected] == [
        "主力清洗",
        "庄家顶底",
        "准备拉升",
    ]
    for formula_id, value_ids in expected.items():
        assert [value["id"] for value in metadata[formula_id]["values"]] == value_ids


def test_formula_screen_request_rejects_a_condition_without_right_operand() -> None:
    with pytest.raises(ValidationError, match="右值"):
        FormulaScreenRequest.model_validate(
            {
                "selected_signals": [],
                "conditions": [{"left_value": "indicator_three.var1", "operator": "gt"}],
                "universe": "all",
                "workers": 1,
                "period": "daily",
            }
        )
