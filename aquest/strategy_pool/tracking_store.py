"""Versioned pool accounts. Only the API owns writes; workers read compressed snapshots."""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import _WRITE_LOCK

# Strategy signatures describe trading intent and remain stable across replay fixes. Snapshot
# engines version the serialized simulation semantics independently so old checkpoints rebuild.
SIGNATURE_ENGINE_VERSION = "pool-account-v1"
ENGINE_VERSION = "pool-account-v2"
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


class PoolTrackingStore:
    def __init__(self, store: DuckDbMarketDataStore):
        self.store = store

    @staticmethod
    def sync_entry(
        connection: duckdb.DuckDBPyConnection, market: str, code: str, now: str | None = None
    ) -> None:
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
                fingerprint = signature(candidate) if candidate else None
                if old and old[1] == fingerprint:
                    connection.execute(
                        "UPDATE pool_tracking_versions SET candidate_json=? WHERE version_id=?",
                        [dumps(candidate), old[0]],
                    )
                    continue
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
                        connection.execute(
                            "UPDATE pool_tracking_versions SET signature=?,candidate_json=? "
                            "WHERE version_id=?",
                            [fingerprint, dumps(candidate), old[0]],
                        )
                        continue
                if old:
                    connection.execute(
                        "UPDATE pool_tracking_versions SET is_current=FALSE, "
                        "stopped_at=? WHERE version_id=?",
                        [now, old[0]],
                    )
                if not candidate:
                    continue
                creation_day = (
                    datetime.fromisoformat(now).astimezone(ZoneInfo("Asia/Shanghai")).date()
                )
                date_row = connection.execute(
                    """SELECT MAX(trade_date) FROM daily_bars
                   WHERE market=? AND code=? AND bar_status='completed' AND trade_date<=?""",
                    [market, code, creation_day],
                ).fetchone()
                data_date = date_row[0].isoformat() if date_row and date_row[0] else None
                connection.execute(
                    """INSERT INTO pool_tracking_versions
                    (version_id,market,code,objective,source,signature,candidate_json,
                     created_at,launch_data_date,is_current,updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,TRUE,?)""",
                    [
                        uuid.uuid4().hex,
                        market,
                        code,
                        objective,
                        source,
                        fingerprint,
                        dumps(candidate),
                        now,
                        data_date,
                        now,
                    ],
                )

    def bootstrap(self) -> None:
        with _WRITE_LOCK, self.store._connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                now = datetime.now(timezone.utc).isoformat()
                for market, code in connection.execute(
                    "SELECT market,code FROM strategy_pool_entries"
                ).fetchall():
                    self.sync_entry(connection, market, code, now)
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

    def current_versions(self) -> list[dict[str, Any]]:
        return self._query("WHERE is_current ORDER BY market,code,objective,source")

    def versions(self, market: str, code: str, objective: str) -> list[dict[str, Any]]:
        return self._query(
            "WHERE market=? AND code=? AND objective=? ORDER BY created_at DESC",
            [market, code, objective],
        )

    def _query(self, suffix: str, args: list[Any] | None = None) -> list[dict[str, Any]]:
        with self.store.read_session() as connection:
            cursor = connection.execute(
                "SELECT version_id, market, code, objective, source, "
                "signature, candidate_json, created_at, launch_data_date, stopped_at, is_current, "
                "summary_json, status, last_error, updated_at FROM pool_tracking_versions "
                + suffix,
                args or [],
            )
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row, strict=True))
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
        with _WRITE_LOCK, self.store._connect() as connection:
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

    def record_error(self, version_id: str, message: str) -> None:
        with _WRITE_LOCK, self.store._connect() as connection:
            connection.execute(
                "UPDATE pool_tracking_versions SET status='failed',last_error=? "
                "WHERE version_id=? AND is_current",
                [message[:500], version_id],
            )
