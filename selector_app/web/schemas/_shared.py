"""各请求体共用的校验函数与常量。

原先和 30 个 Request 类挤在一个文件里，改一个字段要在 1500 行里翻找。拆包之后
这里只留它们真正共用的东西：公式校验、标的范围校验、策略发现的日期默认值，
以及夹在两个 Request 之间的 _rankable_research_values。
"""

from __future__ import annotations

import math
from datetime import date

from selector_app.formulas.custom import FormulaParseError, custom_output_id, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.scope import InstrumentScope
from selector_app.screening.models import ScreenCondition
from selector_app.strategy_discovery.catalog import default_indicator_catalog

from ..date_ranges import (
    DateRangeMode,
    resolve_date_range,
)


def validate_formula_signals(
    formula_text: str | None,
    signal_ids: list[str],
    formula_parameters: dict[str, float],
) -> None:
    """Validate signal ids and parameter overrides for preset/custom formulas."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {signal.id for signal in parsed.signals}
        invalid = [signal for signal in signal_ids if signal not in available]
        known_parameters = {parameter.name for parameter in parsed.parameters}
        unknown_parameters = sorted(set(formula_parameters) - known_parameters)
        if unknown_parameters:
            raise ValueError(f"未知公式参数: {', '.join(unknown_parameters)}")
        parameters_by_name = {parameter.name: parameter for parameter in parsed.parameters}
        for name, value in formula_parameters.items():
            definition = parameters_by_name[name]
            if not math.isfinite(value) or not definition.minimum <= value <= definition.maximum:
                raise ValueError(
                    f"参数 {name} 必须在 {definition.minimum:g} 到 {definition.maximum:g} 之间"
                )
            if definition.step == 1 and not value.is_integer():
                raise ValueError(f"参数 {name} 必须是整数")
    else:
        invalid = [signal for signal in signal_ids if not FORMULA_REGISTRY.has_signal(signal)]
        if formula_parameters:
            raise ValueError("预置公式不接受自定义参数")
    if invalid:
        raise ValueError(f"未知选股信号: {', '.join(invalid)}")


def validate_formula_values(formula_text: str | None, value_ids: list[str]) -> None:
    """Validate numeric output identifiers used for ranking or exits."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {custom_output_id(name) for name in parsed.value_names}
        invalid = [value_id for value_id in value_ids if value_id not in available]
    else:
        invalid = [value_id for value_id in value_ids if not FORMULA_REGISTRY.has_value(value_id)]
    if invalid:
        raise ValueError(f"未知指标输出: {', '.join(invalid)}")


StrategyDiscoveryDateMode = DateRangeMode
DEFAULT_STRATEGY_DISCOVERY_LOOKBACK_YEARS = 5


def resolve_strategy_discovery_defaults(
    data_start: date | str | None,
    data_end: date | str | None,
    mode: StrategyDiscoveryDateMode = "recent5",
) -> tuple[date | None, date | None]:
    """Resolve a preset range against the latest completed local bar."""
    return resolve_date_range(
        mode=mode,
        requested_start=None,
        requested_end=None,
        available_start=data_start,
        available_end=data_end,
    )


def validate_formula_conditions(
    formula_text: str | None, conditions: list[ScreenCondition]
) -> None:
    """Validate every numeric output referenced by a screening condition."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {custom_output_id(name) for name in parsed.value_names}
        invalid = [
            value_id
            for condition in conditions
            for value_id in (condition.left_value, condition.right_value)
            if value_id is not None and value_id not in available
        ]
    else:
        invalid = [
            value_id
            for condition in conditions
            for value_id in (condition.left_value, condition.right_value)
            if value_id is not None and not FORMULA_REGISTRY.has_value(value_id)
        ]
    if invalid:
        raise ValueError(f"未知指标输出: {', '.join(dict.fromkeys(invalid))}")


def validate_instrument_scope(
    universe: str,
    instrument_types: list[InstrumentType] | None,
    boards: list[InstrumentBoard] | None,
) -> None:
    InstrumentScope.from_values(
        universe=universe,
        instrument_types=instrument_types,
        boards=boards,
    )


def _rankable_research_values(formula_text: str | None) -> frozenset[str]:
    """Numeric outputs that may be used as a cross-stock ranking key.

    Mirrors ``ConditionResearchService._feature_ids``: a custom formula decides
    its own outputs (minus raw price/volume levels), otherwise the shared
    catalog decides which built-in values are comparable across stocks.
    """

    if formula_text:
        parsed = parse_formula(formula_text)
        return frozenset(
            custom_output_id(name)
            for name in parsed.value_names
            if name not in parsed.price_scale_names
        )
    return frozenset(default_indicator_catalog().rankable_ids)
