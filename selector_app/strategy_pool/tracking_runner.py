"""Independent pool account refresh channel sharing the research process budget."""

from __future__ import annotations

import math
import multiprocessing
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from selector_app.concurrency import visible_cpu_count
from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.jobs import TaskCancelledError
from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget

from .resources import (
    PoolConcurrencyRamp,
    cpu_bounded_worker_count,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    memory_status,
    pool_worker_cap,
)
from .tracking import account_summary, replay_account
from .tracking_store import OBJECTIVES, PoolTrackingStore

_WORKER_SERVICE: PortfolioBacktestService | None = None


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


class PoolTrackingRunner:
    def __init__(self, store: DuckDbMarketDataStore, budget: EvaluationWorkerBudget):
        self.store = store
        self.tracks = PoolTrackingStore(store)
        self.budget = budget

    def run(
        self,
        payload: Mapping[str, Any],
        *,
        progress: Callable[..., None],
        abort_check: Callable[[], bool],
        writer: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        versions = self.tracks.current_versions()
        total = len(versions)
        results: dict[str, dict[str, Any]] = {
            goal: dict(
                mode="signals",
                objective=goal,
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
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        selected_ids = payload.get("version_ids")
        for v in versions:
            if selected_ids is not None and v["version_id"] not in selected_ids:
                continue
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

        def consume(v: dict[str, Any], snapshot: dict[str, Any]) -> None:
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
                ),
            }
            snapshot["summary"] = summary
            ack = writer(
                dict(version_id=v["version_id"], signature=v["signature"], snapshot=snapshot)
            )
            if isinstance(ack, Mapping) and ack.get("saved") is False:
                skipped += 1
                return
            if (
                applied.get((v["market"], v["code"], v["objective"]), {}).get("version_id")
                != v["version_id"]
            ):
                return
            project(v, summary)

        def project(v: dict[str, Any], summary: dict[str, Any]) -> None:
            result = results[v["objective"]]
            result["as_of_date"] = payload.get("as_of_date") or summary["as_of_date"]
            row = dict(
                market=v["market"],
                code=v["code"],
                symbol=f"{v['market']}{v['code']}",
                candidate_id=v["candidate"].get("candidate_id"),
                tracking_version_id=v["version_id"],
                as_of_date=summary["as_of_date"],
                status="held" if summary["holding"] else "flat",
            )
            fresh = not payload.get("as_of_date") or payload["as_of_date"] == summary["as_of_date"]
            buy, sell = fresh and summary["signal_buy"], fresh and summary["signal_sell"]
            row["status"] = (
                "buy_sell" if buy and sell else "buy" if buy else "sell" if sell else row["status"]
            )
            result["states"].append(row)
            if buy:
                result["buy_signals"].append({**row, "signal_type": "buy"})
            if sell:
                result["sell_signals"].append({**row, "signal_type": "sell"})
            for h in summary["holding"]:
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

            first = new_slot(True)
            assert first is not None
            slots = [first]
            pending: dict[Any, tuple[list[dict[str, Any]], ProcessPoolExecutor]] = {}
            exhausted = False
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
                    previous = self.tracks.snapshot(v["version_id"])
                    pending[executor.submit(_replay, v, previous)] = (group, executor)
                if not pending:
                    break
                finished, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in finished:
                    group, _executor = pending.pop(future)
                    try:
                        snapshot = future.result()
                        for v in group:
                            consume(v, snapshot)
                        completed += len(group)
                    except TaskCancelledError:
                        raise
                    except Exception as exc:
                        failed += len(group)
                        for v in group:
                            writer(
                                dict(
                                    version_id=v["version_id"],
                                    signature=v["signature"],
                                    error=str(exc)[:500],
                                )
                            )
                            results[v["objective"]]["holding_snapshot_complete"] = False
                progress(
                    completed + failed,
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
        goal = str(payload.get("objective") or "annual_return")
        return {
            **results[goal],
            "results_by_objective": results,
            "total_symbols": total,
            "completed_symbols": completed - skipped,
            "failed_symbols": failed,
            "skipped_symbols": skipped,
        }
