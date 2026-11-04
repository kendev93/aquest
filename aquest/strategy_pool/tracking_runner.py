"""Independent pool account refresh channel sharing the research process budget."""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import time
import zlib
from collections.abc import Callable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from contextlib import ExitStack
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from aquest.concurrency import visible_cpu_count
from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.jobs import TaskCancelledError, TaskUserError
from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
from aquest.strategy_discovery.worker_api_client import TransientWorkerApiError

from .quality import (
    benchmark_full_return,
    fresh_quality_inputs,
    research_end_from_entry,
    strategy_is_weak,
    weak_strategy_reasons,
)
from .resources import (
    PoolConcurrencyRamp,
    cpu_bounded_worker_count,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    memory_status,
    pool_worker_cap,
)
from .tracking import account_summary, replay_account
from .tracking_store import ENGINE_VERSION, OBJECTIVES, PoolTrackingStore, encode_snapshot

_WORKER_SERVICE: PortfolioBacktestService | None = None
logger = logging.getLogger(__name__)

#: 一次批量上报多少只跟踪快照。64 只 × 4 个参数远低于 SQLite 的 999 变量上限，
#: 单批快照体量约十几 MB；再大只会把一次请求的超时和内存风险放大。
SNAPSHOT_BATCH_SIZE = 64
#: 一个回放任务领多少只：整批只开一次状态库连接读检查点（全量扫描实测逐只打开
#: 0.67 秒/只），单只慢票对整轮的拖尾也只有一批的量。
REPLAY_CHUNK_SIZE = 12
#: 一次预取多少只的上一份检查点压缩块（块已压缩，一只约 0.3 MB）。
SNAPSHOT_PREFETCH = 64
#: 跟踪扫描的并发起步与爬坡：从 4 个起，每 2 秒 +1；实际并发仍受内存与 CPU 压力上限约束。
TRACKING_RAMP_INITIAL_WORKERS = 4
TRACKING_RAMP_INTERVAL_SECONDS = 2.0
#: worker 里按阶段计的耗时键，全部是「各进程之和」，用来看比例，不能和墙钟相加。
#: ``bars_s``…``finalize_s`` 这 6 项在 tracking.replay_account 里记，``weak_s``/``encode_s``
#: 是它返回之后的收尾，``decode_s`` 是回放进程解压上一份检查点（在计算计时之前）。
REPLAY_PHASE_KEYS = (
    "decode_s",
    "bars_s",
    "context_s",
    "prefix_s",
    "simulate_s",
    "summary_s",
    "finalize_s",
    "weak_s",
    "encode_s",
)
#: 日志里每个阶段键对应的中文说明（写的是这段时间**覆盖什么**，不是键名）。
REPLAY_PHASE_LABELS = {
    "decode_s": "检查点解压",
    "bars_s": "行情读取",
    "context_s": "建上下文",
    "prefix_s": "前缀校验",
    "simulate_s": "账户回放",
    "summary_s": "摘要与报告",
    "finalize_s": "存档哈希",
    "weak_s": "弱判定与版本摘要",
    "encode_s": "快照编码",
}


def _initialize(database_path: str) -> None:
    global _WORKER_SERVICE
    store = DuckDbMarketDataStore(database_path, read_only=True)
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    cache_store = store if store.indicator_cache_backend == "sqlite" else None
    _WORKER_SERVICE = PortfolioBacktestService(
        adapter=adapter, indicator_cache=IndicatorCache(cache_store, max_memory_entries=64)
    )


def _final_summary(version: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """回放后的最终摘要：账户字段、引擎版本、数据修订与弱策略起点一次算完。

    这一步原来在主进程里逐只做（全量扫描的「排队/IPC」一大半就是它），现在跟回放
    一起在 worker 里并行。
    """

    boundary = (
        version.get("launch_data_date")
        or datetime.fromisoformat(version["created_at"])
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    summary = {
        **snapshot["summary"],
        **account_summary(
            snapshot["checkpoint"],
            boundary,
            float(version["candidate"].get("config", {}).get("initial_cash", 1000000)),
            (snapshot.get("report") or {}).get("current_state"),
        ),
    }
    summary["engine_version"] = ENGINE_VERSION
    summary["data_revision"] = version.get("data_revision")
    previous_summary = version.get("summary")
    previous_weak_since = (
        previous_summary.get("weak_since") if isinstance(previous_summary, Mapping) else None
    )
    weak_metrics, weak_benchmark = fresh_quality_inputs(
        summary,
        version["candidate"] if isinstance(version["candidate"], Mapping) else None,
        fallback_benchmark=version.get("benchmark_return"),
        research_end=version.get("research_end"),
    )
    if strategy_is_weak(weak_metrics, weak_benchmark):
        weak_since = previous_weak_since or str(summary.get("as_of_date") or "")
        summary["weak_since"] = weak_since
        # 连续判弱按交易日数，而不是自然日差：周五判弱、下周三只有 3 个交易日，
        # 自然日却已经过了 5 天。净值行本身就是账户的交易日序列，直接数行。
        summary["weak_trading_days"] = _weak_trading_days(snapshot["checkpoint"], weak_since)
    else:
        summary["weak_since"] = None
        summary["weak_trading_days"] = None
    snapshot["summary"] = summary


def _weak_trading_days(
    checkpoint: Mapping[str, Any],
    weak_since: str | None,
) -> int | None:
    """从第一次判弱那天起，账户净值已经走过多少个交易日（含当天）。"""

    if not isinstance(weak_since, str) or not weak_since:
        return None
    equity = checkpoint.get("equity")
    if not isinstance(equity, list):
        return None
    count = sum(
        1 for row in equity if isinstance(row, Mapping) and str(row.get("date", "")) >= weak_since
    )
    return count or None


def _replay_one(
    version: dict[str, Any], previous_blob: bytes | None
) -> tuple[str, dict[str, Any], float, dict[str, float]]:
    """回放一只并压好快照：返回（压缩块, 最终摘要, 计算耗时, 阶段耗时）。

    上一份检查点由主进程整批读好、以压缩块传进来（回放进程自己开状态库会和 backend
    的写事务抢跨进程锁），这里只负责解压解析；解析失败就整段重放，不能判失败。
    解压在计时起点之前，所以单独计一份 ``decode_s``，否则这段开销谁都不认。
    """

    if _WORKER_SERVICE is None:
        raise RuntimeError("跟踪回放进程未初始化")
    adapter = _WORKER_SERVICE._adapter
    clear = getattr(adapter, "clear_thread_stock_cache", None)
    if callable(clear):
        clear()
    ref = adapter.stock_ref("", version["market"], version["code"])
    previous: dict[str, Any] | None = None
    decode_s = 0.0
    if previous_blob is not None:
        decode_started = time.monotonic()
        try:
            previous = json.loads(zlib.decompress(previous_blob))
        except (TypeError, ValueError, zlib.error) as exc:
            logger.warning("上一份跟踪检查点无法解析，本轮整段重放 %s: %s", ref.symbol, exc)
        decode_s = time.monotonic() - decode_started
    timings: dict[str, float] = {}
    started = time.monotonic()
    snapshot = replay_account(_WORKER_SERVICE, ref, version, previous, timings)
    weak_started = time.monotonic()
    _final_summary(version, snapshot)
    encode_started = time.monotonic()
    blob, summary = encode_snapshot(snapshot)
    timings["weak_s"] = encode_started - weak_started
    timings["encode_s"] = time.monotonic() - encode_started
    timings["decode_s"] = decode_s
    return blob, summary, time.monotonic() - started, timings


def _replay(
    version: dict[str, Any], previous_blob: bytes | None = None
) -> tuple[str, dict[str, Any], float, dict[str, float]]:
    """回放一只标的（单只路径/测试用）。"""

    return _replay_one(version, previous_blob)


def _replay_batch(
    versions: list[dict[str, Any]], previous_blobs: list[bytes | None]
) -> list[tuple[str, Any]]:
    """一批标的共用同一份预取的检查点块；单只失败不连累同批其它标的。"""

    outcomes: list[tuple[str, Any]] = []
    for version, previous_blob in zip(versions, previous_blobs, strict=True):
        try:
            blob, summary, compute_s, timings = _replay_one(version, previous_blob)
        except Exception as exc:  # noqa: BLE001 - 一只失败不能连累同批
            outcomes.append(("error", str(exc)[:500] or type(exc).__name__))
        else:
            outcomes.append(("ok", (blob, summary, compute_s, timings)))
    return outcomes


def _snapshot_reusable(version: Mapping[str, Any], revision: str | None, target_date: str) -> bool:
    """行情日、数据修订与回放引擎都没变时，上一份快照就是这一轮的结果。

    只比日期不够：同一天的数据被修正（本地导入/全量刷新）时修订号会变，必须重放。
    旧快照没有修订号/引擎版本，第一次扫描重放一遍后写入摘要，之后才可复用。
    """

    if not target_date or not revision or str(version.get("status") or "") == "failed":
        return False
    summary = version.get("summary")
    if not isinstance(summary, Mapping):
        return False
    if "holding" not in summary:
        # 摘要不完整（旧版本只写了半份）就没法直接投影，照旧读检查点回放。
        return False
    if not isinstance(summary.get("full_period"), Mapping):
        # 升级前的老快照没有「回放到最新收盘」的全区间指标：重放一次把它补上，
        # 否则表格里的绩效会一直停在研究那一刻。
        return False
    return (
        str(summary.get("engine_version") or "") == ENGINE_VERSION
        and str(summary.get("data_revision") or "") == revision
        and str(summary.get("as_of_date") or "") == target_date
    )


class PoolTrackingRunner:
    def __init__(
        self,
        store: DuckDbMarketDataStore,
        budget: EvaluationWorkerBudget,
    ):
        self.store = store
        self.tracks = PoolTrackingStore(store)
        self.budget = budget

    def _instrument_names(self, versions: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        """按跟踪版本批量取证券简称，供信号行展示。

        买入/卖出信号行是用跟踪版本字典拼的——那里面只有 market/code，没有名字，
        前端就只剩一个悬空的「·」分隔符（2026-10-08 用户报告）。名字的权威来源是
        行情库的 instruments 表，一次批量查询拿全；持仓行不需要查，它的名字跟着
        回放引擎的 checkpoint 走（引擎内部的 ref 自带名字），两条路各自都有名字
        之后三张卡片才一致。
        """

        keys = {(str(v["market"]), str(v["code"])) for v in versions}
        if not keys:
            return {}
        placeholders = ", ".join("(?, ?)" for _ in keys)
        params: list[object] = []
        for market, code in sorted(keys):
            params.extend((market, code))
        with self.store.read_session() as connection:
            rows = connection.execute(
                f"""
                SELECT market, code, name FROM instruments
                WHERE (market, code) IN ({placeholders})
                """,
                params,
            ).fetchall()
        return {(str(row[0]), str(row[1])): str(row[2] or "") for row in rows}

    def _snapshot_blobs(self, version_ids: list[str]) -> dict[str, bytes]:
        """整批取上一份检查点的压缩块；读不到就整段重放，跳过只是优化。"""

        if not version_ids:
            return {}
        try:
            return self.tracks.snapshot_blobs(version_ids)
        except Exception as exc:  # noqa: BLE001 - 复用优化不能反过来让扫描失败
            logger.warning("读取跟踪检查点失败，本轮整段重放: %s", exc)
            return {}

    def _data_revisions(self, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """一次取本轮所有标的的数据修订号；读不到就全部回放，跳过只是优化。"""

        if not keys:
            return {}
        try:
            return self.store.data_revisions(keys)
        except Exception:  # noqa: BLE001 - 复用优化不能反过来让扫描失败
            logger.warning("无法读取行情数据修订号，本轮全部回放", exc_info=True)
            return {}

    def _entry_context(
        self, versions: list[dict[str, Any]]
    ) -> tuple[dict[tuple[str, str], float | None], dict[tuple[str, str], date | None]]:
        """按跟踪版本批量取条目的全区间基准与研究结论日期。

        基准在条目级 benchmark_json 里，与研究、信号扫描读的是同一份数据；
        ``updated_at`` 用来判断跟踪回放窗口有没有覆盖到研究结论那天（正常日更
        下总是覆盖，构造出来的短窗口夹具里不覆盖，就该退回研究口径）。
        """

        keys = {(str(v["market"]), str(v["code"])) for v in versions}
        if not keys:
            return {}, {}
        placeholders = ", ".join("(?, ?)" for _ in keys)
        params: list[object] = []
        for market, code in sorted(keys):
            params.extend((market, code))
        with self.store.state_read_session() as connection:
            rows = connection.execute(
                f"""
                SELECT market, code, benchmark_json, updated_at
                FROM strategy_pool_entries
                WHERE (market, code) IN ({placeholders})
                """,
                params,
            ).fetchall()
        returns: dict[tuple[str, str], float | None] = {}
        research_ends: dict[tuple[str, str], date | None] = {}
        for row in rows:
            try:
                benchmark = json.loads(row[2]) if row[2] else None
            except (TypeError, ValueError):
                benchmark = None
            key = (str(row[0]), str(row[1]))
            returns[key] = benchmark_full_return(
                benchmark if isinstance(benchmark, Mapping) else None
            )
            research_ends[key] = research_end_from_entry({"updated_at": row[3]})
        return returns, research_ends

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        progress: Callable[..., None],
        abort_check: Callable[[], bool],
        writer: Callable[[dict[str, Any]], Any],
        batch_writer: Callable[[list[dict[str, Any]]], list[Any]] | None = None,
    ) -> dict[str, Any]:
        run_started = time.monotonic()
        # 阶段耗时：主进程里串行的工作（整批读检查点块、写库等待）和 worker 里的并行回放
        # 分开记，否则只看总时长会误判瓶颈。全部是计数与单调时钟，不改任何行为。
        # ``replay_compute_s`` 是各 worker 计算耗时之和，并行时可能大于 ``replay_wall_s``；
        # 两者之差要结合 ``replay_overhead_s``（排队 + IPC + worker 启动）一起看。
        # ``REPLAY_PHASE_KEYS`` 把 compute 再拆开：每天只多处理一个交易日，单只却按整段
        # 历史付时间，得先看清是读行情、前缀校验还是全历史报告在吃。
        timing: dict[str, float | int] = {
            "prepare_s": 0.0,
            "replay_wall_s": 0.0,
            "snapshot_load_s": 0.0,
            "replay_compute_s": 0.0,
            "replay_overhead_s": 0.0,
            "write_s": 0.0,
            "total_s": 0.0,
            "reused_symbols": 0,
            "replayed_symbols": 0,
            "worker_starts": 0,
            **{key: 0.0 for key in REPLAY_PHASE_KEYS},
        }
        versions = self.tracks.current_versions()
        names = self._instrument_names(versions)
        benchmarks, research_ends = self._entry_context(versions)
        pool_total = len(versions)
        results: dict[str, dict[str, Any]] = {
            goal: dict(
                mode="signals",
                objective=goal,
                as_of_date=payload.get("as_of_date"),
                data_lag=payload.get("data_lag"),
                buy_signals=[],
                sell_signals=[],
                states=[],
                holding_scope="per_stock_strategy",
                holding_snapshot_complete=True,
                model_portfolio=dict(
                    available=True,
                    holding_scope="per_stock_strategy",
                    holdings=[],
                    risk_exit_signals=[],
                    failed_symbols=0,
                ),
            )
            for goal in OBJECTIVES
        }
        applied = {
            (v["market"], v["code"], v["objective"]): v
            for v in versions
            if v["source"] == "automatic"
        }
        applied.update(
            {
                (v["market"], v["code"], v["objective"]): v
                for v in versions
                if v["source"] == "manual"
            }
        )

        def is_applied(v: Mapping[str, Any]) -> bool:
            current = applied.get((v["market"], v["code"], v["objective"]), {})
            return bool(current.get("version_id") == v["version_id"])

        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        selected_ids = payload.get("version_ids")
        selected_versions = [
            v for v in versions if selected_ids is None or v["version_id"] in selected_ids
        ]
        total = len(selected_versions)
        target_data_date = str(payload.get("as_of_date") or "")
        revisions = self._data_revisions(
            {(str(v["market"]), str(v["code"])) for v in selected_versions}
        )
        for v in selected_versions:
            key = (
                v["market"],
                v["code"],
                v["signature"],
                v["summary"].get("checkpoint_hash"),
                bool(v["summary"]),
            )
            groups.setdefault(key, []).append(v)
        # 复用与否只看摘要，先整体判定一遍；后面按这个顺序消费，需要回放的那几只
        # 检查点也按同样的顺序成批预取（一只检查点解压后接近 1 MB，不能一次全拿）。
        work_items = list(groups.values())
        reusable = [
            all(
                _snapshot_reusable(
                    member,
                    revisions.get((str(member["market"]), str(member["code"]))),
                    target_data_date,
                )
                for member in group
            )
            for group in work_items
        ]
        # 回放任务按批提交：整批只开一次状态库连接读检查点，也不让单只慢票把整轮拖长。
        # 复用组仍逐组内联处理（不提交任何任务），保持原来的「行情没变就秒过」。
        tasks: list[tuple[list[list[dict[str, Any]]], bool]] = []
        chunk: list[list[dict[str, Any]]] = []
        for group, is_reusable in zip(work_items, reusable, strict=True):
            if is_reusable:
                if chunk:
                    tasks.append((chunk, False))
                    chunk = []
                tasks.append(([group], True))
            else:
                chunk.append(group)
                if len(chunk) >= REPLAY_CHUNK_SIZE:
                    tasks.append((chunk, False))
                    chunk = []
        if chunk:
            tasks.append((chunk, False))
        replay_ids = [
            group[0]["version_id"]
            for group, is_reusable in zip(work_items, reusable, strict=True)
            if not is_reusable
        ]
        blob_window: dict[str, bytes] = {}
        blob_cursor = 0

        def take_blob(version_id: str) -> bytes | None:
            """上一份检查点的压缩块：窗口里没有再补一批（正常就是接下来要用的那几只）。"""

            nonlocal blob_cursor
            if version_id in blob_window:
                return blob_window.pop(version_id)
            chunk_ids = replay_ids[blob_cursor : blob_cursor + SNAPSHOT_PREFETCH]
            blob_cursor += len(chunk_ids)
            started = time.monotonic()
            blob_window.update(self._snapshot_blobs(chunk_ids))
            timing["snapshot_load_s"] += time.monotonic() - started
            return blob_window.pop(version_id, None)

        completed = failed = skipped = 0
        # worker 各阶段的耗时在这里汇总（各进程之和）；只统计认得的键，
        # 免得 worker 与主进程版本不一致时静默丢数。
        phase_totals: dict[str, float] = dict.fromkeys(REPLAY_PHASE_KEYS, 0.0)
        # 逐只写库实测 15.8 ms：一次全市场扫描 4,409 只就是约 70 秒串行写库。批量上报把
        # 一批合成一个事务（实测摊到 0.14 ms/行），快照是派生数据，丢了下一轮重算。
        snapshot_batch: list[dict[str, Any]] = []

        def write_batch(batch: list[dict[str, Any]]) -> list[Any]:
            """写一批快照，返回逐只回执；不碰计数器（回执在主线程里结算）。"""

            acks: list[Any] | None = None
            if batch_writer is not None:
                try:
                    acks = batch_writer(batch)
                except TransientWorkerApiError as exc:
                    logger.warning("批量保存跟踪快照暂不可用，退回逐只写入: %s", exc)
                except Exception:
                    logger.exception("批量保存跟踪快照失败，退回逐只写入")
            if acks is not None:
                return list(acks)
            acks = []
            for item in batch:
                try:
                    acks.append(writer(item))
                except TransientWorkerApiError as exc:
                    logger.warning(
                        "跟踪快照暂未落库，下一轮重试 version_id=%s: %s",
                        item.get("version_id"),
                        exc,
                    )
                    acks.append({"saved": False})
                except Exception:
                    logger.exception("无法保存跟踪快照 version_id=%s", item.get("version_id"))
                    acks.append({"saved": False})
            return acks

        if not groups:
            progress(
                0,
                total,
                "跟踪各股策略",
                dict(
                    mode="signals",
                    completed_symbols=0,
                    failed_symbols=0,
                    total_symbols=total,
                    current_parallelism=0,
                    maximum_parallelism=pool_worker_cap(),
                    phase_label="增量跟踪各股账户",
                ),
            )

        def project_reused(v: dict[str, Any]) -> None:
            """复用上一份快照时直接按摘要投影。

            ``summary_json`` 就是上一轮 ``consume`` 的产物（已经合并过持仓与账户字段），
            所以复用路径不需要再把检查点 blob 读出来解压。
            """

            summary = v.get("summary")
            if not isinstance(summary, Mapping) or not is_applied(v):
                return
            project(v, dict(summary))

        def project(v: dict[str, Any], summary: dict[str, Any]) -> None:
            result = results[v["objective"]]
            result["as_of_date"] = payload.get("as_of_date") or summary["as_of_date"]
            row = dict(
                market=v["market"],
                code=v["code"],
                symbol=f"{v['market']}{v['code']}",
                name=names.get((v["market"], v["code"]), ""),
                candidate_id=v["candidate"].get("candidate_id"),
                tracking_version_id=v["version_id"],
                as_of_date=summary["as_of_date"],
                status="held" if summary["holding"] else "flat",
            )
            fresh = not payload.get("as_of_date") or payload["as_of_date"] == summary["as_of_date"]
            # 买入信号有四层含义，这里取它们的交集：
            # 1. ``fresh``——快照停在最新交易日，不是停牌/失败留下的旧状态；
            # 2. ``signal_buy``——买入规则当日成立（规则事实）；
            # 3. 没有 ``buy_blocked_reason``——执行层没有否决它。风控冷却期内的标的
            #    规则可以成立，但选股与买入两层都会跳过，注定不成交，展示成买入信号
            #    会误导（2026-10-08 用户澄清）。已持仓标的没有阻断原因，因此「持仓中
            #    继续触发买入」照常显示。
            # 4. 策略质量过关（2026-10-09 用户决策）——全区间样本不足或跑输基准的
            #    策略，三个信号列表都不展示，状态退回 held/flat。判定用该版本自带
            #    候选指标（sync_entry 与条目保持同步）+ 条目级基准，与信号扫描路径
            #    （service._scan_signals）共用 quality 模块，口径必须一致。
            symbol_key = (str(v["market"]), str(v["code"]))
            weak_metrics, weak_benchmark = fresh_quality_inputs(
                summary,
                v["candidate"] if isinstance(v["candidate"], Mapping) else None,
                fallback_benchmark=benchmarks.get(symbol_key),
                research_end=research_ends.get(symbol_key),
            )
            weak_reasons = weak_strategy_reasons(weak_metrics, weak_benchmark)
            buy = (
                fresh
                and summary["signal_buy"]
                and not summary.get("buy_blocked_reason")
                and not weak_reasons
            )
            sell = fresh and summary["signal_sell"] and not weak_reasons
            # 状态列同样只认可执行的买入：被冷却期挡掉的行退回 held/flat，不再标 buy。
            # 持仓事实由另外两层表达：今日持仓中面板、条目表的「持仓中」徽章
            # （entryIsHeld，来自回放的日终持仓）；买入面板的行自带「当前持仓收益」，
            # 持仓上下文在信号行内即可见。状态列按规则事实取优先级，held/flat 只在
            # 没有规则触发时兜底。
            row["status"] = (
                "buy_sell" if buy and sell else "buy" if buy else "sell" if sell else row["status"]
            )
            result["states"].append(row)
            if buy:
                result["buy_signals"].append({**row, "signal_type": "buy"})
            if sell:
                result["sell_signals"].append({**row, "signal_type": "sell"})
            for h in summary["holding"]:
                if weak_reasons:
                    # 弱策略的持仓行同样隐藏（三个列表口径一致，2026-10-09 用户决策）。
                    continue
                holding = {**h, "status": "held"}
                # 没参与本批回放的版本，快照可能停在更早的交易日（停牌、数据延迟、
                # 当日失败）。直接丢弃会让模型组合在增量作业里几乎变空，所以保留但
                # 标出来：前端不该把「昨天已平仓」当成当日持仓。风险退出信号走的是
                # 另一条路（下面 ``if fresh``），两处口径至此一致。
                if not fresh:
                    holding["status"] = "held_stale"
                    holding["as_of_date"] = summary["as_of_date"]
                # account_summary 已补算 unrealized_pnl_pct，这里映射为前端读取的
                # holding_return_pct（与信号扫描路径 service._scan_signals 口径一致）
                pnl = h.get("unrealized_pnl_pct")
                if isinstance(pnl, (int, float)) and math.isfinite(pnl):
                    holding["holding_return_pct"] = float(pnl)
                result["model_portfolio"]["holdings"].append(holding)
            if fresh:
                result["model_portfolio"]["risk_exit_signals"].extend(
                    t
                    for t in summary.get("risk_exits", [])
                    if t["date"] == summary["as_of_date"] and t.get("risk_trigger")
                )

        if selected_ids is not None:
            for v in versions:
                if (
                    v["version_id"] not in selected_ids
                    and v["summary"]
                    and applied.get((v["market"], v["code"], v["objective"]), {}).get("version_id")
                    == v["version_id"]
                ):
                    project(v, v["summary"])

        def fail_group(group: list[dict[str, Any]], message: str) -> None:
            nonlocal failed
            failed += len(group)
            for v in group:
                try:
                    writer(
                        dict(
                            version_id=v["version_id"],
                            signature=v["signature"],
                            error=message,
                        )
                    )
                except Exception:
                    logger.exception(
                        "无法记录策略追踪失败状态 version_id=%s",
                        v["version_id"],
                    )
                if is_applied(v):
                    result = results[v["objective"]]
                    result["holding_snapshot_complete"] = False
                    result["model_portfolio"]["failed_symbols"] += 1

        def apply_snapshot(group: list[dict[str, Any]], blob: str, summary: dict[str, Any]) -> None:
            nonlocal completed, failed, skipped
            for v in group:
                entry = dict(
                    version_id=v["version_id"],
                    signature=v["signature"],
                    snapshot_blob=blob,
                    summary=summary,
                )
                try:
                    if batch_writer is not None:
                        snapshot_batch.append(entry)
                        if len(snapshot_batch) >= SNAPSHOT_BATCH_SIZE:
                            flush_snapshots()
                        if is_applied(v):
                            project(v, summary)
                        completed += 1
                        continue
                    # 没有批量通道（单只重跑等）：写被拒就不投影，面板里不留没落库的状态。
                    ack = writer(entry)
                except TransientWorkerApiError as exc:
                    # 传输/忙碌不是策略失败：版本状态保持原样，下一轮扫描重试。
                    skipped += 1
                    logger.warning(
                        "跟踪快照暂未落库，下一轮重试 version_id=%s: %s",
                        v["version_id"],
                        exc,
                    )
                    if is_applied(v):
                        project(v, summary)
                    continue
                except TaskCancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    try:
                        writer(
                            dict(
                                version_id=v["version_id"],
                                signature=v["signature"],
                                error=str(exc)[:500],
                            )
                        )
                    except Exception:
                        logger.exception(
                            "无法记录策略追踪失败状态 version_id=%s",
                            v["version_id"],
                        )
                    if is_applied(v):
                        result = results[v["objective"]]
                        result["holding_snapshot_complete"] = False
                        result["model_portfolio"]["failed_symbols"] += 1
                    continue
                if isinstance(ack, Mapping) and ack.get("saved") is False:
                    skipped += 1
                    continue
                if is_applied(v):
                    project(v, summary)
                completed += 1

        ramp = PoolConcurrencyRamp(
            interval_seconds=TRACKING_RAMP_INTERVAL_SECONDS,
            initial_target=TRACKING_RAMP_INITIAL_WORKERS,
        )
        maximum = pool_worker_cap()
        timing["prepare_s"] = time.monotonic() - run_started
        replay_started = time.monotonic()
        with self.budget.stock(), ExitStack() as cleanup:
            # 写库放到后台线程：压缩 + 批量写一批约十几秒，占着这段主循环就等于让所有
            # worker 空转（全量扫描实测写库占墙钟 46%）。最多排两批，超出的先等最早的。
            flush_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-write")
            cleanup.callback(flush_pool.shutdown, wait=False, cancel_futures=True)
            flush_futures: list[Future[list[Any]]] = []

            def apply_acks(acks: list[Any]) -> None:
                nonlocal skipped, completed
                for ack in acks:
                    if isinstance(ack, Mapping) and ack.get("saved") is False:
                        # 这些标的进循环时已按「已完成」计过，退回给 skipped，三个计数不重复。
                        skipped += 1
                        completed -= 1

            def drain_one() -> None:
                if not flush_futures:
                    return
                started = time.monotonic()
                acks = flush_futures.pop(0).result()
                timing["write_s"] += time.monotonic() - started
                apply_acks(acks)

            def flush_snapshots(*, drain_all: bool = False) -> None:
                if snapshot_batch:
                    batch, snapshot_batch[:] = list(snapshot_batch), []
                    flush_futures.append(flush_pool.submit(write_batch, batch))
                while flush_futures and (drain_all or len(flush_futures) >= 2):
                    drain_one()

            def new_slot(blocking: bool) -> tuple[ExitStack, ProcessPoolExecutor] | None:
                context = ExitStack()
                try:
                    grant = context.enter_context(
                        self.budget.lease(1, abort_check) if blocking else self.budget.try_lease()
                    )
                    if not grant:
                        context.close()
                        return None
                    executor = context.enter_context(
                        ProcessPoolExecutor(
                            max_workers=1,
                            mp_context=multiprocessing.get_context("spawn"),
                            initializer=_initialize,
                            initargs=(str(self.store.database_path),),
                        )
                    )
                    cleanup.callback(context.close)
                    timing["worker_starts"] += 1
                    return context, executor
                except BaseException:
                    context.close()
                    raise

            # 首个 slot 只在「确实有要回放的组」时申请：建一个 slot 要 spawn 进程 + 重新
            # import（实测 0.7-0.9 秒），全命中复用时它整轮都在闲着，白付一次冷启动。
            first = new_slot(True) if replay_ids else None
            slots = [first] if first is not None else []
            pending: dict[Any, tuple[list[list[dict[str, Any]]], ProcessPoolExecutor, float]] = {}
            task_cursor = 0
            exhausted = not bool(tasks)

            def report_progress() -> None:
                progress(
                    completed + failed + skipped,
                    max(1, total),
                    "跟踪各股策略",
                    dict(
                        mode="signals",
                        completed_symbols=completed,
                        failed_symbols=failed,
                        total_symbols=total,
                        current_parallelism=len(pending),
                        maximum_parallelism=maximum,
                        phase_label="增量跟踪各股账户",
                    ),
                )

            while not exhausted or pending:
                if abort_check():
                    for future in pending:
                        future.cancel()
                    raise TaskCancelledError("每日策略池跟踪已取消")
                # 复用组就地处理，不占进程也不占 slot：先把排在队头的复用组全处理掉，
                # 行情没变的整轮就能在零个 worker 的情况下扫完。
                while task_cursor < len(tasks) and tasks[task_cursor][1]:
                    chunk, _reusable = tasks[task_cursor]
                    task_cursor += 1
                    # 同一行情日、同一份数据、同一版引擎：上一份快照就是这一轮的结果。
                    # 摘要里已经有上一轮算好的持仓与账户字段，不提交回放、也不读检查点。
                    group = chunk[0]
                    timing["reused_symbols"] += len(group)
                    for member in group:
                        project_reused(member)
                        completed += 1
                    report_progress()
                exhausted = task_cursor >= len(tasks)
                memory = memory_status()
                memory_limit = memory_bounded_worker_count(
                    status=memory,
                    estimated_worker_bytes=512 * 1024**2,
                    maximum_workers=maximum,
                    active_workers=len(pending),
                )
                cpu_limit = cpu_bounded_worker_count(
                    load_average_1m=get_cpu_load_average_1m(),
                    visible_cpus=visible_cpu_count(),
                    maximum_workers=maximum,
                    active_workers=len(pending),
                )
                target = ramp.target(
                    resource_limit=min(memory_limit, cpu_limit),
                    active_workers=len(pending),
                    now=time.monotonic(),
                )
                busy = {id(executor) for _, executor, _ in pending.values()}
                # 回收只看内存，不看 CPU 压力：把并发拉起来之后抬高分位数的主要是我们
                # 自己那批 worker，按 CPU 拆掉再重开要再付一次 spawn+import。实测一轮
                # 101 次启动、有效并行只有 2.4/8。空闲 slot 不占 CPU，只占配额，下一批直接用。
                affordable = memory_bounded_worker_count(
                    status=memory,
                    estimated_worker_bytes=512 * 1024**2,
                    maximum_workers=maximum,
                    active_workers=0,
                )
                keep = max(1, affordable)
                if len(slots) > keep:
                    for slot in list(reversed(slots)):
                        if len(slots) <= keep:
                            break
                        if id(slot[1]) not in busy:
                            slot[0].close()
                            slots.remove(slot)
                if not exhausted and len(slots) < target:
                    borrowed = new_slot(False)
                    if borrowed:
                        slots.append(borrowed)
                for _, executor in slots[:target]:
                    if exhausted or id(executor) in busy:
                        continue
                    # 队头到这里一定已经不是复用组（上面刚排干净），直接当回放任务领。
                    chunk, _reusable = tasks[task_cursor]
                    task_cursor += 1
                    exhausted = task_cursor >= len(tasks)
                    versions = [
                        dict(
                            group[0],
                            target_data_date=payload.get("as_of_date"),
                            data_revision=revisions.get(
                                (str(group[0]["market"]), str(group[0]["code"]))
                            ),
                            benchmark_return=benchmarks.get(
                                (str(group[0]["market"]), str(group[0]["code"]))
                            ),
                            research_end=research_ends.get(
                                (str(group[0]["market"]), str(group[0]["code"]))
                            ),
                        )
                        for group in chunk
                    ]
                    previous_blobs = [take_blob(group[0]["version_id"]) for group in chunk]
                    pending[executor.submit(_replay_batch, versions, previous_blobs)] = (
                        chunk,
                        executor,
                        time.monotonic(),
                    )
                if not pending:
                    if exhausted:
                        break
                    if not slots or target <= 0:
                        raise TaskUserError("资源不足，本轮信号扫描已跳过，稍后自动重试")
                    # 复用路径不提交任何任务（行情没变时整轮都是复用），以前这里直接
                    # break，结果只扫到第一组就当成"扫完了"发布出去：周末全命中复用时，
                    # 当天面板会只剩一只标的，其余全部显示"信号待刷新"。还有组没处理
                    # 就得继续下一轮。
                    continue
                finished, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in finished:
                    chunk, _executor, submitted_at = pending.pop(future)
                    try:
                        outcomes = future.result()
                    except TaskCancelledError:
                        raise
                    except Exception as exc:
                        for chunk_group in chunk:
                            fail_group(chunk_group, str(exc)[:500])
                        continue
                    elapsed = time.monotonic() - submitted_at
                    computed = 0.0
                    for group, outcome in zip(chunk, outcomes, strict=True):
                        status, outcome_payload = outcome
                        if status != "ok":
                            fail_group(group, str(outcome_payload))
                            continue
                        blob, summary, compute_s, timings = cast(
                            tuple[str, dict[str, Any], float, dict[str, float]], outcome_payload
                        )
                        computed += compute_s
                        timing["replay_compute_s"] += compute_s
                        timing["replayed_symbols"] += len(group)
                        for phase_key, phase_seconds in timings.items():
                            if phase_key in phase_totals:
                                phase_totals[phase_key] += phase_seconds
                        apply_snapshot(group, blob, summary)
                    timing["replay_overhead_s"] += max(0.0, elapsed - computed)
                report_progress()
            # 最后一批也要落库：仍走后台线程，避免重复一套同步写。
            flush_snapshots(drain_all=True)
        timing["replay_wall_s"] = time.monotonic() - replay_started
        timing["total_s"] = time.monotonic() - run_started
        timing.update(phase_totals)
        logger.info(
            "策略池信号扫描耗时 %.1fs：准备 %.1fs，回放墙钟 %.1fs"
            "（计算 %.1fs，排队/IPC %.1fs），检查点读取 %.1fs，写库等待 %.1fs；"
            "复用 %d 只，回放 %d 只，worker 启动 %d 次",
            timing["total_s"],
            timing["prepare_s"],
            timing["replay_wall_s"],
            timing["replay_compute_s"],
            timing["replay_overhead_s"],
            timing["snapshot_load_s"],
            timing["write_s"],
            timing["reused_symbols"],
            timing["replayed_symbols"],
            timing["worker_starts"],
        )
        replayed = max(1, int(timing["replayed_symbols"]))
        logger.info(
            "跟踪回放阶段拆解（各进程之和，回放 %d 只；括号内是单只均摊）：%s",
            replayed,
            "，".join(
                f"{REPLAY_PHASE_LABELS[key]} {phase_totals[key]:.1f}s"
                f"({phase_totals[key] * 1000 / replayed:.0f}ms)"
                for key in REPLAY_PHASE_KEYS
            ),
        )
        goal = str(payload.get("objective") or "annual_return")
        return {
            **results[goal],
            "results_by_objective": results,
            "total_symbols": total,
            "pool_total_symbols": pool_total,
            "completed_symbols": completed,
            "failed_symbols": failed,
            "skipped_symbols": skipped,
            "timing": timing,
        }
