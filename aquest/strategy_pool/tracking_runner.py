"""Independent pool account refresh channel sharing the research process budget."""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aquest.concurrency import visible_cpu_count
from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.jobs import TaskCancelledError, TaskUserError
from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
from aquest.strategy_discovery.worker_api_client import TransientWorkerApiError

from .quality import benchmark_full_return, weak_strategy_reasons
from .resources import (
    PoolConcurrencyRamp,
    cpu_bounded_worker_count,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    memory_status,
    pool_worker_cap,
)
from .tracking import account_summary, replay_account
from .tracking_store import ENGINE_VERSION, OBJECTIVES, PoolTrackingStore

_WORKER_SERVICE: PortfolioBacktestService | None = None
logger = logging.getLogger(__name__)

#: 一次批量上报多少只跟踪快照。64 只 × 4 个参数远低于 SQLite 的 999 变量上限，
#: 单批快照体量约十几 MB；再大只会把一次请求的超时和内存风险放大。
SNAPSHOT_BATCH_SIZE = 64


def _initialize(database_path: str) -> None:
    global _WORKER_SERVICE
    store = DuckDbMarketDataStore(database_path, read_only=True)
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    cache_store = store if store.indicator_cache_backend == "sqlite" else None
    _WORKER_SERVICE = PortfolioBacktestService(
        adapter=adapter, indicator_cache=IndicatorCache(cache_store, max_memory_entries=64)
    )


def _replay(version: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if _WORKER_SERVICE is None:
        raise RuntimeError("跟踪回放进程未初始化")
    adapter = _WORKER_SERVICE._adapter
    clear = getattr(adapter, "clear_thread_stock_cache", None)
    if callable(clear):
        clear()
    ref = adapter.stock_ref("", version["market"], version["code"])
    return replay_account(_WORKER_SERVICE, ref, version, previous)


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

    def _data_revisions(self, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """一次取本轮所有标的的数据修订号；读不到就全部回放，跳过只是优化。"""

        if not keys:
            return {}
        try:
            return self.store.data_revisions(keys)
        except Exception:  # noqa: BLE001 - 复用优化不能反过来让扫描失败
            logger.warning("无法读取行情数据修订号，本轮全部回放", exc_info=True)
            return {}

    def _benchmark_returns(
        self, versions: list[dict[str, Any]]
    ) -> dict[tuple[str, str], float | None]:
        """按跟踪版本批量取条目的全区间基准收益，供弱策略判定使用。

        跟踪版本自带候选指标（sync_entry 与条目保持同步），基准在条目级
        benchmark_json 里——与信号扫描路径读的是同一份数据，口径一致。
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
                SELECT market, code, benchmark_json FROM strategy_pool_entries
                WHERE (market, code) IN ({placeholders})
                """,
                params,
            ).fetchall()
        result: dict[tuple[str, str], float | None] = {}
        for row in rows:
            try:
                benchmark = json.loads(row[2]) if row[2] else None
            except (TypeError, ValueError):
                benchmark = None
            result[(str(row[0]), str(row[1]))] = benchmark_full_return(
                benchmark if isinstance(benchmark, Mapping) else None
            )
        return result

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        progress: Callable[..., None],
        abort_check: Callable[[], bool],
        writer: Callable[[dict[str, Any]], Any],
        batch_writer: Callable[[list[dict[str, Any]]], list[Any]] | None = None,
    ) -> dict[str, Any]:
        versions = self.tracks.current_versions()
        names = self._instrument_names(versions)
        benchmarks = self._benchmark_returns(versions)
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
        work = iter(groups.values())
        completed = failed = skipped = 0
        # 逐只写库实测 15.8 ms：一次全市场扫描 4,409 只就是约 70 秒串行写库。批量上报把
        # 一批合成一个事务（实测摊到 0.14 ms/行），快照是派生数据，丢了下一轮重算。
        snapshot_batch: list[dict[str, Any]] = []

        def flush_snapshots() -> None:
            nonlocal skipped, completed
            if not snapshot_batch:
                return
            batch, snapshot_batch[:] = list(snapshot_batch), []
            acks: list[Any] | None = None
            if batch_writer is not None:
                try:
                    acks = batch_writer(batch)
                except TransientWorkerApiError as exc:
                    logger.warning("批量保存跟踪快照暂不可用，退回逐只写入: %s", exc)
                except Exception:
                    logger.exception("批量保存跟踪快照失败，退回逐只写入")
            if acks is None:
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
            for ack in acks:
                if isinstance(ack, Mapping) and ack.get("saved") is False:
                    # 这些标的进循环时已按「已完成」计过，退回给 skipped，三个计数不重复。
                    skipped += 1
                    completed -= 1

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

        def consume(v: dict[str, Any], snapshot: dict[str, Any], *, persist: bool = True) -> None:
            nonlocal skipped
            snapshot = dict(snapshot)
            boundary = (
                v.get("launch_data_date")
                or datetime.fromisoformat(v["created_at"])
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .date()
                .isoformat()
            )
            summary = {
                **snapshot["summary"],
                **account_summary(
                    snapshot["checkpoint"],
                    boundary,
                    float(v["candidate"].get("config", {}).get("initial_cash", 1000000)),
                    (snapshot.get("report") or {}).get("current_state"),
                ),
            }
            # 这两个值只用于判断下一轮能否复用；摘要里已有就不再写库。
            summary["engine_version"] = ENGINE_VERSION
            summary["data_revision"] = revisions.get((str(v["market"]), str(v["code"])))
            snapshot["summary"] = summary
            if persist:
                entry = dict(
                    version_id=v["version_id"], signature=v["signature"], snapshot=snapshot
                )
                if batch_writer is not None:
                    snapshot_batch.append(entry)
                else:
                    try:
                        ack = writer(entry)
                    except TransientWorkerApiError:
                        # 本轮的回放结果仍然有效，只是快照没落库；照常产出信号，下一轮再存。
                        if is_applied(v):
                            project(v, summary)
                        raise
                    if isinstance(ack, Mapping) and ack.get("saved") is False:
                        skipped += 1
                        return
            if not is_applied(v):
                return
            project(v, summary)

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
            weak_reasons = weak_strategy_reasons(
                v["candidate"].get("metrics") if isinstance(v["candidate"], Mapping) else None,
                benchmarks.get((str(v["market"]), str(v["code"]))),
            )
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
        ramp = PoolConcurrencyRamp()
        maximum = pool_worker_cap()
        with self.budget.stock(), ExitStack() as cleanup:

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
                    return context, executor
                except BaseException:
                    context.close()
                    raise

            first = new_slot(True) if groups else None
            slots = [first] if first is not None else []
            pending: dict[Any, tuple[list[dict[str, Any]], ProcessPoolExecutor]] = {}
            exhausted = not bool(groups)

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
                memory = memory_status()
                target = ramp.target(
                    resource_limit=min(
                        memory_bounded_worker_count(
                            status=memory,
                            estimated_worker_bytes=512 * 1024**2,
                            maximum_workers=maximum,
                            active_workers=len(pending),
                        ),
                        cpu_bounded_worker_count(
                            load_average_1m=get_cpu_load_average_1m(),
                            visible_cpus=visible_cpu_count(),
                            maximum_workers=maximum,
                            active_workers=len(pending),
                        ),
                    ),
                    active_workers=len(pending),
                    now=time.monotonic(),
                )
                busy = {id(executor) for _, executor in pending.values()}
                if len(slots) > target:
                    for slot in list(reversed(slots)):
                        if len(slots) <= target:
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
                    group = next(work, None)
                    if group is None:
                        exhausted = True
                        break
                    v = dict(group[0], target_data_date=payload.get("as_of_date"))
                    if all(
                        _snapshot_reusable(
                            member,
                            revisions.get((str(member["market"]), str(member["code"]))),
                            target_data_date,
                        )
                        for member in group
                    ):
                        # 同一行情日、同一份数据、同一版引擎：上一份快照就是这一轮的结果。
                        # 摘要里已经有上一轮算好的持仓与账户字段，不必再解压检查点——
                        # 一条序列的检查点解压加解析约 10 ms，全市场就是 40 秒。
                        for member in group:
                            project_reused(member)
                            completed += 1
                        report_progress()
                        continue
                    previous = self.tracks.snapshot(v["version_id"])
                    pending[executor.submit(_replay, v, previous)] = (group, executor)
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
                    group, _executor = pending.pop(future)
                    try:
                        snapshot = future.result()
                    except TaskCancelledError:
                        raise
                    except Exception as exc:
                        failed += len(group)
                        for v in group:
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
                    else:
                        for v in group:
                            skipped_before = skipped
                            try:
                                consume(v, snapshot)
                            except TaskCancelledError:
                                raise
                            except TransientWorkerApiError as exc:
                                # 传输/忙碌不是策略失败：版本状态保持原样，下一轮扫描重试。
                                skipped += 1
                                logger.warning(
                                    "跟踪快照暂未落库，下一轮重试 version_id=%s: %s",
                                    v["version_id"],
                                    exc,
                                )
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
                            else:
                                if skipped == skipped_before:
                                    completed += 1
                                if len(snapshot_batch) >= SNAPSHOT_BATCH_SIZE:
                                    flush_snapshots()
                report_progress()
        flush_snapshots()
        goal = str(payload.get("objective") or "annual_return")
        return {
            **results[goal],
            "results_by_objective": results,
            "total_symbols": total,
            "pool_total_symbols": pool_total,
            "completed_symbols": completed,
            "failed_symbols": failed,
            "skipped_symbols": skipped,
        }
