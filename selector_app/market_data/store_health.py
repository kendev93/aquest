"""Read-only health scan over the stored daily bars.

这里是「行情有没有洞」的唯一判定处。所有判定都只读，不做任何修复——修复动作
（删掉重下）由 ``/market-data/health/repair`` 显式触发，因为判断权要留在人手里。

两处容易被写错的口径，改前先看 ``health.py`` 的模块注释：

- 交易日是从数据里选出来的（覆盖中位数的一个比例），不是外部日历；
- 缺口只在标的自己的首末日期之间数，上市前与退市后都不算缺口。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from .health import (
    CALENDAR_COVERAGE_RATIO,
    GROWTH_JUMP_LIMIT,
    LONG_GAP_DAYS,
    MAIN_JUMP_LIMIT,
    STALE_TRADING_DAYS,
    CheckKey,
    HealthIssueRow,
    HealthReport,
    build_check,
)
from .store_base import _DuckDbStoreBase

#: 同一 (market, code, trade_date) 可能有多行（不同 price_basis、本地行压过在线行），
#: 所有检查都只看最新的一行，否则同一天会被重复计数、前收盘价也会错位。
_COMMON_CTE = """
WITH ranked AS (
    SELECT market, code, trade_date, open, high, low, close, volume,
           ROW_NUMBER() OVER (
               PARTITION BY market, code, trade_date
               ORDER BY imported_at DESC, source DESC
           ) AS dedup_rank
    FROM daily_bars
),
bars_one AS (
    SELECT market, code, trade_date, open, high, low, close, volume
    FROM ranked
    WHERE dedup_rank = 1
),
per_date AS (
    SELECT trade_date, COUNT(DISTINCT code) AS instrument_count
    FROM bars_one
    GROUP BY trade_date
),
coverage AS (
    SELECT MAX(instrument_count) AS peak_count FROM per_date
),
calendar AS (
    SELECT per_date.trade_date,
           ROW_NUMBER() OVER (ORDER BY per_date.trade_date) AS day_index
    FROM per_date, coverage
    WHERE per_date.instrument_count >= coverage.peak_count * ?
),
spans AS (
    SELECT market, code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
    FROM bars_one
    GROUP BY market, code
),
expected AS (
    SELECT spans.market, spans.code, calendar.trade_date, calendar.day_index
    FROM spans
    JOIN calendar
      ON calendar.trade_date >= spans.first_date
     AND calendar.trade_date <= spans.last_date
),
missing AS (
    SELECT expected.market, expected.code, expected.trade_date, expected.day_index
    FROM expected
    ANTI JOIN bars_one
      ON bars_one.market = expected.market
     AND bars_one.code = expected.code
     AND bars_one.trade_date = expected.trade_date
),
runs AS (
    SELECT market, code, trade_date,
           day_index - ROW_NUMBER() OVER (
               PARTITION BY market, code ORDER BY day_index
           ) AS run_id
    FROM missing
),
segments AS (
    SELECT market, code, run_id,
           MIN(trade_date) AS seg_start,
           MAX(trade_date) AS seg_end,
           COUNT(*) AS seg_days
    FROM runs
    GROUP BY market, code, run_id
)
"""

_OVERVIEW_SQL = (
    _COMMON_CTE
    + """
SELECT
    (SELECT COUNT(*) FROM instruments) AS instrument_count,
    (SELECT COUNT(*) FROM daily_bars) AS bar_count,
    (SELECT COUNT(*) FROM calendar) AS calendar_days,
    (SELECT MIN(trade_date) FROM bars_one) AS data_start,
    (SELECT MAX(trade_date) FROM bars_one) AS data_end
"""
)

_GAP_SUMMARY_SQL = (
    _COMMON_CTE
    + """
SELECT COUNT(DISTINCT code) AS affected, COALESCE(SUM(seg_days), 0) AS occurrences
FROM segments
"""
)

_GAP_EXAMPLES_SQL = (
    _COMMON_CTE
    + """
SELECT segments.market, segments.code, instruments.name,
       MIN(segments.seg_start) AS first_missing,
       MAX(segments.seg_end) AS last_missing,
       SUM(segments.seg_days) AS missing_days,
       COUNT(*) AS segment_count
FROM segments
LEFT JOIN instruments
  ON instruments.market = segments.market AND instruments.code = segments.code
GROUP BY segments.market, segments.code, instruments.name
ORDER BY missing_days DESC, segments.market, segments.code
LIMIT ?
"""
)

_LONG_GAP_SUMMARY_SQL = (
    _COMMON_CTE
    + """
SELECT COUNT(DISTINCT code) AS affected, COUNT(*) AS occurrences
FROM segments
WHERE seg_days >= ?
"""
)

_LONG_GAP_EXAMPLES_SQL = (
    _COMMON_CTE
    + """
SELECT segments.market, segments.code, instruments.name,
       segments.seg_start, segments.seg_end, segments.seg_days
FROM segments
LEFT JOIN instruments
  ON instruments.market = segments.market AND instruments.code = segments.code
WHERE segments.seg_days >= ?
ORDER BY segments.seg_days DESC, segments.market, segments.code
LIMIT ?
"""
)

_BAR_JOIN_CTE = """
WITH ranked AS (
    SELECT market, code, trade_date, open, high, low, close, volume,
           ROW_NUMBER() OVER (
               PARTITION BY market, code, trade_date
               ORDER BY imported_at DESC, source DESC
           ) AS dedup_rank
    FROM daily_bars
),
bars_one AS (
    SELECT market, code, trade_date, open, high, low, close, volume
    FROM ranked
    WHERE dedup_rank = 1
),
joined AS (
    SELECT bars_one.market, bars_one.code, bars_one.trade_date,
           bars_one.open, bars_one.high, bars_one.low, bars_one.close, bars_one.volume,
           instruments.name, instruments.board,
           LAG(bars_one.close) OVER (
               PARTITION BY bars_one.market, bars_one.code ORDER BY bars_one.trade_date
           ) AS prev_close
    FROM bars_one
    LEFT JOIN instruments
      ON instruments.market = bars_one.market AND instruments.code = bars_one.code
)
"""

_OHLC_WHERE = """
WHERE joined.high < joined.low
   OR joined.close < joined.low OR joined.close > joined.high
   OR joined.open < joined.low OR joined.open > joined.high
   OR joined.close <= 0 OR joined.high <= 0 OR joined.low <= 0 OR joined.open <= 0
   OR joined.volume < 0
"""

_OHLC_SUMMARY_SQL = (
    _BAR_JOIN_CTE
    + f"SELECT COUNT(DISTINCT code) AS affected, COUNT(*) AS occurrences FROM joined {_OHLC_WHERE}"
)

_OHLC_EXAMPLES_SQL = (
    _BAR_JOIN_CTE
    + f"""
SELECT market, code, name, trade_date, open, high, low, close, volume
FROM joined
{_OHLC_WHERE}
ORDER BY trade_date DESC, market, code
LIMIT ?
"""
)

_JUMP_LIMIT_EXPR = """
(CASE WHEN joined.board IN ('star', 'chinext') THEN ? ELSE ? END)
"""

_JUMP_WHERE = f"""
WHERE joined.prev_close > 0
  AND ABS(joined.close / joined.prev_close - 1.0) > {_JUMP_LIMIT_EXPR}
"""

_JUMP_SUMMARY_SQL = (
    _BAR_JOIN_CTE
    + f"SELECT COUNT(DISTINCT code) AS affected, COUNT(*) AS occurrences FROM joined {_JUMP_WHERE}"
)

_JUMP_EXAMPLES_SQL = (
    _BAR_JOIN_CTE
    + f"""
SELECT market, code, name, trade_date, close, prev_close, board
FROM joined
{_JUMP_WHERE}
ORDER BY ABS(close / prev_close - 1.0) DESC, market, code
LIMIT ?
"""
)

_ZERO_VOLUME_WHERE = "WHERE joined.volume <= 0"

_ZERO_VOLUME_SUMMARY_SQL = (
    _BAR_JOIN_CTE
    + f"""
SELECT COUNT(DISTINCT code) AS affected, COUNT(*) AS occurrences
FROM joined
{_ZERO_VOLUME_WHERE}
"""
)

_ZERO_VOLUME_EXAMPLES_SQL = (
    _BAR_JOIN_CTE
    + f"""
SELECT market, code, name, trade_date, volume, COUNT(*) OVER (
    PARTITION BY market, code
) AS days_for_code
FROM joined
{_ZERO_VOLUME_WHERE}
ORDER BY days_for_code DESC, trade_date DESC, market, code
LIMIT ?
"""
)

_STALE_SUMMARY_SQL = (
    _COMMON_CTE
    + """
SELECT COUNT(DISTINCT spans.code) AS affected, COUNT(DISTINCT spans.code) AS occurrences
FROM spans
JOIN calendar ON calendar.trade_date = spans.last_date
CROSS JOIN (SELECT MAX(day_index) AS max_index FROM calendar) AS latest
WHERE latest.max_index - calendar.day_index >= ?
"""
)

_STALE_EXAMPLES_SQL = (
    _COMMON_CTE
    + """
SELECT spans.market, spans.code, instruments.name, spans.last_date,
       latest.max_index - calendar.day_index AS behind_days
FROM spans
JOIN calendar ON calendar.trade_date = spans.last_date
LEFT JOIN instruments
  ON instruments.market = spans.market AND instruments.code = spans.code
CROSS JOIN (SELECT MAX(day_index) AS max_index FROM calendar) AS latest
WHERE latest.max_index - calendar.day_index >= ?
ORDER BY behind_days DESC, spans.market, spans.code
LIMIT ?
"""
)

_CODES_WITHOUT_BARS_SQL = """
WITH wanted AS (SELECT UNNEST(?) AS code)
SELECT wanted.code
FROM wanted
ANTI JOIN (SELECT DISTINCT code FROM daily_bars) AS stored
  ON stored.code = wanted.code
ORDER BY wanted.code
"""


def _as_date(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _as_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class DuckDbHealthStore:
    """Read-only repository for the market-data health report."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def overview(self) -> dict[str, object]:
        """Total counts plus the trading calendar derived from the stored bars."""

        with self._store._connect() as connection:
            row = connection.execute(_OVERVIEW_SQL, [CALENDAR_COVERAGE_RATIO]).fetchone()
        if row is None:
            return {}
        keys = ("instrument_count", "bar_count", "calendar_days", "data_start", "data_end")
        values = dict(zip(keys, row, strict=False))
        return {
            "instrument_count": _as_int(values["instrument_count"]),
            "bar_count": _as_int(values["bar_count"]),
            "calendar_days": _as_int(values["calendar_days"]),
            "data_start": _as_date(values["data_start"]),
            "data_end": _as_date(values["data_end"]),
        }

    def gap(self, *, example_limit: int) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        """Missing trading days inside each instrument's own first/last range."""

        summary = self._summary(_GAP_SUMMARY_SQL, [CALENDAR_COVERAGE_RATIO])
        with self._store._connect() as connection:
            rows = connection.execute(
                _GAP_EXAMPLES_SQL, [CALENDAR_COVERAGE_RATIO, example_limit]
            ).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=(
                    f"共缺 {_as_int(row[5])} 个交易日，分 {_as_int(row[6])} 段"
                    f"（最早缺失 {_as_date(row[3])}）"
                ),
                occurrences=_as_int(row[5]),
                sort_value=float(_as_int(row[5])),
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def long_gap(
        self, *, min_days: int, example_limit: int
    ) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        params = [CALENDAR_COVERAGE_RATIO, min_days]
        summary = self._summary(_LONG_GAP_SUMMARY_SQL, params)
        with self._store._connect() as connection:
            rows = connection.execute(_LONG_GAP_EXAMPLES_SQL, [*params, example_limit]).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=f"{_as_date(row[3])} ~ {_as_date(row[4])} 连续缺 {_as_int(row[5])} 个交易日",
                occurrences=1,
                sort_value=float(_as_int(row[5])),
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def ohlc(self, *, example_limit: int) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        summary = self._summary(_OHLC_SUMMARY_SQL, [])
        with self._store._connect() as connection:
            rows = connection.execute(_OHLC_EXAMPLES_SQL, [example_limit]).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=(
                    f"开 {_as_float(row[4]):.2f} / 高 {_as_float(row[5]):.2f} / "
                    f"低 {_as_float(row[6]):.2f} / 收 {_as_float(row[7]):.2f}"
                ),
                occurrences=1,
                sort_value=1.0,
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def price_jump(
        self, *, main_limit: float, growth_limit: float, example_limit: int
    ) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        params = [main_limit, growth_limit]
        summary = self._summary(_JUMP_SUMMARY_SQL, params)
        with self._store._connect() as connection:
            rows = connection.execute(_JUMP_EXAMPLES_SQL, [*params, example_limit]).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=(
                    f"前收 {_as_float(row[5]):.2f} → 收盘 {_as_float(row[4]):.2f}"
                    f"（{(_as_float(row[4]) / _as_float(row[5]) - 1.0) * 100:.1f}%）"
                ),
                occurrences=1,
                sort_value=abs(_as_float(row[4]) / _as_float(row[5]) - 1.0)
                if _as_float(row[5])
                else 0.0,
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def _instrument_names(
        self, symbols: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        """按 ``(market, code)`` 批量取证券简称，供示例展示用。

        只查这批候选而不是全表：候选通常是几十到几百个，而 instruments 有几千行，
        而且每次体检都全量拉一遍名字是纯粹的浪费。
        """

        if not symbols:
            return {}
        placeholders = ", ".join("(?, ?)" for _ in symbols)
        params: list[object] = []
        for market, code in symbols:
            params.extend((market, code))
        with self._store.read_session() as connection:
            rows = connection.execute(
                f"""
                SELECT market, code, name FROM instruments
                WHERE (market, code) IN ({placeholders})
                """,
                params,
            ).fetchall()
        return {(str(row[0]), str(row[1])): _as_text(row[2]) for row in rows}

    def missing_action(
        self, *, example_limit: int
    ) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        """超跌停、但事件表给不出解释的跳空 —— 除权漏记的候选。

        判据复用 :meth:`read_price_gap_candidates`，这里既不重判也不重写 SQL：那才是
        检测入口，再写一份就会漂移，漂移的下场是**体检报「无」而补全能扫出几百个**，
        比完全不体检更糟 —— 体检的用处是让人相信它说「安全」的时候真安全。

        排序刻意**按日期倒序而不是按跌幅**：这一项的用途是让人一眼看出「最近有没有
        新增」。按跌幅排会把最新的一条压在几百条历史候选底下，那就失去了它存在的
        意义 —— 历史候选本就是常态，新的那条才是要管的。
        """

        reader = getattr(self._store, "read_price_gap_candidates", None)
        if not callable(reader):
            # 没有检测入口的存储实现（例如只装配了基础层的替身）按「没有」处理，
            # 而不是在这一项上永久报错。
            return 0, 0, ()
        frame = reader()
        if frame.empty:
            return 0, 0, ()
        # ``_frame_from_query`` 会把日期列转成 pandas Timestamp，而 Timestamp 是
        # datetime 的子类 —— ``isinstance(x, date)`` 为真，于是 ``date.isoformat()``
        # 会把报告里的每个日期写成 2024-01-21T00:00:00。先统一落成 ``datetime.date``。
        frame = frame.assign(
            date=[item.date() if isinstance(item, datetime) else item for item in frame["date"]]
        )
        candidates = list(frame.itertuples(index=False))
        # 名字要一次批量取完。放在行循环里会变成「每行一条 SQL」，候选有几百条时
        # 就是几百次往返，而它们明明可以在一次查询里全部拿到。
        symbols = sorted({(str(row.market), str(row.code)) for row in candidates})
        names = self._instrument_names(symbols)
        rows = tuple(
            HealthIssueRow(
                market=str(row.market),
                code=str(row.code),
                name=names.get((str(row.market), str(row.code))),
                # 列名是 ``date`` 而不是 ``trade_date``：查询出来的帧统一被
                # ``_frame_from_query`` 重命名过（同时把日期转成 datetime）。
                # 这一条必须跟着 ``gap_backfill`` 的写法走，不能照 SQL 里的别名硬猜。
                trade_date=_as_date(row.date),
                detail=(
                    f"前收 {_as_float(row.prev_close):.2f} → 收盘 {_as_float(row.close):.2f}"
                    f"（跌 {(_as_float(row.close) / _as_float(row.prev_close) - 1.0) * 100:.1f}%，"
                    f"事件表当日无记录）"
                ),
                occurrences=1,
                sort_value=float(row.date.toordinal())
                if isinstance(row.date, date)
                else 0.0,
            )
            for row in candidates
        )
        affected = len({(row.market, row.code) for row in rows})
        # 检出后还要再排一次：真正的「全部 row」要用于上面的 affected / occurrences
        # 计数，示例交给下游按同样顺序截断，两处口径才一致。
        ordered = sorted(rows, key=lambda item: (-item.sort_value, item.symbol))
        return affected, len(rows), tuple(ordered[: max(example_limit, 0)])

    def zero_volume(self, *, example_limit: int) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        summary = self._summary(_ZERO_VOLUME_SUMMARY_SQL, [])
        with self._store._connect() as connection:
            rows = connection.execute(_ZERO_VOLUME_EXAMPLES_SQL, [example_limit]).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=f"成交量 {_as_float(row[4]):.0f}，该标的共 {_as_int(row[5])} 个零成交日",
                occurrences=1,
                sort_value=float(_as_int(row[5])),
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def stale(
        self, *, min_days: int, example_limit: int
    ) -> tuple[int, int, tuple[HealthIssueRow, ...]]:
        params = [CALENDAR_COVERAGE_RATIO, min_days]
        summary = self._summary(_STALE_SUMMARY_SQL, params)
        with self._store._connect() as connection:
            rows = connection.execute(_STALE_EXAMPLES_SQL, [*params, example_limit]).fetchall()
        issues = tuple(
            HealthIssueRow(
                market=str(row[0]),
                code=str(row[1]),
                name=_as_text(row[2]),
                trade_date=_as_date(row[3]),
                detail=f"最后一条日线 {_as_date(row[3])}，落后全市场 {_as_int(row[4])} 个交易日",
                occurrences=1,
                sort_value=float(_as_int(row[4])),
            )
            for row in rows
        )
        return summary[0], summary[1], issues

    def codes_for(self, keys: tuple[CheckKey, ...]) -> tuple[tuple[str, str], ...]:
        """Collect every instrument hit by the given checks, for the repair job.

        与报告分开扫一遍而不是从示例里取：报告只带前 20 条示例，修复要的是完整
        名单，否则「一键修复」会静默漏掉大部分有问题的标的。
        """

        queries: list[tuple[str, list[object]]] = []
        if "gap" in keys:
            queries.append(
                (
                    _COMMON_CTE
                    + """
                    SELECT DISTINCT market, code FROM segments ORDER BY market, code
                    """,
                    [CALENDAR_COVERAGE_RATIO],
                )
            )
        if "long_gap" in keys:
            queries.append(
                (
                    _COMMON_CTE
                    + """
                    SELECT DISTINCT market, code FROM segments
                    WHERE seg_days >= ?
                    ORDER BY market, code
                    """,
                    [CALENDAR_COVERAGE_RATIO, LONG_GAP_DAYS],
                )
            )
        if "ohlc" in keys:
            queries.append(
                (
                    _BAR_JOIN_CTE
                    + f"""
                    SELECT DISTINCT market, code FROM joined {_OHLC_WHERE}
                    ORDER BY market, code
                    """,
                    [],
                )
            )
        if "price_jump" in keys:
            queries.append(
                (
                    _BAR_JOIN_CTE
                    + f"""
                    SELECT DISTINCT market, code FROM joined {_JUMP_WHERE}
                    ORDER BY market, code
                    """,
                    [MAIN_JUMP_LIMIT, GROWTH_JUMP_LIMIT],
                )
            )
        if "zero_volume" in keys:
            queries.append(
                (
                    _BAR_JOIN_CTE
                    + f"""
                    SELECT DISTINCT market, code FROM joined {_ZERO_VOLUME_WHERE}
                    ORDER BY market, code
                    """,
                    [],
                )
            )
        if "stale" in keys:
            queries.append(
                (
                    _COMMON_CTE
                    + """
                    SELECT DISTINCT spans.market, spans.code
                    FROM spans
                    JOIN calendar ON calendar.trade_date = spans.last_date
                    CROSS JOIN (SELECT MAX(day_index) AS max_index FROM calendar) AS latest
                    WHERE latest.max_index - calendar.day_index >= ?
                    """,
                    [CALENDAR_COVERAGE_RATIO, STALE_TRADING_DAYS],
                )
            )
        collected: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        with self._store._connect() as connection:
            for sql, params in queries:
                for market, code in connection.execute(sql, params).fetchall():
                    key = (str(market), str(code))
                    if key not in seen:
                        seen.add(key)
                        collected.append(key)
        return tuple(collected)

    def codes_without_bars(self, codes: Sequence[str]) -> tuple[str, ...]:
        """Which of ``codes`` have no bars left at all.

        修复是先删后下的，在线源已经没有的标的（退市）删了就补不回来。这个查询把
        这批标的单独挑出来报给用户，而不是让它悄悄变成空数据。
        """

        if not codes:
            return ()
        with self._store._connect() as connection:
            rows = connection.execute(_CODES_WITHOUT_BARS_SQL, [list(codes)]).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _summary(self, sql: str, params: Sequence[object]) -> tuple[int, int]:
        with self._store._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        if row is None:
            return 0, 0
        return _as_int(row[0]), _as_int(row[1])


def build_report(
    store: DuckDbHealthStore,
    *,
    generated_at: str,
    duration_seconds: float,
    example_limit: int,
) -> HealthReport:
    """Run every check and assemble the report."""

    overview = store.overview()
    gap_affected, gap_occurrences, gap_rows = store.gap(example_limit=example_limit)
    long_gap_affected, long_gap_occurrences, long_gap_rows = store.long_gap(
        min_days=LONG_GAP_DAYS, example_limit=example_limit
    )
    ohlc_affected, ohlc_occurrences, ohlc_rows = store.ohlc(example_limit=example_limit)
    jump_affected, jump_occurrences, jump_rows = store.price_jump(
        main_limit=MAIN_JUMP_LIMIT, growth_limit=GROWTH_JUMP_LIMIT, example_limit=example_limit
    )
    action_affected, action_occurrences, action_rows = store.missing_action(
        example_limit=example_limit
    )
    zero_affected, zero_occurrences, zero_rows = store.zero_volume(example_limit=example_limit)
    stale_affected, stale_occurrences, stale_rows = store.stale(
        min_days=STALE_TRADING_DAYS, example_limit=example_limit
    )
    return HealthReport(
        generated_at=generated_at,
        instrument_count=_as_int(overview.get("instrument_count")),
        bar_count=_as_int(overview.get("bar_count")),
        calendar_days=_as_int(overview.get("calendar_days")),
        data_start=_as_date(overview.get("data_start")),
        data_end=_as_date(overview.get("data_end")),
        duration_seconds=duration_seconds,
        checks=(
            build_check(
                "gap",
                gap_rows,
                example_limit=example_limit,
                affected_instruments=gap_affected,
                occurrences=gap_occurrences,
            ),
            build_check(
                "long_gap",
                long_gap_rows,
                example_limit=example_limit,
                affected_instruments=long_gap_affected,
                occurrences=long_gap_occurrences,
            ),
            build_check(
                "ohlc",
                ohlc_rows,
                example_limit=example_limit,
                affected_instruments=ohlc_affected,
                occurrences=ohlc_occurrences,
            ),
            build_check(
                "price_jump",
                jump_rows,
                example_limit=example_limit,
                affected_instruments=jump_affected,
                occurrences=jump_occurrences,
            ),
            build_check(
                "zero_volume",
                zero_rows,
                example_limit=example_limit,
                affected_instruments=zero_affected,
                occurrences=zero_occurrences,
            ),
            build_check(
                "stale",
                stale_rows,
                example_limit=example_limit,
                affected_instruments=stale_affected,
                occurrences=stale_occurrences,
            ),
            build_check(
                "missing_action",
                action_rows,
                example_limit=example_limit,
                affected_instruments=action_affected,
                occurrences=action_occurrences,
            ),
        ),
    )
