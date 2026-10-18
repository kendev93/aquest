"""API for the full-market per-security strategy pool."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from time import monotonic
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.market_data.store_base import _WRITE_LOCK
from aquest.portfolio_backtest.benchmark import curve_return
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_discovery.job_backend import StrategyJobBackend
from aquest.strategy_pool.store import DuckDbStrategyPoolStore, Objective
from aquest.strategy_pool.tracking_scheduler import TRACKING_OPERATION, latest_completed_day
from aquest.strategy_pool.tracking_store import PoolTrackingStore

router = APIRouter(prefix="/strategy-pool", tags=["strategy-pool"])
JOB_DESCRIPTION = "全市场策略池研究"


class PoolJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "single", "signals", "portfolio"] = "full"
    market: Literal["SH", "SZ"] | None = None
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    objective: Literal["annual_return", "sharpe"] | None = None
    automatic: bool = False
    rebuild: bool = False
    risk_trials: Literal[1_000, 2_000, 3_000] = 3_000
    #: 最长持仓**交易日**数，逐股研究时下发给每个候选（None = 不限制）。
    max_hold_days: int | None = Field(default=20, ge=1, le=10_000)
    reversal_multiple: float = Field(default=2.0, gt=0, le=20)
    atr_window: int = Field(default=14, ge=1, le=250)
    min_bars: int = Field(default=5, ge=1, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)
    #: 全市场任务的研究范围。None = 不限板块（兼容历史任务）；单股重跑忽略。
    #: 只决定"哪些票参与研究"，不进入研究口径指纹，改勾选不会触发存量重跑。
    boards: list[Literal["main", "chinext", "star"]] | None = Field(default=None, min_length=1)
    exclude_st: bool = False

    @model_validator(mode="after")
    def validate_mode(self) -> PoolJobRequest:
        if self.automatic and (self.mode != "signals" or self.market or self.code):
            raise ValueError("每日自动扫描只支持全市场当天信号")
        if self.rebuild and self.mode != "full":
            raise ValueError("只有全市场任务支持清空重建")
        if self.mode == "single" and (self.market is None or self.code is None):
            raise ValueError("单股重跑必须提供市场和 6 位证券代码")
        if self.mode == "full" and (self.market is not None or self.code is not None):
            raise ValueError("全市场任务不应指定单只证券")
        if self.mode in {"signals", "portfolio"} and self.objective is None:
            raise ValueError("信号扫描或组合回测必须选择年化收益或夏普目标")
        if self.mode not in {"signals", "portfolio"} and self.objective is not None:
            raise ValueError("年化/夏普目标只用于信号扫描或组合回测")
        if self.mode == "signals" and (self.market is None) != (self.code is None):
            raise ValueError("定向信号扫描必须同时提供市场和证券代码")
        if self.mode == "portfolio" and (self.market is not None or self.code is not None):
            raise ValueError("组合回测不应指定单只证券")
        return self


class PoolCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: dict[str, object]
    benchmark: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_replayable(self) -> PoolCandidateRequest:
        if not isinstance(self.candidate.get("buy_rule"), Mapping):
            raise ValueError("策略候选缺少买入规则")
        if not isinstance(self.candidate.get("sell_rule"), Mapping):
            raise ValueError("策略候选缺少卖出规则")
        return self


def _pool_store(request: Request) -> DuckDbStrategyPoolStore:
    return cast(DuckDbStrategyPoolStore, request.app.state.strategy_pool_store)


def _market_store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _job_store(request: Request) -> DuckDbStrategyJobStore:
    return cast(DuckDbStrategyJobStore, request.app.state.strategy_job_store)


def _job_backend(request: Request) -> StrategyJobBackend:
    return cast(StrategyJobBackend, request.app.state.strategy_job_backend)


def _objective(value: str) -> Objective:
    if value not in {"annual_return", "sharpe"}:
        raise HTTPException(status_code=422, detail="优化目标必须是 annual_return 或 sharpe")
    return cast(Objective, value)


def _entry_or_404(market: str, code: str, request: Request) -> dict[str, object]:
    item = _pool_store(request).get(market, code)
    if item is None:
        raise HTTPException(status_code=404, detail="策略池中找不到这只股票")
    return item


def _pool_job_state(job_id: str, request: Request) -> dict[str, object]:
    try:
        envelope = _job_store(request).read_request(job_id)
        if envelope is None or envelope.get("operation") not in {
            "market_strategy_pool",
            TRACKING_OPERATION,
        }:
            raise HTTPException(status_code=404, detail="策略池任务不存在")
        state = _job_backend(request).get(job_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="策略池任务状态暂时无法读取，请稍后自动重试",
        ) from exc
    if state is None:
        raise HTTPException(status_code=404, detail="策略池任务不存在")
    return state


#: 列表页真正读的顶层字段。逐标的的完整内容另有出处：`/detail` 给两套策略的规则全文，
#: `/versions/{id}/curve|trades` 给曲线与成交。整份候选（config 与规则树）、跟踪快照里
#: 重复带的整套 candidate、分段切分元数据都只是带宽：拿 2026-10-18 的生产数据实测，
#: 3350 行一次刷新回 32.5 MB（每行 9.7 KB），其中 94% 的字节是页面一行都没读的候选与
#: 快照；按下表白名单投影后每行 2.9 KB（3.6 倍），nginx 再压缩上线约 1.2 MB。省掉的不
#: 只是等待——这些字节要让后端序列化、让代理落成临时文件、让浏览器解析，把进程和库
#: 一起占住，策略 worker 的逐标的落库请求正是被这类长读挤到超时的。
_LIST_ENTRY_FIELDS = frozenset(
    {
        "market",
        "code",
        "name",
        "status",
        "last_error",
        "active_source",
        "active_candidate",
        "automatic_candidates",
        "benchmark",
        "tracking",
        "tracking_status",
        "tracking_as_of_date",
        "created_return",
        "post_launch_return",
        "post_launch_benchmark_return",
        "post_launch_days",
        "post_launch_has_data",
        "latest_close",
        "latest_close_date",
    }
)
#: 候选里表格会读的键（`candidate_id`、是否可回放、有没有买卖规则、绩效、质量标记）。
_LIST_CANDIDATE_FIELDS = frozenset(
    {"candidate_id", "objective", "replayable", "buy_rule", "sell_rule", "metrics", "quality"}
)
#: 跟踪摘要里已经投影成顶层字段、或持仓风控线要用的键。
_LIST_SUMMARY_FIELDS = frozenset(
    {
        "as_of_date",
        "holding",
        "created_return",
        "post_launch_return",
        "post_launch_benchmark_return",
        "post_launch_days",
        "has_post_launch_data",
    }
)

#: 「最新收盘」的缓存时长：这个数一天只变一次（收盘后同步进来才变），而一次全市场重选里
#: 页面每 1.5 秒轮询一次、完成数一变就重取列表。生产库有 1105 万行日线，同规模合成表实测
#: 这次聚合冷读 0.41 秒、热读 0.05 秒（8 线程）——单次不贵，但按轮询节奏重复它就是在和
#: 正在逐标的落库的研究请求抢同一批查询线程。
_LATEST_CLOSE_TTL_SECONDS = 60.0


def _latest_closes(request: Request) -> dict[tuple[str, str], tuple[float, str]]:
    """每只标的最后一根已完成日线的收盘价与日期，带 60 秒缓存。

    不走跟踪摘要里的字段：摘要只有"跑过全量回放"的版本才有，而列表要求每一行都给价，
    包括还没有跟踪版本的标的。价和日期是配套的，日期本身就是这条数的时点。
    """

    state = request.app.state
    cached: tuple[float, dict[tuple[str, str], tuple[float, str]]] | None = getattr(
        state, "latest_close_cache", None
    )
    now = monotonic()
    if cached is not None and now - cached[0] < _LATEST_CLOSE_TTL_SECONDS:
        return cached[1]
    try:
        closes = _market_store(request).latest_completed_closes()
    except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError):
        # 读不到行情时宁可继续用上一轮那份（日期跟着一起给，不会假装是今天的价），
        # 也没有缓存就让整列留白——不能拿别的标的的价或旧的判定日期凑数。
        return cached[1] if cached is not None else {}
    state.latest_close_cache = (now, closes)
    return closes


def _project_candidate_for_list(candidate: object) -> object:
    """列表用的候选：丢掉 config / 分段元数据，买卖规则只留「有没有」。"""

    if not isinstance(candidate, Mapping):
        return candidate
    projected = {key: value for key, value in candidate.items() if key in _LIST_CANDIDATE_FIELDS}
    for rule_key in ("buy_rule", "sell_rule"):
        # 表格只按 `buy_rule && sell_rule` 判定策略能否回放；空规则本来就不算有，
        # 所以留着键只把内容换成空对象，缺失与假值原样保留，不改变真值。
        if projected.get(rule_key):
            projected[rule_key] = {}
    return projected


def _project_tracking_for_list(tracking: object) -> object:
    """列表用的跟踪版本：持仓风控线要读的摘要留下，整份 candidate 不带。"""

    if not isinstance(tracking, Mapping):
        return tracking
    projected = {
        key: tracking[key]
        for key in ("version_id", "status", "objective", "launch_data_date", "summary")
        if key in tracking
    }
    summary = tracking.get("summary")
    if isinstance(summary, Mapping):
        projected["summary"] = {
            key: value for key, value in summary.items() if key in _LIST_SUMMARY_FIELDS
        }
    return projected


@router.get("/entries")
def list_strategy_pool_entries(
    request: Request,
    objective: str = Query(default="annual_return"),
) -> dict[str, object]:
    selected = _objective(objective)
    items = _pool_store(request).list_entries()
    tracks = PoolTrackingStore(_market_store(request)).current_versions()
    track_index = {(v["market"], v["code"], v["objective"], v["source"]): v for v in tracks}
    closes = _latest_closes(request)
    projected_items: list[dict[str, object]] = []
    for item in items:
        automatic = item.get("automatic_candidates")
        overrides = item.get("manual_overrides")
        automatic_map = automatic if isinstance(automatic, Mapping) else {}
        override_map = overrides if isinstance(overrides, Mapping) else {}
        active = override_map.get(selected) or automatic_map.get(selected)
        if isinstance(active, Mapping) and isinstance(active.get("candidate_ref"), str):
            active = automatic_map.get(active["candidate_ref"])
        item["active_candidate"] = active
        item["active_source"] = "manual" if override_map.get(selected) is not None else "automatic"
        tracking = track_index.get((item["market"], item["code"], selected, item["active_source"]))
        summary = tracking["summary"] if tracking else {}
        item["tracking"] = tracking
        item["created_at"] = tracking["created_at"] if tracking else None
        item["tracking_status"] = tracking["status"] if tracking else "unavailable"
        item["tracking_as_of_date"] = summary.get("as_of_date")
        item["created_return"] = summary.get("created_return")
        # 上线后窗口（从这一版策略的 launch_data_date 起算）单独投影：收益榜要在一次读取里
        # 同时比收益、同期买入持有基准和上线天数，靠这里给的就绪摘要，不去解压每个版本的
        # checkpoint。没有上线时间的老版本给不出窗口，has_post_launch_data 为假，不进榜。
        item["post_launch_return"] = summary.get("post_launch_return")
        item["post_launch_benchmark_return"] = summary.get("post_launch_benchmark_return")
        item["post_launch_days"] = summary.get("post_launch_days")
        item["post_launch_has_data"] = summary.get("has_post_launch_data")
        # 最新收盘：每一行都给，包括空仓、没有信号、甚至还没有跟踪版本的标的。价与日期
        # 配套，日期就是这条数的时点；风控三档线自己带着判定日期，两个日期不一致就说明
        # 跟踪落后于行情（停牌或落库失败），而不是这里算错了。
        price = closes.get((str(item["market"]), str(item["code"])))
        item["latest_close"] = price[0] if price else None
        item["latest_close_date"] = price[1] if price else None
        projected_items.append(
            {
                **{key: value for key, value in item.items() if key in _LIST_ENTRY_FIELDS},
                "active_candidate": _project_candidate_for_list(active),
                # 只回当前所选目标的自动候选：页面切换目标时会重新取列表。
                "automatic_candidates": {
                    selected: _project_candidate_for_list(automatic_map.get(selected))
                },
                "tracking": _project_tracking_for_list(tracking),
            }
        )

    return {"data": {"objective": selected, "items": projected_items}}


DEFAULT_RESEARCH_BOARDS: tuple[str, ...] = ("main", "chinext", "star")
VALID_RESEARCH_BOARDS = frozenset(DEFAULT_RESEARCH_BOARDS)


class ResearchSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boards: list[Literal["main", "chinext", "star"]] = Field(min_length=1)
    exclude_st: bool = False


@router.get("/research-settings")
def get_research_settings(request: Request) -> dict[str, object]:
    """研究范围偏好；从未保存过时返回默认（三个板块全选、不过滤 ST）。"""

    try:
        saved = _pool_store(request).read_research_settings()
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究范围偏好暂时无法读取") from exc
    if saved is None:
        return {"data": {"boards": list(DEFAULT_RESEARCH_BOARDS), "exclude_st": False}}
    raw_boards = saved["boards"]
    boards = [
        str(item)
        for item in (raw_boards if isinstance(raw_boards, list) else [])
        if str(item) in VALID_RESEARCH_BOARDS
    ]
    if not boards:
        return {"data": {"boards": list(DEFAULT_RESEARCH_BOARDS), "exclude_st": False}}
    return {"data": {"boards": boards, "exclude_st": bool(saved["exclude_st"])}}


@router.put("/research-settings")
def update_research_settings(
    payload: ResearchSettingsRequest, request: Request
) -> dict[str, object]:
    try:
        saved = _pool_store(request).save_research_settings(
            boards=payload.boards, exclude_st=payload.exclude_st
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究范围偏好暂时无法保存") from exc
    return {"data": {"boards": saved["boards"], "exclude_st": saved["exclude_st"]}}


class FocusMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["SH", "SZ"]
    code: str = Field(pattern=r"^\d{6}$")
    name: str = ""


@router.get("/focus")
def get_focus(request: Request) -> dict[str, object]:
    """特别关注名单；一只都没关注过时返回空列表。"""

    try:
        items = _pool_store(request).read_focus()
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="特别关注暂时无法读取") from exc
    return {"data": {"items": items}}


@router.post("/focus")
def add_focus_member(payload: FocusMemberRequest, request: Request) -> dict[str, object]:
    """关注一只标的；重复关注幂等，超过上限 422。"""

    try:
        items = _pool_store(request).add_focus(payload.market, payload.code, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="特别关注暂时无法保存") from exc
    return {"data": {"items": items}}


@router.delete("/focus/{market}/{code}")
def remove_focus_member(market: str, code: str, request: Request) -> dict[str, object]:
    """取消关注；没有关注过的代码同样幂等返回当前名单。"""

    try:
        items = _pool_store(request).remove_focus(market.upper(), code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="特别关注暂时无法保存") from exc
    return {"data": {"items": items}}


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_pool_job(payload: PoolJobRequest, request: Request) -> dict[str, object]:
    backend = _job_backend(request)
    if payload.mode == "single":
        try:
            ref = request.app.state.market_data_adapter.stock_ref(
                "", cast(Literal["SH", "SZ"], payload.market), cast(str, payload.code)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if getattr(ref, "instrument_type", "stock") != "stock":
            raise HTTPException(status_code=422, detail="策略池只支持 A 股股票")
        with _market_store(request).read_session() as connection:
            completed = connection.execute(
                """
                SELECT 1 FROM daily_bars
                WHERE market = ? AND code = ? AND instrument_type = 'stock'
                  AND bar_status = 'completed'
                LIMIT 1
                """,
                [payload.market, payload.code],
            ).fetchone()
        if completed is None:
            raise HTTPException(status_code=422, detail="这只股票没有已完成的本地日线")
    elif payload.mode == "full":
        refs = request.app.state.market_data_adapter.list_stock_refs(
            "", "all", instrument_types=("stock",)
        )
        if not refs:
            raise HTTPException(status_code=422, detail="本地行情没有可研究的 A 股日线")
        with _market_store(request).read_session() as connection:
            completed_count = connection.execute(
                """
                SELECT COUNT(DISTINCT market || ':' || code)
                FROM daily_bars
                WHERE instrument_type = 'stock' AND bar_status = 'completed'
                """
            ).fetchone()
        if completed_count is None or int(completed_count[0] or 0) == 0:
            raise HTTPException(status_code=422, detail="本地行情没有已完成的 A 股日线")
    else:
        if not _pool_store(request).list_entries():
            raise HTTPException(
                status_code=422, detail="策略池还没有完成研究，暂时没有可扫描的策略"
            )
    if payload.mode == "signals":
        if payload.automatic:
            return {"data": {"job_id": "", "status": "completed", "skipped": True}}
        active_tracking = [
            j for j in backend.active_snapshots() if j.get("operation") == TRACKING_OPERATION
        ]
        if active_tracking:
            raise HTTPException(status_code=409, detail="已有策略池跟踪任务正在运行")
        target_ids = None
        if payload.market and payload.code:
            target_ids = [
                v["version_id"]
                for v in PoolTrackingStore(_market_store(request)).current_versions()
                if v["market"] == payload.market and v["code"] == payload.code
            ]
        job_id = backend.submit(
            TRACKING_OPERATION,
            {
                **payload.model_dump(exclude_none=True),
                "as_of_date": latest_completed_day(_market_store(request)),
                "version_ids": target_ids,
            },
            description="策略池双目标跟踪",
            worker_count=1,
            metadata={"mode": "signals", "phase_label": "排队等待跟踪通道"},
        )
        return {"data": {"job_id": job_id, "status": "queued"}}
    # Serialize the durable daily check and submission, including simultaneous page entries.
    with _WRITE_LOCK:
        if payload.automatic:
            current = datetime.now(ZoneInfo("Asia/Shanghai"))
            start = datetime.combine(current.date(), time.min, tzinfo=current.tzinfo)
            try:
                existing = _job_store(request).latest_pool_signal_attempt(
                    start.timestamp(), (start + timedelta(days=1)).timestamp()
                )
            except (MarketDataStoreError, OSError) as exc:
                raise HTTPException(status_code=503, detail="每日扫描记录暂时无法读取") from exc
            if existing is not None:
                return {"data": {**existing, "skipped": True}}
        active = [
            item
            for item in backend.active_snapshots()
            if item.get("operation") == "market_strategy_pool"
        ]
        active_metadata = active[0].get("metadata") if len(active) == 1 else None
        can_queue_single_after_signal_scan = (
            payload.mode == "single"
            and isinstance(active_metadata, Mapping)
            and active_metadata.get("mode") == "signals"
        )
        if active and not can_queue_single_after_signal_scan:
            raise HTTPException(status_code=409, detail="已有全市场策略池任务正在运行")
        job_payload = payload.model_dump(exclude_none=True)
        try:
            job_id = backend.submit(
                "market_strategy_pool",
                job_payload,
                description=JOB_DESCRIPTION,
                worker_count=1,
                metadata={"mode": payload.mode, "phase_label": "排队等待策略 worker"},
            )
        except (MarketDataStoreError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="策略池任务暂时无法提交") from exc
        return {"data": {"job_id": job_id, "status": "queued"}}


@router.get("/jobs")
def list_strategy_pool_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    try:
        return {
            "data": _job_store(request).list_market_strategy_pool_history(
                limit=limit, offset=offset
            )
        }
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略池任务历史暂不可用") from exc


@router.get("/jobs/stats")
def get_strategy_pool_job_stats(request: Request) -> dict[str, object]:
    """按模式汇总最近终态任务的耗时。

    单独一个接口而不是塞进列表：列表每页只有 20 条且几乎全是十分钟一次的信号
    扫描，平均时长必须按模式各取最近若干条终态任务，跟分页无关。必须注册在
    ``/jobs/{job_id}`` 之前，否则 stats 会被当成 job_id。
    """

    try:
        return {"data": _job_store(request).market_pool_job_stats()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略池任务统计暂不可用") from exc


@router.get("/jobs/{job_id}")
def get_strategy_pool_job(job_id: str, request: Request) -> dict[str, object]:
    state = _pool_job_state(job_id, request)
    try:
        result = (
            _job_backend(request).result(job_id) if state.get("status") == "completed" else None
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="策略池任务结果暂时无法读取，请稍后自动重试",
        ) from exc
    return {"data": {**state, "result": result}}


@router.post("/jobs/{job_id}/cancel")
def cancel_strategy_pool_job(job_id: str, request: Request) -> dict[str, object]:
    _pool_job_state(job_id, request)
    try:
        return {"data": {"cancelled": _job_backend(request).cancel(job_id)}}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略池取消请求暂不可用") from exc


@router.post("/entries/{market}/{code}/overrides/{objective}")
def update_strategy_pool_override(
    market: str,
    code: str,
    objective: str,
    payload: PoolCandidateRequest,
    request: Request,
) -> dict[str, object]:
    selected = _objective(objective)
    try:
        _pool_store(request).set_manual_override(
            market.upper(),
            code,
            selected,
            payload.candidate,
            benchmark=payload.benchmark,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": _entry_or_404(market.upper(), code, request)}


@router.delete("/entries/{market}/{code}/overrides/{objective}")
def clear_strategy_pool_override(
    market: str,
    code: str,
    objective: str,
    request: Request,
) -> dict[str, object]:
    selected = _objective(objective)
    _entry_or_404(market.upper(), code, request)
    _pool_store(request).clear_manual_override(market.upper(), code, selected)
    return {"data": _entry_or_404(market.upper(), code, request)}


__all__ = ["router"]


@router.get("/entries/{market}/{code}/detail")
def pool_detail(market: str, code: str, request: Request) -> dict[str, object]:
    item = _entry_or_404(market, code, request)
    tracks = PoolTrackingStore(_market_store(request))
    return {
        "data": {
            "market": market,
            "code": code,
            "name": item["name"],
            "objectives": {
                goal: tracks.versions(market, code, goal) for goal in ("annual_return", "sharpe")
            },
            "manual_overrides": item["manual_overrides"],
        }
    }


def _owned_version(
    market: str, code: str, version_id: str, request: Request
) -> tuple[PoolTrackingStore, dict[str, Any]]:
    tracks = PoolTrackingStore(_market_store(request))
    versions = [
        v for goal in ("annual_return", "sharpe") for v in tracks.versions(market, code, goal)
    ]
    version = next((v for v in versions if v["version_id"] == version_id), None)
    if version is None:
        raise HTTPException(status_code=404, detail="跟踪版本不存在")
    return tracks, version


@router.get("/entries/{market}/{code}/versions/{version_id}/curve")
def pool_curve(
    market: str,
    code: str,
    version_id: str,
    request: Request,
    scope: Literal["created", "full"] = "created",
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    if not snapshot:
        return {"data": {"version": version, "backtest": None}}
    cp = snapshot["checkpoint"]
    summary = snapshot["summary"]
    start = summary["baseline_date"] if scope == "created" else ""
    points = [p for p in cp["equity"] if p["date"] >= start]
    baseline = (
        summary["baseline_equity"]
        if scope == "created"
        else float(version["candidate"].get("config", {}).get("initial_cash", 1000000))
    )
    trades = [t for t in cp["trades"] if t["date"] >= start]
    preserve = {0, len(points) - 1}
    event_dates = {t["date"] for t in trades}
    launch_day = (
        datetime.fromisoformat(version["created_at"])
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    launch_index = next((j for j, p in enumerate(points) if p["date"] >= launch_day), len(points))
    for i, point in enumerate(points):
        if point["date"] in event_dates or abs(i - launch_index) <= 1:
            preserve.add(i)
    step = max(1, len(points) // 240)
    directions: dict[str, list[str]] = {}
    for trade in trades:
        directions.setdefault(trade["date"], []).append(trade["direction"])
    sampled = [
        dict(
            p,
            return_pct=float(p["total"]) / baseline - 1,
            trade_directions=directions.get(p["date"], []),
        )
        for i, p in enumerate(points)
        if i in preserve or i % step == 0
    ]
    report = snapshot["report"]
    strategy_return = (
        summary["created_return"]
        if scope == "created"
        else report["performance"].get("total_return")
    )
    # 基准按**同一窗口**重算：full 是整段研究，created 从账户基线日起算。直接取全段
    # performance.total_return 会把基线日之前的涨跌算进「创建以来」，与策略收益口径不
    # 一致；这两个值此前根本没进 payload，所以详情页的持有基准与超额收益一直是空的。
    benchmark_curve = report.get("benchmark")
    benchmark_return = curve_return(benchmark_curve, start)
    excess_return = (
        float(strategy_return) - benchmark_return
        if isinstance(strategy_return, (int, float)) and benchmark_return is not None
        else None
    )
    return {
        "data": {
            "version": version,
            "summary": summary,
            "backtest": {
                **report,
                "equity_curve": sampled,
                "closed_trades": sum(
                    t["direction"] == "SELL" and not t.get("rejected") for t in trades
                ),
                "start_date": points[0]["date"] if points else report["start_date"],
                "trades": [],
                "performance": {**report["performance"], "total_return": strategy_return},
                "benchmark": (
                    {**benchmark_curve, "total_return": benchmark_return}
                    if isinstance(benchmark_curve, Mapping)
                    else None
                ),
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "launch_date": launch_day,
                "baseline_date": summary["baseline_date"],
            },
        }
    }


@router.get("/entries/{market}/{code}/versions/{version_id}/trades")
def pool_trades(
    market: str,
    code: str,
    version_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    scope: Literal["created", "full"] = "created",
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    trades = snapshot["checkpoint"]["trades"] if snapshot else []
    if snapshot and scope == "created":
        trades = [t for t in trades if t["date"] >= snapshot["summary"]["baseline_date"]]
    if order == "desc":
        # 默认最近成交在前：详情里要看的是刚发生的买卖，不是 2015 年的第一笔。
        # 排序是稳定的，同一天的买入仍然排在卖出前面。分页按 offset 切片，
        # 所以翻转必须在这里做——前端反转 items 只会把第 2 页变成次旧的 50 笔。
        trades = sorted(trades, key=lambda trade: str(trade["date"]), reverse=True)
    return {"data": {"items": trades[offset : offset + limit], "total": len(trades)}}
