"""Orchestration for the single-instrument workbench.

这一层只做三件事：拼配置、调既有的回测准备流程、把校验错误翻译成人话。所有的
计算都在 `values.py` / `conditions.py` 里，且**不重新发明任何指标或回测逻辑**——
回测引擎和指标缓存已经被条件研究和适配性评估用着，检验台复用同一条路径，读数才
能和别处的结论对得上。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from aquest.backtest.models import BacktestConfig, BacktestReport
from aquest.backtest.service import BacktestService
from aquest.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from aquest.strategy_discovery.catalog import default_indicator_catalog

from .conditions import (
    ConditionCheckInput,
    check_conditions,
    condition_from_mapping,
    condition_key,
    condition_label,
    significant_conditions,
)
from .models import (
    DEFAULT_ALPHA,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_HORIZON,
    DEFAULT_PERMUTATIONS,
    DEFAULT_SEED,
    MAX_CHECK_CONDITIONS,
    MAX_HORIZON,
    MAX_LIBRARY_CONDITIONS,
    MAX_PERMUTATIONS,
    MAX_VALUE_IDS,
    MIN_HORIZON,
    MIN_PERMUTATIONS,
    ConditionCheckReport,
    InstrumentValues,
    LibraryCondition,
)
from .values import build_instrument_values


class InstrumentLabService:
    """Read one instrument's indicators, rules and mined conditions."""

    def __init__(
        self,
        store: object | None = None,
        *,
        backtest_service: BacktestService | None = None,
        registry: FormulaRegistry | None = None,
    ) -> None:
        self._store = store
        self._backtest = backtest_service or BacktestService()
        self._registry = registry or FORMULA_REGISTRY

    def values(
        self,
        *,
        market: str,
        code: str,
        vipdoc_path: str = "",
        value_ids: Sequence[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> InstrumentValues:
        resolved = self._value_ids(value_ids)
        prepared = self._backtest.prepare(
            self._bare_config(market, code, vipdoc_path, start_date, end_date),
            feature_ids=resolved,
        )
        return build_instrument_values(
            prepared,
            resolved,
            start_date=start_date,
            end_date=end_date,
        )

    def condition_library(self, *, limit: int = MAX_LIBRARY_CONDITIONS) -> list[LibraryCondition]:
        if self._store is None:
            return []
        return significant_conditions(self._store, limit=max(1, limit))

    def check_conditions(
        self,
        *,
        market: str,
        code: str,
        vipdoc_path: str = "",
        conditions: Sequence[Mapping[str, object]],
        horizon: int = DEFAULT_HORIZON,
        start_date: date | None = None,
        end_date: date | None = None,
        permutations: int = DEFAULT_PERMUTATIONS,
        block_size: int = DEFAULT_BLOCK_SIZE,
        seed: int = DEFAULT_SEED,
        alpha: float = DEFAULT_ALPHA,
    ) -> ConditionCheckReport:
        if not conditions:
            raise ValueError("至少选择一条条件")
        if len(conditions) > MAX_CHECK_CONDITIONS:
            raise ValueError(f"一次最多检验 {MAX_CHECK_CONDITIONS} 条条件")
        if not MIN_HORIZON <= horizon <= MAX_HORIZON:
            raise ValueError(f"前瞻天数必须在 {MIN_HORIZON}~{MAX_HORIZON} 之间")
        if not MIN_PERMUTATIONS <= permutations <= MAX_PERMUTATIONS:
            raise ValueError(f"置换次数必须在 {MIN_PERMUTATIONS}~{MAX_PERMUTATIONS} 之间")
        if block_size < 1:
            raise ValueError("置换块大小必须大于 0")
        if not 0 < alpha < 0.5:
            raise ValueError("显著性门槛必须在 0 到 0.5 之间")
        inputs: list[ConditionCheckInput] = []
        feature_ids: list[str] = []
        for payload in conditions:
            condition = condition_from_mapping(payload)
            inputs.append(
                ConditionCheckInput(
                    key=condition_key(condition),
                    label=condition_label(condition),
                    condition=condition,
                )
            )
            feature_ids.extend(condition_value_ids(condition))
        prepared = self._backtest.prepare(
            self._bare_config(market, code, vipdoc_path, start_date, end_date),
            feature_ids=tuple(dict.fromkeys(feature_ids)),
        )
        return check_conditions(
            prepared,
            inputs,
            horizon=horizon,
            start_date=start_date,
            end_date=end_date,
            permutations=permutations,
            block_size=block_size,
            seed=seed,
            alpha=alpha,
            registry=self._registry,
        )

    def backtest(self, config: BacktestConfig) -> BacktestReport:
        """Run the shared single-stock engine; nothing workbench specific."""

        return self._backtest.run(config)

    def _bare_config(
        self,
        market: str,
        code: str,
        vipdoc_path: str,
        start_date: date | None,
        end_date: date | None,
    ) -> BacktestConfig:
        # 「持续满仓 + 持有到期末」让 prepare 的两条规则校验放行：检验台要的只是
        # 指标值和条件命中，不是一遍真实回测。
        return BacktestConfig(
            market=market,  # type: ignore[arg-type]
            code=code,
            vipdoc_path=vipdoc_path,
            always_in_market=True,
            close_at_end=True,
            start_date=start_date,
            end_date=end_date,
        )

    def _value_ids(self, value_ids: Sequence[str]) -> tuple[str, ...]:
        catalog = default_indicator_catalog()
        resolved = tuple(dict.fromkeys(catalog.canonical_id(item) for item in value_ids))
        if not resolved:
            raise ValueError("至少选择一个指标")
        if len(resolved) > MAX_VALUE_IDS:
            raise ValueError(f"一次最多查看 {MAX_VALUE_IDS} 个指标")
        unknown = tuple(item for item in resolved if not catalog.has_value(item))
        if unknown:
            raise ValueError(f"未知指标: {', '.join(unknown)}")
        return resolved


def condition_value_ids(condition: object) -> tuple[str, ...]:
    """Numeric outputs a condition needs, canonicalised like the catalog does."""

    catalog = default_indicator_catalog()
    values: list[str] = []
    left = getattr(condition, "left_value", None)
    right = getattr(condition, "right_value", None)
    for value in (left, right):
        if isinstance(value, str) and value.strip():
            values.append(catalog.canonical_id(value.strip()))
    return tuple(values)
