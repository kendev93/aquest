"""Versioned pool accounts. Only the API owns writes; workers read compressed snapshots."""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import _WRITE_LOCK

# Strategy signatures describe trading intent and remain stable across replay fixes. Snapshot
# engines version the serialized simulation semantics independently so old checkpoints rebuild.
SIGNATURE_ENGINE_VERSION = "pool-account-v1"
# v3：风控退出改为「触发当日收盘判定、下一交易日开盘成交」，并新增持仓风控线与持有
# 到期的展示字段。成交时点变了会让既有跟踪账户的净值与持仓对不上，旧快照一律重建。
# v4：2016 年后上市的标的把前 268 根让给指标预热，回放起点随研究窗口后移；
# 旧快照按前移的窗口重放会多出冷指标交易，必须重建。
# v5：预热口径全市场统一——老票的 2015 段只有 244 根、凑不满 268，差额继续从 2016
# 年里让出去。回放起点再后移一次，旧快照的净值与持仓都不再可比，一律重建。
ENGINE_VERSION = "pool-account-v5"
OBJECTIVES = ("annual_return", "sharpe")
DDL = """CREATE TABLE IF NOT EXISTS pool_tracking_versions (
 version_id VARCHAR PRIMARY KEY, market VARCHAR NOT NULL, code VARCHAR NOT NULL,
 objective VARCHAR NOT NULL, source VARCHAR NOT NULL, signature VARCHAR NOT NULL,
 candidate_json VARCHAR NOT NULL, created_at VARCHAR NOT NULL, launch_data_date VARCHAR,
 stopped_at VARCHAR, is_current BOOLEAN NOT NULL, summary_json VARCHAR,
 snapshot BLOB, status VARCHAR NOT NULL DEFAULT 'pending', last_error VARCHAR NOT NULL DEFAULT '',
 updated_at VARCHAR NOT NULL
)"""


def dumps(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def signature(candidate: Mapping[str, Any]) -> str:
    return _signature(candidate, include_max_hold_days=True)


def _legacy_signature(candidate: Mapping[str, Any]) -> str:
    return _signature(candidate, include_max_hold_days=False)


def _signature(candidate: Mapping[str, Any], *, include_max_hold_days: bool) -> str:
    config = dict(candidate.get("config") or {})
    defaults: dict[str, Any] = dict(
        initial_cash=1000000,
        commission=0.0001,
        min_commission=0.1,
        stamp_tax=0.001,
        slippage=0.0,
        execution="next_open",
        position_mode="full",
        fixed_size=None,
        rebalance_frequency="daily",
        fitness_rank_weight=0.0,
        fitness_min_trades=5,
        fitness_max_drawdown=0.3,
        formula_text=None,
        formula_parameters={},
        stop_loss_pct=None,
        take_profit_pct=None,
        trailing_stop_pct=None,
        atr_period=14,
        stop_loss_mode="pct",
        take_profit_mode="pct",
        trailing_stop_mode="pct",
    )
    if include_max_hold_days and config.get("max_hold_days") is not None:
        defaults["max_hold_days"] = config["max_hold_days"]
    for label in ("stop_loss", "take_profit", "trailing_stop"):
        defaults[label + "_atr_multiplier"] = None
        defaults[label + "_cooldown_days"] = 0
    for key in defaults:
        if key in config:
            defaults[key] = config[key]
    rules = {
        side: candidate.get(side + "_rule") or config.get(side + "_rule") or {}
        for side in ("buy", "sell")
    }
    for side, rule in rules.items():
        normalized = dict(
            signals=sorted(rule.get("signals", [])),
            conditions=sorted(rule.get("conditions", []), key=dumps),
            required_conditions=sorted(rule.get("required_conditions", []), key=dumps),
            combine_mode=rule.get("combine_mode", "all"),
            minimum_matches=rule.get("minimum_matches"),
        )
        rules[side] = normalized
    names: set[str] = set()
    for rule in rules.values():
        names.update(str(value).split(".")[0] for value in rule.get("signals", []))
        for condition in [*rule.get("conditions", []), *rule.get("required_conditions", [])]:
            for key in ("left_value", "right_value"):
                if condition.get(key):
                    names.add(str(condition[key]).split(".")[0])
    versions = {}
    for name in sorted(names):
        try:
            versions[name] = FORMULA_REGISTRY.formula(name).cache_version
        except (KeyError, ValueError):
            versions[name] = "unknown"

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return value

    encoded = dumps(
        normalize(
            dict(config=defaults, rules=rules, formulas=versions, engine=SIGNATURE_ENGINE_VERSION)
        )
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def launch_day() -> date:
    """新版本落库那一刻的「上线日」（上海时区的自然日），首根日线按这天封顶。"""

    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()


def launch_data_date(store: DuckDbMarketDataStore, market: str, code: str) -> str | None:
    """该标的上线窗口第一天的最后一根已完成日线。

    必须在写事务**之外**查：`daily_bars` 现在是 4.8 GB 的表，而这条 `MAX(trade_date)`
    以前跑在 `sync_entry` 里，`sync_entry` 又一直被调在持有整库排他锁的写事务中——等于
    每建一个跟踪账户版本，就把全市场研究的只读读者冻住一次扫描（2026-10-19 一轮里
    16–25 只标的就是这么等满 25 秒预算后失败的）。
    """

    with store.read_session() as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM daily_bars "
            "WHERE market = ? AND code = ? AND bar_status = 'completed' AND trade_date <= ?",
            [market, code, launch_day()],
        ).fetchone()
    value = row[0] if row else None
    return value.isoformat() if value is not None else None


def launch_data_dates(
    store: DuckDbMarketDataStore, symbols: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], str | None]:
    """批量版：一次分组查询算出很多只标的的上线首日，口径与逐只查询一致。"""

    wanted = {(market, code) for market, code in symbols}
    if not wanted:
        return {}
    with store.read_session() as connection:
        rows = connection.execute(
            "SELECT market, code, MAX(trade_date) FROM daily_bars "
            "WHERE bar_status = 'completed' AND trade_date <= ? GROUP BY market, code",
            [launch_day()],
        ).fetchall()
    return {
        (str(market), str(code)): (value.isoformat() if value is not None else None)
        for market, code, value in rows
        if (str(market), str(code)) in wanted
    }


class PoolTrackingStore:
    def __init__(self, store: DuckDbMarketDataStore):
        self.store = store

    @staticmethod
    def _refresh_candidate_json(
        connection: duckdb.DuckDBPyConnection,
        version_id: str,
        candidate_json: str,
    ) -> None:
        connection.execute(
            "UPDATE pool_tracking_versions SET candidate_json=? WHERE version_id=?",
            [candidate_json, version_id],
        )

    @staticmethod
    def _sync_version(
        connection: duckdb.DuckDBPyConnection,
        known: MutableMapping[tuple[str, str, str, str], tuple[str, str, str]] | None,
        *,
        market: str,
        code: str,
        objective: str,
        source: str,
        candidate: Mapping[str, Any] | None,
        old: tuple[str, str, str] | None,
        now: str,
        data_date: str | None,
    ) -> None:
        """把一条（标的, 目标, 来源）的版本对齐到当前候选。

        `known` 是 bootstrap 预加载的当前版本索引；单只写入传 None，逐条查。候选的指标
        会随新数据变、但交易规则签名不变，所以签名相同时只在 JSON 真的变了才回写。
        """

        key = (market, code, objective, source)
        fingerprint = signature(candidate) if candidate else None
        if old and old[1] == fingerprint:
            candidate_json = dumps(candidate)
            if candidate_json != old[2]:
                PoolTrackingStore._refresh_candidate_json(connection, old[0], candidate_json)
                if known is not None:
                    known[key] = (old[0], old[1], candidate_json)
            return
        if old and candidate:
            old_candidate = json.loads(old[2])
            # Upgrade only the legacy signature salt/whitelist in place when both the
            # old and new candidate encode the same current trading rules. This keeps
            # created_at and launch_data_date stable; the v2 replay engine rebuilds any
            # incompatible saved checkpoint on its next run.
            if (
                old[1] == _legacy_signature(old_candidate)
                and signature(old_candidate) == fingerprint
            ):
                candidate_json = dumps(candidate)
                connection.execute(
                    "UPDATE pool_tracking_versions SET signature=?,candidate_json=? "
                    "WHERE version_id=?",
                    [fingerprint, candidate_json, old[0]],
                )
                if known is not None:
                    known[key] = (old[0], str(fingerprint), candidate_json)
                return
        if old:
            connection.execute(
                "UPDATE pool_tracking_versions SET is_current=FALSE, "
                "stopped_at=? WHERE version_id=?",
                [now, old[0]],
            )
            if known is not None:
                known.pop(key, None)
        if not candidate:
            return
        version_id = uuid.uuid4().hex
        candidate_json = dumps(candidate)
        connection.execute(
            """INSERT INTO pool_tracking_versions
            (version_id,market,code,objective,source,signature,candidate_json,
             created_at,launch_data_date,is_current,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,TRUE,?)""",
            [
                version_id,
                market,
                code,
                objective,
                source,
                fingerprint,
                candidate_json,
                now,
                data_date,
                now,
            ],
        )
        if known is not None:
            known[key] = (version_id, str(fingerprint), candidate_json)

    @staticmethod
    def sync_entry(
        connection: duckdb.DuckDBPyConnection,
        market: str,
        code: str,
        now: str | None = None,
        *,
        data_date: str | None,
    ) -> None:
        """同步一只标的的跟踪账户版本。

        `data_date`（新版本的上线首日）必须由调用方在**写事务之外**算好传进来：这条查询
        扫的是日线大表，留在写事务里就是把整库排他锁按扫描时长举着，见 `launch_data_date`。
        """

        now = now or datetime.now(timezone.utc).isoformat()
        row = connection.execute(
            """SELECT annual_candidate_json, sharpe_candidate_json,
                    manual_annual_json, manual_sharpe_json FROM strategy_pool_entries
                    WHERE market=? AND code=?""",
            [market, code],
        ).fetchone()
        if not row:
            return
        candidates = [json.loads(value) if value else None for value in row]
        for index, objective in enumerate(OBJECTIVES):
            automatic = candidates[index]
            if automatic and automatic.get("candidate_ref"):
                automatic = candidates[0 if automatic["candidate_ref"] == "annual_return" else 1]
            for source, candidate in [("automatic", automatic), ("manual", candidates[index + 2])]:
                old = connection.execute(
                    """SELECT version_id, signature, candidate_json FROM pool_tracking_versions
                    WHERE market=? AND code=? AND objective=? AND source=? AND is_current""",
                    [market, code, objective, source],
                ).fetchone()
                PoolTrackingStore._sync_version(
                    connection,
                    None,
                    market=market,
                    code=code,
                    objective=objective,
                    source=source,
                    candidate=candidate,
                    old=old,
                    now=now,
                    data_date=data_date,
                )

    def bootstrap(self) -> None:
        with self.store.read_session() as connection:
            symbols = [
                (str(market), str(code))
                for market, code in connection.execute(
                    "SELECT market, code FROM strategy_pool_entries"
                ).fetchall()
            ]
        # 上线首日先在写事务之外一次算完：逐只在写事务里扫一遍日线，等于把整库排他锁
        # 按几千次扫描的总时长举着，全市场研究的只读读者就是这么被等到超时的。
        data_dates = launch_data_dates(self.store, symbols)
        now = datetime.now(timezone.utc).isoformat()
        with _WRITE_LOCK, self.store._connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                # 当前版本与池条目各只读一次：逐只调 sync_entry 会按标的做 4 次全表查找，
                # 4,409 只启动要 8 秒；一次预加载后逐条比对，写事务里只剩有变化的行。
                current_rows = connection.execute(
                    """SELECT version_id, market, code, objective, source, signature,
                              candidate_json
                       FROM pool_tracking_versions WHERE is_current"""
                ).fetchall()
                known: dict[tuple[str, str, str, str], tuple[str, str, str]] = {}
                for (
                    version_id,
                    market,
                    code,
                    objective,
                    source,
                    fingerprint,
                    candidate_json,
                ) in current_rows:
                    known.setdefault(
                        (str(market), str(code), str(objective), str(source)),
                        (str(version_id), str(fingerprint), str(candidate_json)),
                    )
                entry_rows = connection.execute(
                    """SELECT market, code, annual_candidate_json, sharpe_candidate_json,
                              manual_annual_json, manual_sharpe_json
                       FROM strategy_pool_entries"""
                ).fetchall()
                for market, code, *raw_candidates in entry_rows:
                    candidates = [json.loads(value) if value else None for value in raw_candidates]
                    data_date = data_dates.get((str(market), str(code)))
                    for index, objective in enumerate(OBJECTIVES):
                        automatic = candidates[index]
                        if automatic and automatic.get("candidate_ref"):
                            automatic = candidates[
                                0 if automatic["candidate_ref"] == "annual_return" else 1
                            ]
                        for source, candidate in (
                            ("automatic", automatic),
                            ("manual", candidates[index + 2]),
                        ):
                            key = (str(market), str(code), objective, source)
                            PoolTrackingStore._sync_version(
                                connection,
                                known,
                                market=str(market),
                                code=str(code),
                                objective=objective,
                                source=source,
                                candidate=candidate,
                                old=known.get(key),
                                now=now,
                                data_date=data_date,
                            )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def archive_all(connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            "UPDATE pool_tracking_versions SET is_current=FALSE, stopped_at=? WHERE is_current",
            [datetime.now(timezone.utc).isoformat()],
        )

    def current_versions(self, *, with_candidate: bool = True) -> list[dict[str, Any]]:
        return self._query(
            "WHERE is_current ORDER BY market,code,objective,source", with_candidate=with_candidate
        )

    def versions(self, market: str, code: str, objective: str) -> list[dict[str, Any]]:
        return self._query(
            "WHERE market=? AND code=? AND objective=? ORDER BY created_at DESC",
            [market, code, objective],
        )

    def _query(
        self, suffix: str, args: list[Any] | None = None, *, with_candidate: bool = True
    ) -> list[dict[str, Any]]:
        # ``with_candidate=False`` 是给列表那条热路径的：candidate_json 是每次刷新里最大的一
        # 块（2026-10-19 实测 4439 行回 11.5 MB），而接口在投影时会把整份 candidate 丢掉，
        # 读出来再解析一遍纯属白做。其余调用方（跟踪 runner、详情页）确实要 candidate，保持默认。
        candidate_column = "candidate_json, " if with_candidate else ""
        with self.store.read_session() as connection:
            cursor = connection.execute(
                "SELECT version_id, market, code, objective, source, signature, "
                + candidate_column
                + "created_at, launch_data_date, stopped_at, is_current, "
                "summary_json, status, last_error, updated_at FROM pool_tracking_versions "
                + suffix,
                args or [],
            )
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row, strict=True))
            if with_candidate:
                item["candidate"] = json.loads(item.pop("candidate_json"))
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            result.append(item)
        return result

    def buy_blockades(self, objective: str) -> dict[tuple[str, str], tuple[str, str]]:
        """当前版本里「买入规则成立但注定不成交」的标的 -> (原因, 快照日期)。

        只读 ``summary_json``，不解压 checkpoint blob——冷却结论已经由回放写进摘要，
        这里拿它给不跑模拟的直连扫描路径用，避免为了一个布尔值解压整份快照。
        手动策略优先于自动策略，与 ``tracking_runner`` 的 ``applied`` 取舍一致。
        """

        with self.store.read_session() as connection:
            rows = connection.execute(
                """SELECT market, code, source, summary_json FROM pool_tracking_versions
                WHERE is_current AND objective=? AND summary_json IS NOT NULL""",
                [objective],
            ).fetchall()
        result: dict[tuple[str, str], tuple[str, str]] = {}
        for market, code, source, summary in rows:
            try:
                payload = json.loads(summary or "{}")
            except (TypeError, ValueError):
                continue
            reason = payload.get("buy_blocked_reason")
            if not reason:
                continue
            key = (str(market), str(code))
            # 手动优先，且能覆盖已经放进去的自动结果（行序不保证）。
            if source == "manual" or key not in result:
                result[key] = (str(reason), str(payload.get("as_of_date") or ""))
        return result

    def applied_summaries(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        """当前生效版本（手工优先）的 summary_json，免解压读取。

        与 :meth:`buy_blockades` 同款：只读 ``summary_json``、不解压 checkpoint
        blob，给「上线后衰减」这类只需要摘要字段的判定用。返回
        ``(market, code, objective) -> summary``。
        """

        with self.store.read_session() as connection:
            rows = connection.execute(
                """SELECT market, code, objective, source, summary_json
                FROM pool_tracking_versions
                WHERE is_current AND summary_json IS NOT NULL"""
            ).fetchall()
        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        for market, code, objective, source, summary in rows:
            try:
                payload = json.loads(summary or "{}")
            except (TypeError, ValueError):
                continue
            if not payload:
                continue
            key = (str(market), str(code), str(objective))
            # 手动优先，且能覆盖已经放进去的自动结果（行序不保证）。
            if source == "manual" or key not in result:
                result[key] = payload
        return result

    def snapshot(self, version_id: str) -> dict[str, Any] | None:
        with self.store.read_session() as connection:
            row = connection.execute(
                "SELECT snapshot FROM pool_tracking_versions WHERE version_id=?", [version_id]
            ).fetchone()
        return json.loads(zlib.decompress(bytes(row[0]))) if row and row[0] else None

    def save_snapshot(self, version_id: str, fingerprint: str, snapshot: Mapping[str, Any]) -> bool:
        # 校验与写入要在同一个事务里：分成两条独立语句时，中间被别的写者插进来就可能
        # 把「已经不是当前版本」的快照写回去。口径与 save_snapshots 的批量版一致。
        with _WRITE_LOCK, self.store.write_transaction() as connection:
            row = connection.execute(
                "SELECT signature,is_current,summary_json FROM pool_tracking_versions "
                "WHERE version_id=?",
                [version_id],
            ).fetchone()
            if not row or row[0] != fingerprint or not row[1]:
                return False
            summary = dict(snapshot["summary"])
            old_summary = json.loads(row[2] or "{}")
            if str(old_summary.get("as_of_date") or "") > str(summary.get("as_of_date") or ""):
                return False
            if not isinstance(snapshot.get("checkpoint"), Mapping):
                raise ValueError("跟踪快照缺少账户检查点")
            connection.execute(
                """UPDATE pool_tracking_versions SET snapshot=?, summary_json=?,
                status='ready',last_error='',updated_at=? WHERE version_id=? AND is_current""",
                [
                    zlib.compress(dumps(snapshot).encode()),
                    dumps(summary),
                    datetime.now(timezone.utc).isoformat(),
                    version_id,
                ],
            )
        return True

    def save_snapshots(self, items: Sequence[tuple[str, str, Mapping[str, Any]]]) -> list[bool]:
        """把一批跟踪快照写进一个事务，按传入顺序返回每一只是否真的保存了。

        口径与 :meth:`save_snapshot` 逐条一致（签名对得上、仍是当前生效、日期不许倒退、
        必须有检查点），只是把逐只的 SELECT + UPDATE 合成一次批量 UPDATE：实测单只 15.8 ms，
        一次全市场扫描 4,409 只就是约 70 秒的串行写库，摊成批量后是秒级。快照是派生数据，
        进程中途退出最多丢一个未提交的批，下一轮扫描会重算。
        """

        if not items:
            return []
        for _, _, snapshot in items:
            if not isinstance(snapshot.get("checkpoint"), Mapping):
                raise ValueError("跟踪快照缺少账户检查点")
        version_ids = list(dict.fromkeys(version_id for version_id, _, _ in items))
        placeholders = ",".join("?" for _ in version_ids)
        updated_at = datetime.now(timezone.utc).isoformat()
        saved: list[bool] = []
        with _WRITE_LOCK, self.store.write_transaction() as connection:
            rows = connection.execute(
                "SELECT version_id, signature, is_current, summary_json "
                f"FROM pool_tracking_versions WHERE version_id IN ({placeholders})",
                version_ids,
            ).fetchall()
            known = {str(row[0]): row for row in rows}
            # 同一批里同一个版本可能出现多条：按传入顺序判定（与逐只写一致），但只把
            # 最后一条真正发出去——多行 VALUES 里重复键的行为没有保证。
            accepted: dict[str, list[Any]] = {}
            effective_date: dict[str, str] = {}
            for version_id, fingerprint, snapshot in items:
                row = known.get(version_id)
                if not row or row[1] != fingerprint or not row[2]:
                    saved.append(False)
                    continue
                summary = dict(snapshot["summary"])
                previous = json.loads(row[3] or "{}")
                as_of = str(summary.get("as_of_date") or "")
                floor = max(
                    str(previous.get("as_of_date") or ""), effective_date.get(version_id, "")
                )
                if floor > as_of:
                    saved.append(False)
                    continue
                effective_date[version_id] = as_of
                accepted[version_id] = [
                    version_id,
                    zlib.compress(dumps(snapshot).encode()),
                    dumps(summary),
                    updated_at,
                ]
                saved.append(True)
            if accepted:
                count = len(accepted)
                params = [value for row in accepted.values() for value in row]
                values = ",".join("(?,?,?,?)" for _ in range(count))
                connection.execute(
                    "UPDATE pool_tracking_versions AS current "
                    "SET snapshot = batch.snapshot, summary_json = batch.summary_json, "
                    "status = 'ready', last_error = '', updated_at = batch.updated_at "
                    f"FROM (VALUES {values}) "
                    "AS batch(version_id, snapshot, summary_json, updated_at) "
                    "WHERE current.version_id = batch.version_id AND current.is_current",
                    params,
                )
        return saved

    def record_error(self, version_id: str, message: str) -> None:
        with _WRITE_LOCK, self.store._connect() as connection:
            connection.execute(
                "UPDATE pool_tracking_versions SET status='failed',last_error=? "
                "WHERE version_id=? AND is_current",
                [message[:500], version_id],
            )
