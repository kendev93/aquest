"""行情数据健康检查的报告模型与组装逻辑。

所有下游结论（指标、回测、追踪）都建立在「行情没有洞」这个前提上，而这个前提
以前从没被验证过：缺 bar、长期停牌被当成价格不变、复权断点、异常价都是**静默**
的——越往下游越看不出来。这里把它显式做成一个只读报告。

口径三条，改任何一条都要同步改 ``docs/feature-backlog.md`` §4.7：

1. **交易日不是外部日历，是从数据里选出来的**。某天有 bar 的标的数达到**峰值**的
   ``CALENDAR_COVERAGE_RATIO`` 才算交易日。方向是刻意的：多算一个交易日只会多报
   一处缺口（误报，看得见），少算一个交易日则会把当天所有标的的缺口**掩盖**掉
   （漏报，看不见）。所以取峰值而不是中位数——中位数会被大范围缺失拉低，
   一旦被拉低，缺失本身就变成了"这天不算交易日"，自己把自己藏起来。
2. **缺口只在标的自己的首末日期之间数**。上市前的日期不算缺口，退市后的日期也不
   算（那条单独走 ``stale`` 检查），否则每只票都会被判成"缺了几年"。
3. **只报不修**。判断权留在人手里；修复走 ``/health/repair``，且由用户显式触发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CheckKey = Literal["gap", "long_gap", "ohlc", "price_jump", "zero_volume", "stale"]

CHECK_KEYS: tuple[CheckKey, ...] = (
    "gap",
    "long_gap",
    "ohlc",
    "price_jump",
    "zero_volume",
    "stale",
)

#: 有问题的检查项在报告里的严重级别：``gap`` / ``ohlc`` / ``price_jump`` 是数据确实
#: 错了，``long_gap`` / ``zero_volume`` / ``stale`` 可能是停牌、退市这类正常情况，
#: 所以只提示不判错。affected 为 0 时一律回落成 ``ok``。
CHECK_SEVERITY: dict[CheckKey, Literal["error", "warn"]] = {
    "gap": "error",
    "long_gap": "warn",
    "ohlc": "error",
    "price_jump": "error",
    "zero_volume": "warn",
    "stale": "warn",
}

CHECK_LABELS: dict[CheckKey, str] = {
    "gap": "行情缺口",
    "long_gap": "连续缺失段",
    "ohlc": "价格结构错误",
    "price_jump": "异常跳变",
    "zero_volume": "零成交日",
    "stale": "数据陈旧",
}

CHECK_DESCRIPTIONS: dict[CheckKey, str] = {
    "gap": "标的在自己首个与最后一个交易日之间，本该有日线却没有的交易日。"
    "缺口会让均线、RSI 这类需要连续序列的指标在缺口附近失真。",
    "long_gap": "连续缺失达到 LONG_GAP_DAYS 个交易日的整段。可能是长期停牌，"
    "也可能是整段时间根本没同步下来——前者正常，后者是数据缺失。",
    "ohlc": "最高价低于最低价、收盘价落在最高最低价之外、或价格不为正数。这类行无论来源都是错的。",
    "price_jump": "单日涨跌幅超过所在板块的涨跌停幅度（主板 10%、创业板与科创板 20%）"
    "加一个百分点的容差。除权除息在前复权数据里不该出现，出现多半是复权断点。",
    "zero_volume": "成交量为 0 的交易日。停牌日被填成零成交是常见的，"
    "但如果一只票长期零成交而价格还在动，那更像是数据问题。",
    "stale": "最后一条日线落后全市场最新交易日 STALE_TRADING_DAYS 个交易日以上。"
    "可能是退市，也可能是这只票从某次同步之后就再没被更新过。",
}

#: 能否通过「删掉重下」修复。``stale`` 里已退市的标的重下也拿不到数据，但仍然值得一试；
#: ``zero_volume`` 重下多半还是同一份数据，所以不给一键修复，只提示。
CHECK_REPAIRABLE: dict[CheckKey, bool] = {
    "gap": True,
    "long_gap": True,
    "ohlc": True,
    "price_jump": True,
    "zero_volume": False,
    "stale": True,
}

#: 各板块的涨跌停幅度。判异常跳变时统一加 ``JUMP_TOLERANCE`` 的容差，避免把四舍五入
#: 与复权尾差算成异常。
BOARD_JUMP_LIMITS: dict[str, float] = {"main": 0.10, "star": 0.20, "chinext": 0.20}

CALENDAR_COVERAGE_RATIO = 0.5
LONG_GAP_DAYS = 5
STALE_TRADING_DAYS = 5
JUMP_TOLERANCE = 0.01
DEFAULT_EXAMPLE_LIMIT = 20

MAIN_JUMP_LIMIT = BOARD_JUMP_LIMITS["main"] + JUMP_TOLERANCE
GROWTH_JUMP_LIMIT = BOARD_JUMP_LIMITS["star"] + JUMP_TOLERANCE

#: 一次修复最多动多少只标的。在线重下是串行的网络请求，几百只已经要跑好几分钟；
#: 上限存在的意义是让「全库都有问题」这种情况停下来让人看一眼，而不是闷头跑一小时。
REPAIR_MAX_CODES = 500


@dataclass(frozen=True)
class HealthExample:
    """一条可核对的证据：哪只票、哪一天、具体是什么情况。"""

    market: str
    code: str
    name: str | None
    trade_date: str | None
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "trade_date": self.trade_date,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HealthIssueRow:
    """存储层扫出来的一行原始证据，按 ``sort_value`` 降序取前若干条做示例。"""

    market: str
    code: str
    name: str | None
    trade_date: str | None
    detail: str
    occurrences: int
    sort_value: float

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class HealthCheck:
    """一类问题的汇总结果。"""

    key: CheckKey
    label: str
    description: str
    severity: str
    affected_instruments: int
    occurrences: int
    repairable: bool
    examples: tuple[HealthExample, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "severity": self.severity,
            "affected_instruments": self.affected_instruments,
            "occurrences": self.occurrences,
            "repairable": self.repairable,
            "examples": [item.to_dict() for item in self.examples],
        }


@dataclass(frozen=True)
class HealthReport:
    """一次体检的完整结果，只读、可直接序列化给前端。"""

    generated_at: str
    instrument_count: int
    bar_count: int
    calendar_days: int
    data_start: str | None
    data_end: str | None
    duration_seconds: float
    checks: tuple[HealthCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "instrument_count": self.instrument_count,
            "bar_count": self.bar_count,
            "calendar_days": self.calendar_days,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "duration_seconds": self.duration_seconds,
            "checks": [item.to_dict() for item in self.checks],
        }


def build_check(
    key: CheckKey,
    rows: tuple[HealthIssueRow, ...],
    *,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
    affected_instruments: int | None = None,
    occurrences: int | None = None,
) -> HealthCheck:
    """把存储层扫出来的行汇总成一项检查结果。

    ``affected_instruments`` 与 ``occurrences`` 要给存储层扫出来的**真值**：``rows``
    是截到示例上限的那几行（默认 20），拿它求和得到的是「前 20 行的次数」，一个数
    看着像总数、其实是截断值。存储层已经把两个总数算出来了，这里只是接住。
    """

    ordered = sorted(rows, key=lambda row: (-row.sort_value, row.symbol))
    affected = len(rows) if affected_instruments is None else affected_instruments
    total = sum(row.occurrences for row in rows) if occurrences is None else occurrences
    severity = "ok" if affected == 0 else CHECK_SEVERITY[key]
    return HealthCheck(
        key=key,
        label=CHECK_LABELS[key],
        description=CHECK_DESCRIPTIONS[key],
        severity=severity,
        affected_instruments=affected,
        occurrences=total,
        repairable=CHECK_REPAIRABLE[key],
        examples=tuple(
            HealthExample(
                market=row.market,
                code=row.code,
                name=row.name,
                trade_date=row.trade_date,
                detail=row.detail,
            )
            for row in ordered[: max(example_limit, 0)]
        ),
    )


def normalize_keys(values: object) -> tuple[CheckKey, ...]:
    """把外部传进来的检查项名字收敛成已知集合，顺序按 CHECK_KEYS 稳定输出。"""

    if values is None:
        return CHECK_KEYS
    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, (list, tuple, set, frozenset)):
        candidates = [str(item) for item in values]
    else:
        return CHECK_KEYS
    wanted = {item.strip().lower() for item in candidates}
    return tuple(key for key in CHECK_KEYS if key in wanted)
