"""Online market-data synchronization endpoints.

同步、回填、兼容别名 ``/sync`` 共用 ``_create_sync_job``：这条路径只走在线
通道和本地行情库，本地 vipdoc 导入在 ``local_import`` 里另有一套。
"""

from __future__ import annotations

from datetime import date, datetime
from inspect import Parameter, signature
from zoneinfo import ZoneInfo

import duckdb
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aquest.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
)
from aquest.adapters.online_market_sync import OnlineProviderError
from aquest.market_data.auto_sync import (
    FULL_REFRESH_DESCRIPTION,
    MARKET_SYNC_EXCLUSIVE_GROUP,
)
from aquest.market_data.store import MarketDataStoreError
from aquest.market_data.sync_session import (
    latest_completed_session,
    read_synced_session,
    refresh_calendar_when_stale,
    sync_scope_fingerprint,
)
from aquest.screening.jobs import (
    ActiveJobConflictError,
    ProgressReporter,
    TaskCancelledError,
    TaskUserError,
    report_progress_detail,
)

from ...schemas import MarketRefreshRequest, MarketSyncRequest
from ._shared import (
    MarketSyncService,
    _invalidate_indicator_cache,
    _market_status,
    _reject_duplicate_market_job,
    _runner,
    _service,
    _storage_error_message,
    _store,
)
from .precompute import _with_indicator_precompute

router = APIRouter(tags=["market-data"])

#: 重刷要删掉两种写入来源：本地导入的行永久压过在线行，只删 online 覆盖不到它们。
_BAR_SOURCES = ("online", "local")
#: 结果里只点名前几只"删了没补回来"的标的，全列会淹掉整份报告。
_EMPTY_REPORT_LIMIT = 20


@router.post("/sync-online", status_code=status.HTTP_202_ACCEPTED)
def create_online_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request)


@router.post("/backfill", status_code=status.HTTP_202_ACCEPTED)
def create_eastmoney_backfill_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    if payload.provider != "eastmoney":
        raise HTTPException(status_code=422, detail="全量回填只支持在线日线通道")
    # 回填只覆盖股票；没显式给类型时回落到股票，保持旧调用的行为不变。
    full_payload = payload.model_copy(
        update={
            "provider": "eastmoney",
            "instrument_types": payload.instrument_types or ["stock"],
            "full_history": True,
        }
    )
    return _create_sync_job(
        full_payload,
        request,
        description="历史全量回填",
    )


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
def create_full_refresh_job(payload: MarketRefreshRequest, request: Request) -> JSONResponse:
    """Re-download the selected scope from scratch.

    常规同步只补最新一段，全量补齐只补已有区间**之外**的缺口——中间那段写歪了
    两个入口都覆盖不到。这里先把范围内标的的日线删干净，再整段重新下载。
    """

    return _create_sync_job(
        payload.as_sync_request(),
        request,
        description=FULL_REFRESH_DESCRIPTION,
        reset=True,
    )


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def create_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request)


@router.get("/sync/jobs/{job_id}")
def sync_job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description not in {
        "行情同步",
        "全市场行情同步",
        "历史全量回填",
        FULL_REFRESH_DESCRIPTION,
    }:
        raise HTTPException(status_code=404, detail="行情同步任务不存在或已过期")
    return {"data": state.snapshot()}


def _create_sync_job(
    payload: MarketSyncRequest,
    request: Request,
    *,
    description: str = "行情同步",
    reset: bool = False,
) -> JSONResponse:
    """Create an online-only sync job.

    This route, its compatibility alias ``/sync``, backfill, and the automatic
    scheduler all operate on online providers and the local database.
    """

    runner = _runner(request)
    _reject_duplicate_market_job(runner)
    job_id_holder = [""]

    def abort_check() -> bool:
        return bool(job_id_holder[0]) and runner.is_cancel_requested(job_id_holder[0])

    config = _sync_config(payload, prefer_store_targets=False)
    # 全量回填与重刷是显式要求「把这段整个下回来」，force 是用户自己绕过闸门，都不拦。
    if not reset and not payload.full_history and not payload.force:
        already_covered = _already_covered_session(request, config)
        if already_covered is not None:
            return JSONResponse(status_code=200, content={"data": already_covered})

    def run_sync(progress: ProgressReporter) -> dict[str, object]:
        reset_codes: tuple[str, ...] = ()
        deleted_bars = 0
        if reset:
            try:
                reset_codes, deleted_bars = _reset_scope(request, payload)
            except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
                raise TaskUserError(_storage_error_message(exc)) from exc
            if abort_check():
                raise TaskCancelledError("行情全量重刷已取消，已删除的历史请重新同步补回。")
        try:
            if abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            result = _result_dict(
                _call_market_sync(
                    _service(request),
                    config,
                    progress,
                    detail_callback=lambda detail: report_progress_detail(progress, detail),
                    abort_check=abort_check,
                )
            )
            if abort_check():
                raise MarketDataCancelledError("行情同步已取消")
        except MarketDataCancelledError as exc:
            raise TaskCancelledError(str(exc)) from exc
        except OnlineProviderError as exc:
            raise TaskUserError(str(exc)) from exc
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        if abort_check():
            raise TaskCancelledError("行情同步已取消")
        # 同步只影响一部分标的，缓存也只补算那些缺失或过期的。
        result = _with_indicator_precompute(
            request,
            result,
            vipdoc_path=None,
            universe=payload.universe,
            instrument_types=payload.instrument_types,
            boards=payload.boards,
            progress_callback=progress,
            abort_check=abort_check,
            only_changed=True,
            years=payload.precompute_years,
        )
        if not reset:
            return result
        # 重刷是"删了再下"，删掉却没补回来的标的必须单独点名，否则用户只会看到
        # 总数对不上，不知道哪几只票变空了。
        empty = _empty_after_refresh(request, reset_codes)
        return {
            **result,
            "deleted_bars": deleted_bars,
            "refreshed_instruments": len(reset_codes),
            "empty_after_refresh": empty,
        }

    try:
        job_id = runner.submit_callable(
            run_sync,
            description=description,
            exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
        )
        job_id_holder[0] = job_id
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


def _reset_scope(
    request: Request,
    payload: MarketSyncRequest,
) -> tuple[tuple[str, ...], int]:
    """Drop every stored bar of the requested scope.

    删除范围按**库里**的标的算，不是按在线证券列表：退市票在线目录里已经没有，
    但它们留在库里的历史同样属于这次重刷，且不先删就补不回来（在线行写不进
    本地行占住的位置）。返回（被清空的标的代码，删除根数）。
    """

    store = _store(request)
    refs = store.list_instruments(
        market=None if payload.universe == "all" else payload.universe.upper(),
        instrument_types=payload.instrument_types,
        boards=payload.boards,
    )
    codes = tuple(dict.fromkeys(ref.code for ref in refs))
    if not codes:
        return (), 0
    # source 必填两种各删一次：不传会变成"清空整表"，那不是这个按钮的意图。
    deleted = sum(store.delete_bars(codes=list(codes), source=source) for source in _BAR_SOURCES)
    # 删除可能一行都没命中——上一次重刷删了却没补回来时库里就是空的——而那种
    # 情况下 delete_bars 不会重算覆盖，留着旧区间会让这次重下又只补首尾两段，
    # 等于什么都没下。重刷的语义就是"这些标的的覆盖先归零，再从零整段下"。
    store.reset_instrument_coverage([(ref.market, ref.code) for ref in refs])
    # 删掉的行可能正躺在指标缓存里，版本号副本不清掉下一次查询还会命中它。
    _invalidate_indicator_cache(request)
    return codes, deleted


def _empty_after_refresh(request: Request, codes: tuple[str, ...]) -> list[str]:
    """List the codes that stayed empty after the redownload (capped for display)."""

    if not codes:
        return []
    try:
        return list(_store(request).health.codes_without_bars(codes))[:_EMPTY_REPORT_LIMIT]
    except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError):
        return []


#: 上海时区：交易日历与收盘时点都按交易所当地时刻判定。
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _already_covered_session(
    request: Request,
    config: MarketSyncConfig,
) -> dict[str, object] | None:
    """本地已经覆盖本交易日时给前端的答复；还需要下载时返回 None（照常建任务）。

    判据两条，缺一不可：交易日历说今天（或最近一个该收盘的交易日）已经有行情可下，且这个
    下载范围在同步记号里被记为"已经下到了那一天、且没有失败标的"。日历判不出今天是不是交易日
    时一律放行下载——判不出来就该多下一轮，而不是少下一轮。
    """

    store = _store(request)
    fetcher = getattr(_service(request), "fetch_trading_calendar", None)
    now = datetime.now(_SHANGHAI_TZ)
    # 该刷就刷一次（发布时点前后各一次）；调度器没跑到（服务刚重启）时这里就补上那一次。
    refresh_calendar_when_stale(store, fetcher, now=now)
    session = latest_completed_session(store, now=now)
    if session is None:
        return None
    status = _market_status(request)
    if not status.data_end or date.fromisoformat(status.data_end[:10]) < session:
        # 记号之外还要看库本身：数据被清过或没落进去时，记号再新也不算覆盖。
        return None
    scope = sync_scope_fingerprint(
        universe=config.universe,
        provider=config.provider,
        instrument_types=config.instrument_types,
        boards=config.boards,
    )
    stamp = read_synced_session(store, scope)
    if stamp is None or not stamp.usable_for(session=session, scope=scope):
        return None
    if stamp.instruments != status.instrument_count:
        # 记号只能说明「那个范围跑过一轮」，管不了记号之后库里又多了标的：本地导入或
        # 刚入库的新票没有那天的数据，必须放行下载，否则它们永远补不上。
        return None
    return {
        "job_id": None,
        "status": "skipped",
        "skipped": True,
        "session": session.isoformat(),
        "instruments": stamp.instruments,
        "synced_at": stamp.finished_at.isoformat(),
        "message": f"本地已覆盖 {session.isoformat()} 收盘，无需下载",
    }


def _sync_config(payload: MarketSyncRequest, *, prefer_store_targets: bool) -> MarketSyncConfig:
    return MarketSyncConfig(
        universe=payload.universe,
        provider=payload.provider,
        bars=payload.bars,
        instrument_types=tuple(payload.instrument_types or ()),
        boards=tuple(payload.boards or ()),
        prefer_store_targets=prefer_store_targets,
        refresh_names=prefer_store_targets,
        full_history=payload.full_history,
    )


def _result_dict(
    result: InstrumentDiscoveryReport | MarketSyncReport | dict[str, object],
) -> dict[str, object]:
    return (
        result.to_dict()
        if isinstance(result, (InstrumentDiscoveryReport, MarketSyncReport))
        else result
    )


def _call_market_sync(
    service: MarketSyncService,
    config: MarketSyncConfig,
    progress: ProgressReporter,
    *,
    detail_callback: DetailCallback,
    abort_check: AbortCheck,
) -> MarketSyncReport | dict[str, object]:
    if _supports_abort_check(service.sync):
        return service.sync(
            config,
            progress,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )
    return service.sync(config, progress, detail_callback=detail_callback)


def _supports_abort_check(method: object) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "abort_check"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )
