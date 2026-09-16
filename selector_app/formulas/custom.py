"""Safe parsing and evaluation of a bounded subset of TongDaXin formulas.

The parser deliberately interprets an AST instead of executing user-provided
Python.  It supports the array functions used by the preset indicators and
turns named ``OUTPUT:EXPRESSION`` statements into selectable custom signals.
Explicit scalar assignments such as ``N:=5`` become editable parameters.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd

from .common import MYTT, REQUIRED_BAR_COLUMNS, dynamic_ref, safe_divide
from .types import FormulaResult

MAX_FORMULA_LENGTH = 20_000
MAX_STATEMENTS = 100
MAX_AST_NODES = 300

_NAME_PATTERN = re.compile(r"^[A-Za-z_\u3400-\u9fff][\w\u3400-\u9fff]*$")
_NUMERIC_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_PARAMETER_MIN = 1.0
_PARAMETER_MAX = 10_000.0
_ALLOWED_FUNCTIONS = frozenset(
    {
        "ABS",
        "BARSLAST",
        "COUNT",
        "CROSS",
        "DRAWNULL",
        "EMA",
        "HHV",
        "IF",
        "LLV",
        "MA",
        "MAX",
        "MIN",
        "REF",
        "SMA",
        "SUM",
    }
)
_PERIOD_FUNCTIONS = frozenset({"REF", "SMA", "EMA", "MA", "LLV", "HHV", "SUM", "COUNT"})

# Formula variables that carry a raw per-stock level (price in yuan, volume in
# shares, amount in yuan) instead of a dimensionless ratio.
_LEVEL_VARIABLES = frozenset(
    {
        "C",
        "CLOSE",
        "O",
        "OPEN",
        "H",
        "HIGH",
        "L",
        "LOW",
        "V",
        "VOL",
        "VOLUME",
        "AMOUNT",
    }
)
# Functions whose result keeps the physical scale of their input, so a level
# argument makes the whole call a level again. Anything outside this set
# (CROSS/COUNT/BARSLAST/DRAWNULL) yields a flag or count and is dimensionless.
_SCALE_PRESERVING_FUNCTIONS = frozenset(
    {"ABS", "EMA", "HHV", "IF", "LLV", "MA", "MAX", "MIN", "REF", "SMA", "SUM"}
)


def _is_level_expression(node: ast.AST, levels: Mapping[str, bool]) -> bool:
    """Report whether an expression evaluates to a raw price/volume level.

    Constants and parameters are dimensionless, so they never make a result a
    level on their own; ``C / MA(C, 20)`` is a ratio because the division
    cancels the price unit, while ``(C - MA(C, 20)) / 2`` is still a price.
    """

    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Name):
        if node.id.upper() in _LEVEL_VARIABLES:
            return True
        return bool(levels.get(node.id, False))
    if isinstance(node, ast.UnaryOp):
        return _is_level_expression(node.operand, levels)
    if isinstance(node, ast.BinOp):
        left = _is_level_expression(node.left, levels)
        right = _is_level_expression(node.right, levels)
        if isinstance(node.op, ast.Div):
            # A level divided by another level is a ratio.
            return left and not right
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Pow, ast.Mod)):
            return left or right
        return False
    if isinstance(node, ast.Call):
        function_name = node.func.id.upper() if isinstance(node.func, ast.Name) else ""
        if function_name not in _SCALE_PRESERVING_FUNCTIONS:
            return False
        return any(_is_level_expression(argument, levels) for argument in node.args)
    return False


class FormulaParseError(ValueError):
    """User-correctable formula syntax or safety error."""


@dataclass(frozen=True)
class FormulaParameter:
    name: str
    default: float
    minimum: float
    maximum: float
    step: float


@dataclass(frozen=True)
class CustomSignalDefinition:
    id: str
    display_name: str
    description: str


@dataclass(frozen=True)
class _Statement:
    target: str | None
    expression: str
    tree: ast.Expression
    signal_id: str | None


@dataclass(frozen=True)
class ParsedFormula:
    source: str
    parameters: tuple[FormulaParameter, ...]
    signals: tuple[CustomSignalDefinition, ...]
    statements: tuple[_Statement, ...]
    minimum_bars: int

    @property
    def value_names(self) -> tuple[str, ...]:
        """All named scalar/series outputs that can be used for ranking."""

        return tuple(
            [parameter.name for parameter in self.parameters]
            + [statement.target for statement in self.statements if statement.target is not None]
        )

    @property
    def price_scale_names(self) -> frozenset[str]:
        """Outputs that are still a raw price/volume level, not a ratio.

        Mining a cross-stock threshold on such an output really means "share
        price below 7.8 yuan", which is meaningless across stocks with
        different price levels.  They stay usable as condition operands but
        must never become mined features.
        """

        levels: dict[str, bool] = {}
        for statement in self.statements:
            if statement.target is None:
                continue
            levels[statement.target] = _is_level_expression(statement.tree.body, levels)
        return frozenset(name for name, is_level in levels.items() if is_level)

    def metadata(self) -> dict[str, object]:
        price_scale = self.price_scale_names
        return {
            "parameters": [
                {
                    "name": parameter.name,
                    "default": parameter.default,
                    "minimum": parameter.minimum,
                    "maximum": parameter.maximum,
                    "step": parameter.step,
                }
                for parameter in self.parameters
            ],
            "signals": [
                {
                    "id": signal.id,
                    "display_name": signal.display_name,
                    "description": signal.description,
                }
                for signal in self.signals
            ],
            "values": [
                {
                    "id": custom_output_id(name),
                    "display_name": name,
                    "description": f"{name} 的数值输出",
                    "price_scale": name in price_scale,
                }
                for name in self.value_names
            ],
            "minimum_bars": self.minimum_bars,
            "warnings": [],
        }


def parse_formula(source: str) -> ParsedFormula:
    if not isinstance(source, str) or not source.strip():
        raise FormulaParseError("公式不能为空")
    if len(source) > MAX_FORMULA_LENGTH:
        raise FormulaParseError(f"公式长度不能超过 {MAX_FORMULA_LENGTH} 个字符")

    statements = _split_statements(_strip_comments(source))
    if not statements:
        raise FormulaParseError("公式没有可计算语句")
    if len(statements) > MAX_STATEMENTS:
        raise FormulaParseError(f"公式语句不能超过 {MAX_STATEMENTS} 条")

    parameters: list[FormulaParameter] = []
    parsed_statements: list[_Statement] = []
    signals: list[CustomSignalDefinition] = []
    seen_names: set[str] = set()
    seen_signal_ids: set[str] = set()

    for raw_statement in statements:
        target, assignment, expression = _parse_statement(raw_statement)
        if target is not None and target in seen_names:
            raise FormulaParseError(f"变量重复定义: {target}")
        if target is not None:
            seen_names.add(target)
        expression = _strip_drawing_suffix(expression).strip()
        if not expression:
            raise FormulaParseError(f"语句缺少表达式: {raw_statement}")
        tree = _parse_expression(expression)

        if assignment == ":=" and target is not None and _NUMERIC_PATTERN.fullmatch(expression):
            value = float(expression)
            if not math.isfinite(value) or value < _PARAMETER_MIN or value > _PARAMETER_MAX:
                raise FormulaParseError(f"参数 {target} 必须在 1 到 {_PARAMETER_MAX} 之间")
            if any(parameter.name == target for parameter in parameters):
                raise FormulaParseError(f"参数重复定义: {target}")
            step = 1.0 if value.is_integer() else 0.1
            parameters.append(
                FormulaParameter(
                    name=target,
                    default=value,
                    minimum=_PARAMETER_MIN,
                    maximum=_PARAMETER_MAX,
                    step=step,
                )
            )
            continue

        signal_id = None
        if (
            assignment == ":"
            or target is None
            or (assignment == ":=" and target is not None and _looks_like_signal(tree))
        ):
            signal_name = target or "custom_signal"
            signal_id = _signal_id(signal_name)
            if signal_id in seen_signal_ids:
                raise FormulaParseError(f"输出信号重复定义: {signal_name}")
            seen_signal_ids.add(signal_id)
            signals.append(
                CustomSignalDefinition(
                    id=signal_id,
                    display_name=signal_name,
                    description=f"{signal_name} 的最后一根 K 线输出不为 0",
                )
            )
        parsed_statements.append(
            _Statement(target=target, expression=expression, tree=tree, signal_id=signal_id)
        )

    if not signals:
        raise FormulaParseError("公式至少需要一个可选输出，例如 SIGNAL:CROSS(C,REF(C,1))")

    minimum_bars = _minimum_bars(parsed_statements, {p.name: p.default for p in parameters})
    return ParsedFormula(
        source=source,
        parameters=tuple(parameters),
        signals=tuple(signals),
        statements=tuple(parsed_statements),
        minimum_bars=minimum_bars,
    )


def evaluate_custom_formula(
    parsed: ParsedFormula,
    frame: pd.DataFrame,
    parameter_values: Mapping[str, float] | None = None,
) -> FormulaResult:
    missing = [column for column in REQUIRED_BAR_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
    parameters = resolve_formula_parameters(parsed, parameter_values)
    required_bars = required_bars_for_parameters(parsed, parameters)
    length = len(frame)
    env: dict[str, object] = {
        "C": _column(frame, "close"),
        "CLOSE": _column(frame, "close"),
        "O": _column(frame, "open"),
        "OPEN": _column(frame, "open"),
        "H": _column(frame, "high"),
        "HIGH": _column(frame, "high"),
        "L": _column(frame, "low"),
        "LOW": _column(frame, "low"),
        "V": _column(frame, "volume"),
        "VOL": _column(frame, "volume"),
        "VOLUME": _column(frame, "volume"),
        "AMOUNT": _column(frame, "amount"),
    }
    env.update(parameters)
    values: dict[str, np.ndarray] = {
        name: np.full(length, value, dtype=float) for name, value in parameters.items()
    }
    signals: dict[str, np.ndarray] = {}

    for statement in parsed.statements:
        raw_value = _evaluate_node(statement.tree.body, env)
        value = _broadcast(raw_value, length)
        if statement.target is not None:
            env[statement.target] = value
            values[statement.target] = value
        if statement.signal_id is not None:
            signals[statement.signal_id] = _signal_mask(value)

    return FormulaResult.build(
        formula_id="custom",
        display_name="自定义公式",
        minimum_bars=required_bars,
        recommended_bars=max(required_bars, 120),
        data_length=length,
        values=values,
        signals=signals,
    )


def resolve_formula_parameters(
    parsed: ParsedFormula,
    overrides: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Validate overrides and return the complete parameter set in formula order."""

    return _parameter_values(parsed, overrides or {})


def required_bars_for_parameters(
    parsed: ParsedFormula,
    parameters: Mapping[str, float],
) -> int:
    """Return the causal warm-up length for one resolved parameter set."""

    return _minimum_bars(parsed.statements, parameters, parsed.minimum_bars)


def _strip_comments(source: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in source.splitlines())


def _split_statements(source: str) -> list[str]:
    statements: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(source):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise FormulaParseError("括号不匹配")
        elif character == ";" and depth == 0:
            value = source[start:index].strip()
            if value:
                statements.append(value)
            start = index + 1
    if depth != 0:
        raise FormulaParseError("括号不匹配")
    tail = source[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def _parse_statement(statement: str) -> tuple[str | None, str | None, str]:
    match = re.match(
        r"^\s*([A-Za-z_\u3400-\u9fff][\w\u3400-\u9fff]*)\s*(:=|:)(.*)$",
        statement,
        flags=re.DOTALL,
    )
    if not match:
        return None, None, statement
    target, assignment, expression = match.groups()
    if not _NAME_PATTERN.fullmatch(target):
        raise FormulaParseError(f"变量名无效: {target}")
    return target, assignment, expression


def _strip_drawing_suffix(expression: str) -> str:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(expression):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(expression[start:index].strip())
            start = index + 1
    parts.append(expression[start:].strip())
    return parts[0]


def _parse_expression(expression: str) -> ast.Expression:
    translated = expression.replace("<>", "!=").replace("^", "**")
    translated = re.sub(r"\bAND\b", " and ", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bOR\b", " or ", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bNOT\b", " not ", translated, flags=re.IGNORECASE)
    translated = re.sub(r"(?<![<>=!])=(?!=)", "==", translated)
    translated = " ".join(translated.split())
    try:
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as exc:
        raise FormulaParseError(f"表达式无法解析: {expression}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise FormulaParseError(f"表达式复杂度不能超过 {MAX_AST_NODES} 个语法节点")
    for node in nodes:
        _validate_node(node)
    return tree


def _validate_node(node: ast.AST) -> None:
    allowed = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.UnaryOp,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.Mod,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Call,
    )
    if not isinstance(node, allowed):
        raise FormulaParseError(f"不支持的表达式结构: {type(node).__name__}")
    if isinstance(node, ast.Name):
        if not _NAME_PATTERN.fullmatch(node.id) or node.id.startswith("__"):
            raise FormulaParseError(f"变量名不安全: {node.id}")
    if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
        raise FormulaParseError("公式不允许字符串或其他常量")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id.upper() not in _ALLOWED_FUNCTIONS:
            raise FormulaParseError("公式调用了不支持的函数")
        if node.keywords:
            raise FormulaParseError("公式函数不支持关键字参数")
        function_name = node.func.id.upper()
        arity = {
            "ABS": (1, 1),
            "BARSLAST": (1, 1),
            "COUNT": (2, 2),
            "CROSS": (2, 2),
            "DRAWNULL": (0, 0),
            "EMA": (2, 2),
            "HHV": (2, 2),
            "IF": (3, 3),
            "LLV": (2, 2),
            "MA": (2, 2),
            "MAX": (2, 2),
            "MIN": (2, 2),
            "REF": (2, 2),
            "SMA": (2, 3),
            "SUM": (2, 2),
        }[function_name]
        if not arity[0] <= len(node.args) <= arity[1]:
            raise FormulaParseError(f"函数 {function_name} 参数数量不正确")


def _signal_id(name: str) -> str:
    return custom_output_id(name)


def custom_output_id(name: str) -> str:
    """Return the stable API id for a named custom formula output."""

    suffix = name.lower() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else name
    return f"custom.{suffix}"


def _looks_like_signal(tree: ast.Expression) -> bool:
    """Treat boolean assignments (common in TDX formulas) as outputs too."""

    expression = tree.body
    if isinstance(expression, (ast.Compare, ast.BoolOp)):
        return True
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return True
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id.upper() == "CROSS"
    )


def _parameter_values(
    parsed: ParsedFormula,
    overrides: Mapping[str, float],
) -> dict[str, float]:
    known = {parameter.name: parameter for parameter in parsed.parameters}
    unknown = sorted(set(overrides) - set(known))
    if unknown:
        raise ValueError(f"未知公式参数: {', '.join(unknown)}")
    values: dict[str, float] = {}
    for name, parameter in known.items():
        raw = overrides.get(name, parameter.default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"参数 {name} 必须是数字")
        value = float(raw)
        if not math.isfinite(value) or not parameter.minimum <= value <= parameter.maximum:
            raise ValueError(
                f"参数 {name} 必须在 {parameter.minimum:g} 到 {parameter.maximum:g} 之间"
            )
        if parameter.step == 1.0 and not value.is_integer():
            raise ValueError(f"参数 {name} 必须是整数")
        values[name] = value
    return values


def _minimum_bars(
    statements: tuple[_Statement, ...] | list[_Statement],
    parameters: Mapping[str, float],
    baseline: int = 1,
) -> int:
    required = max(1, baseline)
    for statement in statements:
        for node in ast.walk(statement.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            function_name = node.func.id.upper()
            if function_name not in _PERIOD_FUNCTIONS or len(node.args) < 2:
                continue
            period_node = node.args[1]
            period: float | None = None
            if isinstance(period_node, ast.Constant) and isinstance(
                period_node.value, (int, float)
            ):
                period = float(period_node.value)
            elif isinstance(period_node, ast.Name):
                period = parameters.get(period_node.id)
            if period is not None and math.isfinite(period):
                required = max(required, math.ceil(period) + (1 if function_name == "REF" else 0))
    return required


def _column(frame: pd.DataFrame, name: str) -> np.ndarray:
    return cast(np.ndarray, frame[name].to_numpy(dtype=float, copy=True))


def _broadcast(value: object, length: int) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim == 0:
        return np.full(length, float(array), dtype=float)
    if len(array) != length:
        raise ValueError("公式表达式返回了错误长度的数组")
    return np.asarray(array, dtype=float)


def _signal_mask(value: np.ndarray) -> np.ndarray:
    if value.dtype == bool:
        return value.astype(bool, copy=True)
    return cast(np.ndarray, np.isfinite(value) & (value != 0))


def _scalar(value: object) -> float:
    array = np.asarray(value)
    if array.ndim != 0:
        raise ValueError("函数周期参数必须是常量")
    return float(array)


def _period(value: object, function_name: str, *, allow_zero: bool = False) -> int:
    period = _scalar(value)
    minimum = 0 if allow_zero else 1
    if not math.isfinite(period) or not period.is_integer() or period < minimum:
        qualifier = "非负整数" if allow_zero else "正整数"
        raise ValueError(f"{function_name} 的周期必须是{qualifier}")
    return int(period)


def _positive_scalar(value: object, function_name: str) -> float:
    period = _scalar(value)
    if not math.isfinite(period) or period <= 0:
        raise ValueError(f"{function_name} 的周期必须是正数")
    return period


def _evaluate_node(node: ast.AST, env: Mapping[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        uppercase = node.id.upper()
        if uppercase in env:
            return env[uppercase]
        raise ValueError(f"公式引用了未定义变量: {node.id}")
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.USub):
            return -np.asarray(value)
        if isinstance(node.op, ast.Not):
            return np.logical_not(np.asarray(value, dtype=bool))
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, env)
        right = _evaluate_node(node.right, env)
        return _binary(node.op, left, right)
    if isinstance(node, ast.BoolOp):
        values = [_evaluate_node(value, env) for value in node.values]
        bool_result = np.asarray(values[0], dtype=bool)
        for value in values[1:]:
            if isinstance(node.op, ast.And):
                bool_result = np.logical_and(bool_result, np.asarray(value, dtype=bool))
            else:
                bool_result = np.logical_or(bool_result, np.asarray(value, dtype=bool))
        return bool_result
    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, env)
        comparison_result: np.ndarray | None = None
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate_node(comparator, env)
            current = _compare(operator, left, right)
            comparison_result = (
                current if comparison_result is None else np.logical_and(comparison_result, current)
            )
            left = right
        return comparison_result if comparison_result is not None else False
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FormulaParseError("公式函数名无效")
        function_name = node.func.id.upper()
        arguments = [_evaluate_node(argument, env) for argument in node.args]
        return _call(function_name, arguments)
    raise FormulaParseError(f"不支持的表达式结构: {type(node).__name__}")


def _binary(operator: ast.operator, left: object, right: object) -> object:
    if isinstance(operator, ast.Add):
        return np.asarray(left) + np.asarray(right)
    if isinstance(operator, ast.Sub):
        return np.asarray(left) - np.asarray(right)
    if isinstance(operator, ast.Mult):
        return np.asarray(left) * np.asarray(right)
    if isinstance(operator, ast.Div):
        return safe_divide(left, right)
    if isinstance(operator, ast.Pow):
        return np.power(np.asarray(left), np.asarray(right))
    if isinstance(operator, ast.Mod):
        return np.mod(np.asarray(left), np.asarray(right))
    raise FormulaParseError("公式包含不支持的运算符")


def _compare(operator: ast.cmpop, left: object, right: object) -> np.ndarray:
    left_array = np.asarray(left)
    right_array = np.asarray(right)
    if isinstance(operator, ast.Eq):
        return np.asarray(left_array == right_array, dtype=bool)
    if isinstance(operator, ast.NotEq):
        return np.asarray(left_array != right_array, dtype=bool)
    if isinstance(operator, ast.Lt):
        return np.asarray(left_array < right_array, dtype=bool)
    if isinstance(operator, ast.LtE):
        return np.asarray(left_array <= right_array, dtype=bool)
    if isinstance(operator, ast.Gt):
        return np.asarray(left_array > right_array, dtype=bool)
    if isinstance(operator, ast.GtE):
        return np.asarray(left_array >= right_array, dtype=bool)
    raise FormulaParseError("公式包含不支持的比较符")


def _call(name: str, arguments: list[object]) -> object:
    if name == "ABS":
        return np.abs(np.asarray(arguments[0]))
    if name == "MAX":
        return np.maximum(np.asarray(arguments[0]), np.asarray(arguments[1]))
    if name == "MIN":
        return np.minimum(np.asarray(arguments[0]), np.asarray(arguments[1]))
    if name == "IF":
        return np.where(
            np.asarray(arguments[0]),
            np.asarray(arguments[1]),
            np.asarray(arguments[2]),
        )
    if name == "REF":
        source = np.asarray(arguments[0], dtype=float)
        period_value = np.asarray(arguments[1])
        if period_value.ndim == 0:
            return MYTT.REF(source, _period(period_value, "REF", allow_zero=True))
        periods = np.asarray(period_value, dtype=float)
        if len(periods) != len(source):
            raise ValueError("REF 的动态周期长度必须与行情数据一致")
        if np.isfinite(periods).any() and (periods[np.isfinite(periods)] < 0).any():
            raise ValueError("REF 的动态周期必须是非负数")
        finite_periods = periods[np.isfinite(periods)]
        if not np.equal(finite_periods, np.floor(finite_periods)).all():
            raise ValueError("REF 的动态周期必须是整数")
        return dynamic_ref(source, periods)
    if name == "SMA":
        return MYTT.SMA(
            np.asarray(arguments[0], dtype=float),
            _positive_scalar(arguments[1], "SMA"),
            _scalar(arguments[2]) if len(arguments) > 2 else 1,
        )
    if name == "EMA":
        return MYTT.EMA(np.asarray(arguments[0], dtype=float), _period(arguments[1], "EMA"))
    if name == "MA":
        return MYTT.MA(np.asarray(arguments[0], dtype=float), _period(arguments[1], "MA"))
    if name == "LLV":
        return MYTT.LLV(np.asarray(arguments[0], dtype=float), _period(arguments[1], "LLV"))
    if name == "HHV":
        return MYTT.HHV(np.asarray(arguments[0], dtype=float), _period(arguments[1], "HHV"))
    if name == "SUM":
        return MYTT.SUM(np.asarray(arguments[0], dtype=float), _period(arguments[1], "SUM"))
    if name == "COUNT":
        return MYTT.COUNT(np.asarray(arguments[0], dtype=float), _period(arguments[1], "COUNT"))
    if name == "BARSLAST":
        return MYTT.BARSLAST(np.asarray(arguments[0], dtype=bool))
    if name == "CROSS":
        return MYTT.CROSS(
            np.asarray(arguments[0], dtype=float), np.asarray(arguments[1], dtype=float)
        )
    if name == "DRAWNULL":
        return np.nan
    raise FormulaParseError(f"公式调用了不支持的函数: {name}")
