"""筛选方案的内容归一化。

存进库的只有「可复现的输入」——结果、任务状态、页码一律不存。归一化是白名单：
不认识的字段直接丢掉，越界的值钳回合法区间，并且**把改了什么写进 notes**——
静默改掉用户存的东西，下次加载的人会以为方案本来就是这样。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

MAX_CONDITIONS = 50
MAX_SIGNALS = 200
MAX_PARAMETERS = 50
MAX_PAYLOAD_BYTES = 32 * 1024
MAX_WORKERS = 8
MAX_TEXT_LENGTH = 8_000

_CONDITION_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"})
_MODES = frozenset({"preset", "custom"})
_RULE_MODES = frozenset({"signals", "conditions"})
_COMBINE_MODES = frozenset({"all", "any", "at_least"})
_UNIVERSES = frozenset({"all", "sh", "sz", "custom"})
_INSTRUMENT_TYPES = frozenset({"stock"})
_BOARDS = frozenset({"main", "star", "chinext"})
_PERIODS = frozenset({"daily"})

DEFAULT_PAYLOAD: dict[str, object] = {
    "mode": "preset",
    "rule_mode": "signals",
    "selected_signals": [],
    "conditions": [],
    "combine_mode": "all",
    "minimum_matches": None,
    "universe": "all",
    "universe_file": "",
    "instrument_types": [],
    "boards": [],
    "workers": 8,
    "period": "daily",
    "formula_text": "",
    "formula_parameters": {},
}


def _text(value: object, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _string_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    # 去重但保持顺序：同一指标被选两次没有意义，顺序却要给前端稳定复现。
    seen: dict[str, None] = {}
    for item in items[:limit]:
        seen.setdefault(item, None)
    return list(seen)


def _number_map(value: object, limit: int) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        if len(result) >= limit:
            break
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        number = float(item)
        if not math.isfinite(number):
            continue
        result[key.strip()] = number
    return result


def _conditions(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    for item in value[:limit]:
        if not isinstance(item, Mapping):
            continue
        left = _text(item.get("left_value"), 128).strip()
        operator = item.get("operator")
        if not left or operator not in _CONDITION_OPERATORS:
            continue
        raw_value = item.get("right_value")
        raw_constant = item.get("right_constant")
        right_value = _text(raw_value, 128).strip() if isinstance(raw_value, str) else None
        right_constant: float | None = None
        if isinstance(raw_constant, (int, float)) and not isinstance(raw_constant, bool):
            candidate = float(raw_constant)
            if math.isfinite(candidate):
                right_constant = candidate
        # 左右都不成立的条件没人能求值：宁可整条丢掉，也不存一个跑不起来的方案。
        if (right_value is None) == (right_constant is None):
            continue
        result.append(
            {
                "left_value": left,
                "operator": str(operator),
                "right_value": right_value,
                "right_constant": right_constant,
            }
        )
    return result


def normalize_payload(value: object) -> tuple[dict[str, object], list[str]]:
    """Whitelist-clip an incoming payload, returning it together with notes."""

    notes: list[str] = []
    source = value if isinstance(value, Mapping) else {}
    if not isinstance(value, Mapping):
        notes.append("方案内容不是对象，已按空方案保存")

    mode = source.get("mode")
    if mode not in _MODES:
        mode = "preset"
    rule_mode = source.get("rule_mode")
    if rule_mode not in _RULE_MODES:
        rule_mode = "signals"
    combine_mode = source.get("combine_mode")
    if combine_mode not in _COMBINE_MODES:
        combine_mode = "all"
    universe = source.get("universe")
    if universe not in _UNIVERSES:
        universe = "all"
    period = source.get("period")
    if period not in _PERIODS:
        period = "daily"

    raw_signals = source.get("selected_signals")
    selected_signals = _string_list(raw_signals, MAX_SIGNALS)
    if isinstance(raw_signals, (list, tuple)) and len(raw_signals) > MAX_SIGNALS:
        notes.append(f"信号数量超过 {MAX_SIGNALS} 个，只保留前 {MAX_SIGNALS} 个")

    raw_conditions = source.get("conditions")
    conditions = _conditions(raw_conditions, MAX_CONDITIONS)
    if isinstance(raw_conditions, (list, tuple)) and len(raw_conditions) > len(conditions):
        dropped = len(raw_conditions) - len(conditions)
        notes.append(f"有 {dropped} 条条件无法求值（指标为空或左右值不合法），已丢弃")

    minimum_matches: int | None = None
    if combine_mode == "at_least":
        raw_minimum = source.get("minimum_matches")
        if isinstance(raw_minimum, (int, float)) and not isinstance(raw_minimum, bool):
            minimum_matches = int(raw_minimum)
        else:
            minimum_matches = 1
        # 至少满足 N 条里 N 不可能超过条件总数，超过的话规则永远不成立。
        upper = max(len(conditions), 1)
        if minimum_matches < 1 or minimum_matches > upper:
            clamped = min(max(minimum_matches, 1), upper)
            notes.append(f"「至少满足数量」超出 1~{upper} 的范围，已调整为 {clamped}")
            minimum_matches = clamped

    workers_raw = source.get("workers")
    workers = 8
    if isinstance(workers_raw, (int, float)) and not isinstance(workers_raw, bool):
        workers = int(workers_raw)
        if workers < 1 or workers > MAX_WORKERS:
            notes.append(
                f"并发数超出 1~{MAX_WORKERS} 的范围，已调整为 {min(max(workers, 1), MAX_WORKERS)}"
            )
            workers = min(max(workers, 1), MAX_WORKERS)

    instrument_types = [
        item
        for item in _string_list(source.get("instrument_types"), len(_INSTRUMENT_TYPES))
        if item in _INSTRUMENT_TYPES
    ]
    boards = [item for item in _string_list(source.get("boards"), len(_BOARDS)) if item in _BOARDS]
    formula_parameters = _number_map(source.get("formula_parameters"), MAX_PARAMETERS)

    payload: dict[str, object] = {
        "mode": mode,
        "rule_mode": rule_mode,
        "selected_signals": selected_signals,
        "conditions": conditions,
        "combine_mode": combine_mode,
        "minimum_matches": minimum_matches,
        "universe": universe,
        "universe_file": _text(source.get("universe_file"), 512),
        "instrument_types": instrument_types,
        "boards": boards,
        "workers": workers,
        "period": period,
        "formula_text": _text(source.get("formula_text")),
        "formula_parameters": formula_parameters,
    }

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        # 到这一步已经是白名单里的字段了，超限只能是公式文本太长——砍它比砍条件
        # 更安全：条件丢一条规则就变了，公式截一段最多是解析失败并当场报错。
        keep = max(MAX_TEXT_LENGTH // 4, 0)
        payload["formula_text"] = str(payload["formula_text"])[:keep]
        payload["formula_parameters"] = {}
        notes.append("方案内容过大，已截断自定义公式文本并清空公式参数")

    known = set(DEFAULT_PAYLOAD)
    unknown = sorted(key for key in source if isinstance(key, str) and key not in known)
    if unknown:
        notes.append(f"忽略了 {len(unknown)} 个未知字段：{'、'.join(unknown[:5])}")

    return payload, notes
