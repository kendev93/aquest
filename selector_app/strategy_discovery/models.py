"""Serializable domain models for the strategy discovery workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

DiscoverySplit = Literal["search", "coarse", "medium", "full", "validation", "holdout"]
RiskMode = Literal["pct", "atr"]
CandidateMode = Literal["strategy", "risk_only"]


def _condition_to_dict(condition: ScreenCondition) -> dict[str, object]:
    return {
        "left_value": condition.left_value,
        "operator": condition.operator,
        "right_value": condition.right_value,
        "right_constant": condition.right_constant,
    }


def rule_to_dict(rule: RuleGroup | None) -> dict[str, object] | None:
    if rule is None:
        return None
    return {
        "signals": list(rule.signals),
        "conditions": [_condition_to_dict(condition) for condition in rule.conditions],
        "required_conditions": [
            _condition_to_dict(condition) for condition in rule.required_conditions
        ],
        "combine_mode": rule.combine_mode,
        "minimum_matches": rule.minimum_matches,
    }


def _normalize_rule(rule: RuleGroup) -> RuleGroup:
    signals = tuple(sorted(set(rule.signals)))
    conditions = tuple(
        sorted(
            rule.conditions,
            key=lambda item: (
                item.left_value,
                item.operator,
                item.right_value or "",
                str(item.right_constant),
            ),
        )
    )
    required_conditions = tuple(
        sorted(
            rule.required_conditions,
            key=lambda item: (
                item.left_value,
                item.operator,
                item.right_value or "",
                str(item.right_constant),
            ),
        )
    )
    return RuleGroup(
        signals=signals,
        conditions=conditions,
        combine_mode=rule.combine_mode,
        minimum_matches=rule.minimum_matches,
        required_conditions=required_conditions,
    )


@dataclass(frozen=True)
class DiscoveryRequest:
    """Normalized inputs for one portfolio strategy discovery run."""

    base_config: Mapping[str, object]
    splits: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    indicator_ids: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    required_buy_conditions: tuple[ScreenCondition, ...] = ()
    required_sell_conditions: tuple[ScreenCondition, ...] = ()
    condition_constants: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    max_conditions_per_side: int = 3
    max_trials: int = 1_000
    timeout_seconds: int = 7_200
    validation_candidates: int = 50
    holdout_candidates: int = 20
    # 与条件研究的 ``min_trades`` 对齐：都是全市场 / 多槽位口径，一个候选在验证
    # 段做出几十笔交易是常态，5 笔太松——那种样本上的夏普、胜率、平均持仓天数
    # 全是噪音，却最容易因为一两笔极端交易顶到榜首。
    min_trades: int = 20
    max_drawdown: float | None = 0.3
    max_turnover: float | None = None
    max_complexity: int = 12
    multifidelity_enabled: bool = True
    convergence_enabled: bool = False
    research_search_policy: Literal["legacy", "adaptive-v1"] = "legacy"
    seed: int = 0
    candidate_mode: CandidateMode = "strategy"

    def __post_init__(self) -> None:
        if not isinstance(self.base_config, Mapping):
            raise ValueError("基础策略配置格式错误")
        object.__setattr__(self, "base_config", dict(self.base_config))
        object.__setattr__(
            self,
            "splits",
            {
                str(name): {str(key): str(value) for key, value in split.items()}
                for name, split in self.splits.items()
            },
        )
        object.__setattr__(self, "indicator_ids", tuple(dict.fromkeys(self.indicator_ids)))
        object.__setattr__(self, "signal_ids", tuple(dict.fromkeys(self.signal_ids)))
        for field_name in ("required_buy_conditions", "required_sell_conditions"):
            conditions = tuple(getattr(self, field_name))
            if len(conditions) > self.max_conditions_per_side:
                raise ValueError(
                    f"{field_name} 不能超过单侧条件上限 {self.max_conditions_per_side}"
                )
            keys = {
                (
                    condition.left_value,
                    condition.operator,
                    condition.right_value,
                    condition.right_constant,
                )
                for condition in conditions
            }
            if len(keys) != len(conditions):
                raise ValueError(f"{field_name} 不能包含重复条件")
            object.__setattr__(self, field_name, conditions)
        object.__setattr__(
            self,
            "condition_constants",
            {
                str(key): tuple(float(value) for value in values)
                for key, values in self.condition_constants.items()
            },
        )
        if not 0 <= self.max_conditions_per_side <= 3:
            raise ValueError("单侧条件数量必须在 0 到 3 之间")
        if not 1 <= self.max_trials <= 5_000:
            raise ValueError("搜索候选数量必须在 1 到 5000 之间")
        if not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("搜索超时时间必须在 1 到 86400 秒之间")
        if not 1 <= self.validation_candidates <= 200:
            raise ValueError("验证候选数量必须在 1 到 200 之间")
        if not 0 <= self.holdout_candidates <= 50:
            raise ValueError("最终留出候选数量必须在 0 到 50 之间")
        if not 0 <= self.min_trades <= 10_000:
            raise ValueError("最少交易数必须在 0 到 10000 之间")
        if self.max_drawdown is not None and not 0 <= self.max_drawdown <= 1:
            raise ValueError("最大回撤阈值必须在 0 到 1 之间")
        if self.max_turnover is not None and self.max_turnover < 0:
            raise ValueError("最大换手率不能小于 0")
        if not 0 <= self.max_complexity <= 50:
            raise ValueError("策略复杂度必须在 0 到 50 之间")
        if self.research_search_policy not in {"legacy", "adaptive-v1"}:
            raise ValueError("研究搜索策略无效")
        if not isinstance(self.convergence_enabled, bool):
            raise ValueError("提前收敛开关必须是布尔值")
        if self.candidate_mode not in {"strategy", "risk_only"}:
            raise ValueError("候选寻优模式无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "base_config": dict(self.base_config),
            "splits": {name: dict(split) for name, split in self.splits.items()},
            "indicator_ids": list(self.indicator_ids),
            "signal_ids": list(self.signal_ids),
            "required_buy_conditions": [
                _condition_to_dict(condition) for condition in self.required_buy_conditions
            ],
            "required_sell_conditions": [
                _condition_to_dict(condition) for condition in self.required_sell_conditions
            ],
            "condition_constants": {
                key: list(values) for key, values in self.condition_constants.items()
            },
            "max_conditions_per_side": self.max_conditions_per_side,
            "max_trials": self.max_trials,
            "timeout_seconds": self.timeout_seconds,
            "validation_candidates": self.validation_candidates,
            "holdout_candidates": self.holdout_candidates,
            "min_trades": self.min_trades,
            "max_drawdown": self.max_drawdown,
            "max_turnover": self.max_turnover,
            "max_complexity": self.max_complexity,
            "multifidelity_enabled": self.multifidelity_enabled,
            "convergence_enabled": self.convergence_enabled,
            "research_search_policy": self.research_search_policy,
            "seed": self.seed,
            "candidate_mode": self.candidate_mode,
        }


@dataclass(frozen=True)
class StrategyCandidate:
    """One complete, human-readable portfolio strategy configuration."""

    buy_rule: RuleGroup
    sell_rule: RuleGroup
    ranking_value: str
    rank_order: Literal["asc", "desc"]
    max_positions: int
    rebalance_frequency: Literal["daily", "weekly", "monthly"] = "daily"
    take_profit_mode: RiskMode = "pct"
    take_profit_pct: float | None = None
    take_profit_atr_multiplier: float | None = None
    take_profit_cooldown_days: int = 0
    stop_loss_mode: RiskMode = "pct"
    stop_loss_pct: float | None = None
    stop_loss_atr_multiplier: float | None = None
    stop_loss_cooldown_days: int = 0
    trailing_stop_mode: RiskMode = "pct"
    trailing_stop_pct: float | None = None
    trailing_stop_atr_multiplier: float | None = None
    trailing_stop_cooldown_days: int = 0
    atr_period: int = 14
    base_config: Mapping[str, object] = field(default_factory=dict)
    complexity: int = 0
    candidate_hash: str = ""
    metrics: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    human_readable_rules: str = ""
    preserve_base_config: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "buy_rule", _normalize_rule(self.buy_rule))
        object.__setattr__(self, "sell_rule", _normalize_rule(self.sell_rule))
        object.__setattr__(self, "base_config", dict(self.base_config))
        object.__setattr__(
            self, "metrics", {key: dict(value) for key, value in self.metrics.items()}
        )
        if not self.candidate_hash:
            object.__setattr__(self, "candidate_hash", _candidate_hash(self))
        if not self.human_readable_rules:
            object.__setattr__(self, "human_readable_rules", _human_readable(self))

    @property
    def config(self) -> dict[str, object]:
        return self.to_config()

    def with_metrics(
        self, split: DiscoverySplit, metrics: Mapping[str, object]
    ) -> StrategyCandidate:
        updated = dict(self.metrics)
        updated[split] = dict(metrics)
        return replace(self, metrics=updated)

    def to_config(self) -> dict[str, object]:
        # ``base_config`` also carries discovery-only metadata such as the UI
        # date preset.  Keep that metadata in the experiment request, but do
        # not pass it to the strict portfolio-backtest schema.
        config = {key: value for key, value in self.base_config.items() if key != "date_mode"}
        if self.preserve_base_config:
            config.update(
                {
                    "take_profit_mode": self.take_profit_mode,
                    "take_profit_pct": self.take_profit_pct,
                    "take_profit_atr_multiplier": self.take_profit_atr_multiplier,
                    "take_profit_cooldown_days": self.take_profit_cooldown_days,
                    "stop_loss_mode": self.stop_loss_mode,
                    "stop_loss_pct": self.stop_loss_pct,
                    "stop_loss_atr_multiplier": self.stop_loss_atr_multiplier,
                    "stop_loss_cooldown_days": self.stop_loss_cooldown_days,
                    "trailing_stop_mode": self.trailing_stop_mode,
                    "trailing_stop_pct": self.trailing_stop_pct,
                    "trailing_stop_atr_multiplier": self.trailing_stop_atr_multiplier,
                    "trailing_stop_cooldown_days": self.trailing_stop_cooldown_days,
                    "atr_period": self.atr_period,
                }
            )
            return config
        config.update(
            {
                "selected_signals": [],
                "combine_mode": "all",
                "minimum_matches": None,
                "buy_rule": rule_to_dict(self.buy_rule),
                "ranking_value": self.ranking_value,
                "rank_order": self.rank_order,
                "max_positions": self.max_positions,
                "rebalance_frequency": self.rebalance_frequency,
                "sell_signal": None,
                "sell_rule": rule_to_dict(self.sell_rule),
                "take_profit_mode": self.take_profit_mode,
                "take_profit_pct": self.take_profit_pct,
                "take_profit_atr_multiplier": self.take_profit_atr_multiplier,
                "take_profit_cooldown_days": self.take_profit_cooldown_days,
                "stop_loss_mode": self.stop_loss_mode,
                "stop_loss_pct": self.stop_loss_pct,
                "stop_loss_atr_multiplier": self.stop_loss_atr_multiplier,
                "stop_loss_cooldown_days": self.stop_loss_cooldown_days,
                "trailing_stop_mode": self.trailing_stop_mode,
                "trailing_stop_pct": self.trailing_stop_pct,
                "trailing_stop_atr_multiplier": self.trailing_stop_atr_multiplier,
                "trailing_stop_cooldown_days": self.trailing_stop_cooldown_days,
                "atr_period": self.atr_period,
            }
        )
        return config

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_hash": self.candidate_hash,
            "complexity": self.complexity,
            "human_readable_rules": self.human_readable_rules,
            "config": self.to_config(),
            "metrics": {key: dict(value) for key, value in self.metrics.items()},
        }


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: StrategyCandidate
    split: DiscoverySplit
    metrics: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "split": self.split,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class DiscoveryRun:
    request: DiscoveryRequest
    results_by_sharpe: tuple[StrategyCandidate, ...]
    results_by_return: tuple[StrategyCandidate, ...]
    search_metadata: Mapping[str, object] = field(default_factory=dict)
    validation_metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        def result_candidate(candidate: StrategyCandidate) -> dict[str, object]:
            row = candidate.to_dict()
            config = row.get("config")
            validation = self.request.splits.get("validation")
            if isinstance(config, dict) and validation is not None:
                config = dict(config)
                config["start_date"] = validation.get("start_date")
                config["end_date"] = validation.get("end_date")
                row["config"] = config
            return row

        return {
            "request": self.request.to_dict(),
            "results_by_sharpe": [result_candidate(item) for item in self.results_by_sharpe],
            "results_by_return": [result_candidate(item) for item in self.results_by_return],
            "search_metadata": dict(self.search_metadata),
            "validation_metadata": dict(self.validation_metadata),
        }


def _candidate_payload(candidate: StrategyCandidate) -> dict[str, object]:
    config = candidate.to_config()
    return config


def _candidate_hash(candidate: StrategyCandidate) -> str:
    payload = json.dumps(
        _candidate_payload(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _human_readable(candidate: StrategyCandidate) -> str:
    from .catalog import default_indicator_catalog

    catalog = default_indicator_catalog()
    operator_labels = {
        "gt": ">",
        "gte": "≥",
        "lt": "<",
        "lte": "≤",
        "eq": "=",
        "neq": "≠",
        "cross_up": "上穿",
        "cross_down": "下穿",
    }

    def describe(rule: RuleGroup) -> str:
        def condition_text(condition: ScreenCondition) -> str:
            right = (
                catalog.display_name(condition.right_value)
                if condition.right_value is not None
                else condition.right_constant
            )
            return (
                f"{catalog.display_name(condition.left_value)} "
                f"{operator_labels.get(condition.operator, condition.operator)} {right}"
            )

        required_parts = [condition_text(condition) for condition in rule.required_conditions]
        generated_parts = [catalog.display_name(signal) for signal in rule.signals]
        generated_parts.extend(condition_text(condition) for condition in rule.conditions)
        required_text = " AND ".join(required_parts)
        generated_text = (
            f" {rule.combine_mode.upper()} ".join(generated_parts) if generated_parts else ""
        )
        if required_text and generated_text:
            return f"{required_text} AND ({generated_text})"
        return required_text or generated_text or "无"

    def risk_text(
        label: str,
        mode: RiskMode,
        percentage: float | None,
        atr_multiplier: float | None,
        cooldown_days: int,
    ) -> str | None:
        if percentage is None and atr_multiplier is None:
            return None
        setting = (
            f"{percentage:.2%}"
            if mode == "pct" and percentage is not None
            else f"ATR×{atr_multiplier:g}"
        )
        return f"{label}{setting}/冷却{cooldown_days}日"

    risk = [
        value
        for value in (
            risk_text(
                "止损",
                candidate.stop_loss_mode,
                candidate.stop_loss_pct,
                candidate.stop_loss_atr_multiplier,
                candidate.stop_loss_cooldown_days,
            ),
            risk_text(
                "止盈",
                candidate.take_profit_mode,
                candidate.take_profit_pct,
                candidate.take_profit_atr_multiplier,
                candidate.take_profit_cooldown_days,
            ),
            risk_text(
                "移动止盈",
                candidate.trailing_stop_mode,
                candidate.trailing_stop_pct,
                candidate.trailing_stop_atr_multiplier,
                candidate.trailing_stop_cooldown_days,
            ),
        )
        if value is not None
    ]
    rank_direction = "高→低" if candidate.rank_order == "desc" else "低→高"
    return (
        f"买入[{describe(candidate.buy_rule)}]；卖出[{describe(candidate.sell_rule)}]；"
        f"排序[{catalog.display_name(candidate.ranking_value)} {rank_direction}]；"
        f"持仓{candidate.max_positions}个；{('、'.join(risk) if risk else '无额外风控')}"
    )
