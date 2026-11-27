"""API for the full-market per-security strategy pool."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from threading import Lock
from time import monotonic
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.backtest.trades import count_closed_trades
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.market_data.store_base import _WRITE_LOCK
from aquest.portfolio_backtest.benchmark import curve_return
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_discovery.job_backend import StrategyJobBackend
from aquest.strategy_pool.service import MarketStrategyPoolService
from aquest.strategy_pool.signal_session import resolve_signal_session
from aquest.strategy_pool.store import DuckDbStrategyPoolStore, Objective
from aquest.strategy_pool.tracking_scheduler import TRACKING_OPERATION
from aquest.strategy_pool.tracking_store import PoolTrackingStore

from ._errors import store_guard

router = APIRouter(prefix="/strategy-pool", tags=["strategy-pool"])
JOB_DESCRIPTION = "全市场策略池研究"


class PoolJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "single", "signals", "portfolio"] = "full"
    market: Literal["SH", "SZ"] | None = None
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    objective: Literal["annual_return", "sharpe"] | None = None
    automatic: bool = False
    #: 指定信号回放日（只用于手动补齐缺失的历史交易日；不填则扫最新已完成交易日）。
    as_of_date: date | None = None
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
        if self.as_of_date is not None and (self.mode != "signals" or self.automatic):
            raise ValueError("指定回放日期只支持手动信号扫描")
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


#: 任务状态轮询的进程内缓存。页面在任务运行期间每 1.5 秒问一次，每次都要为它开一条
#: 状态库只读连接，正好和扫描的写事务抢同一把跨进程锁（2026-11-05 一轮扫描期间 1,030
#: 次轮询）。缓存几秒足够让进度条照常走，跨进程的只读打开能少一大半；读库失败时退回
#: 上一份——写事务占着库的那几秒，页面只会慢一拍，不会变成 503。
_JOB_STATE_CACHE_TTL_SECONDS = 4.0
_JOB_STATE_CACHE_LIMIT = 8
_job_state_cache: dict[str, tuple[float, dict[str, object]]] = {}
_job_state_cache_lock = Lock()


def _cached_pool_job_state(job_id: str, request: Request) -> dict[str, object]:
    with _job_state_cache_lock:
        cached = _job_state_cache.get(job_id)
    if cached is not None and monotonic() - cached[0] < _JOB_STATE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        state = _pool_job_state(job_id, request)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE and cached is not None:
            return cached[1]
        raise
    with _job_state_cache_lock:
        _job_state_cache[job_id] = (monotonic(), state)
        while len(_job_state_cache) > _JOB_STATE_CACHE_LIMIT:
            oldest = min(_job_state_cache, key=lambda key: _job_state_cache[key][0])
            _job_state_cache.pop(oldest, None)
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
        "benchmark",
        "tracking",
        "tracking_status",
        "tracking_as_of_date",
        "full_period_as_of_date",
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


def _with_fresh_full_period(
    candidate: Mapping[str, object],
    fresh: Mapping[str, object],
) -> dict[str, object]:
    """列表里的全区间指标换成「回放到最新收盘」的那一份。

    规则、配置与分段指标都保持研究时的样子，只替换 ``metrics.full_period``——
    这一块每天由跟踪回放重算，表格的收益 / 年化 / 夏普 / 胜率 / 盈亏比 / 回撤 /
    持仓天数都从这里读。
    """

    metrics = candidate.get("metrics")
    merged = dict(metrics) if isinstance(metrics, Mapping) else {}
    merged["full_period"] = dict(fresh)
    return {**candidate, "metrics": merged}


def _project_benchmark_for_list(benchmark: object) -> object:
    """列表用的基准：只留表格会渲染的那一个数。

    ``label`` / ``method`` / ``caliber_version`` 对每一行都是同一段文字（2026-10-19 实测
    4439 行重复出 51 万字节，占整份响应 4.5%），口径说明在页面说明与使用手册里读得到，
    列头也写了是「全段基准」；这一列真正显示的只有 ``full_period.total_return``。缺失的
    标的照旧给「没有基准」而不是补 0。
    """

    if not isinstance(benchmark, Mapping):
        return benchmark
    full_period = benchmark.get("full_period")
    total_return = full_period.get("total_return") if isinstance(full_period, Mapping) else None
    return {"full_period": {"total_return": total_return}}


@router.get("/entries")
def list_strategy_pool_entries(
    request: Request,
    objective: str = Query(default="annual_return"),
) -> dict[str, object]:
    selected = _objective(objective)
    items = _pool_store(request).list_entries(objective=selected)
    # 列表只要摘要：candidate_json 读出来会在投影里整个丢掉，所以干脆不读、不解析。
    tracks = PoolTrackingStore(_market_store(request)).current_versions(with_candidate=False)
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
        # 全区间指标与基准：跟踪摘要里那份是「当前策略回放到最新收盘」的口径，
        # 每天跟着信号扫描刷新；没有这一块（升级前的老快照）就继续显示研究口径。
        use_fresh = False
        fresh_benchmark: Mapping[str, object] | None = None
        item["full_period_as_of_date"] = None
        if isinstance(summary, Mapping):
            benchmark_value = summary.get("benchmark")
            fresh_benchmark = benchmark_value if isinstance(benchmark_value, Mapping) else None
            fresh_value = summary.get("full_period")
            if isinstance(active, Mapping) and isinstance(fresh_value, Mapping):
                active = _with_fresh_full_period(active, fresh_value)
                item["active_candidate"] = active
                item["full_period_as_of_date"] = fresh_value.get("as_of_date")
                use_fresh = True
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
        item["benchmark"] = _project_benchmark_for_list(
            fresh_benchmark
            if use_fresh and isinstance(fresh_benchmark, Mapping)
            else item.get("benchmark")
        )
        projected_items.append(
            {
                **{key: value for key, value in item.items() if key in _LIST_ENTRY_FIELDS},
                "active_candidate": _project_candidate_for_list(active),
                # 不再单回 automatic_candidates：active_candidate 已经是「手工覆盖优先于
                # 自动最优」算完的那一份，页面切目标时会重新取列表，剩下那两处读它的地方
                # 是永远走不到的兜底。这一整块在 2026-10-19 占 11.5 MB 响应的 31.3%。
                "tracking": _project_tracking_for_list(tracking),
            }
        )

    return {"data": {"objective": selected, "items": projected_items}}


@router.post("/research-estimate")
def estimate_research_workload(payload: PoolJobRequest, request: Request) -> dict[str, object]:
    """点「增量更新策略池」之前，先给出这次真正要跑多少只标的。

    判定与任务预扫描同源（同一份研究口径指纹、同一个复用剔除函数，含弱策略到期与上线后
    衰减两道判定），所以这里算出的「待重算」就是点下去之后进度条上「需重算 N 只」那个数。
    """

    if payload.mode != "full":
        raise HTTPException(status_code=422, detail="只有全市场任务支持预先统计工作量")
    store = _market_store(request)
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        pool_store=_pool_store(request),
    )
    try:
        with store_guard("工作量统计暂时无法读取", include_runtime=False):
            return {"data": service.estimate_incremental(payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


DEFAULT_RESEARCH_BOARDS: tuple[str, ...] = ("main", "chinext", "star")
VALID_RESEARCH_BOARDS = frozenset(DEFAULT_RESEARCH_BOARDS)


class ResearchSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boards: list[Literal["main", "chinext", "star"]] = Field(min_length=1)
    exclude_st: bool = False


@router.get("/research-settings")
def get_research_settings(request: Request) -> dict[str, object]:
    """研究范围偏好；从未保存过时返回默认（三个板块全选、不过滤 ST）。"""

    with store_guard("研究范围偏好暂时无法读取", include_runtime=False):
        saved = _pool_store(request).read_research_settings()
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
    with store_guard("研究范围偏好暂时无法保存", include_runtime=False):
        saved = _pool_store(request).save_research_settings(
            boards=payload.boards, exclude_st=payload.exclude_st
        )
    return {"data": {"boards": saved["boards"], "exclude_st": saved["exclude_st"]}}


class FocusMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["SH", "SZ"]
    code: str = Field(pattern=r"^\d{6}$")
    name: str = ""


@router.get("/focus")
def get_focus(request: Request) -> dict[str, object]:
    """特别关注名单；一只都没关注过时返回空列表。"""

    with store_guard("特别关注暂时无法读取", include_runtime=False):
        items = _pool_store(request).read_focus()
    return {"data": {"items": items}}


@router.post("/focus")
def add_focus_member(payload: FocusMemberRequest, request: Request) -> dict[str, object]:
    """关注一只标的；重复关注幂等，超过上限 422。"""

    try:
        with store_guard("特别关注暂时无法保存", include_runtime=False):
            items = _pool_store(request).add_focus(payload.market, payload.code, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"items": items}}


@router.delete("/focus/{market}/{code}")
def remove_focus_member(market: str, code: str, request: Request) -> dict[str, object]:
    """取消关注；没有关注过的代码同样幂等返回当前名单。"""

    try:
        with store_guard("特别关注暂时无法保存", include_runtime=False):
            items = _pool_store(request).remove_focus(market.upper(), code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"items": items}}


class FocusGroupNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class FocusGroupMembershipRequest(FocusMemberRequest):
    group_ids: list[str]


@router.get("/focus-groups")
def get_focus_groups(request: Request) -> dict[str, object]:
    with store_guard("特别关注分组暂时无法读取", include_runtime=False):
        groups = _pool_store(request).read_focus_groups()
    return {"data": {"groups": groups}}


@router.post("/focus-groups")
def create_focus_group(payload: FocusGroupNameRequest, request: Request) -> dict[str, object]:
    try:
        with store_guard("特别关注分组暂时无法保存", include_runtime=False):
            _pool_store(request).create_focus_group(payload.name)
            groups = _pool_store(request).read_focus_groups()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"groups": groups}}


@router.put("/focus-groups/membership")
def set_focus_membership(
    payload: FocusGroupMembershipRequest, request: Request
) -> dict[str, object]:
    try:
        with store_guard("特别关注分组暂时无法保存", include_runtime=False):
            groups = _pool_store(request).set_focus_groups(
                payload.market, payload.code, payload.name, payload.group_ids
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"groups": groups}}


@router.put("/focus-groups/{group_id}")
def rename_focus_group(
    group_id: str, payload: FocusGroupNameRequest, request: Request
) -> dict[str, object]:
    try:
        with store_guard("特别关注分组暂时无法保存", include_runtime=False):
            _pool_store(request).rename_focus_group(group_id, payload.name)
            groups = _pool_store(request).read_focus_groups()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"groups": groups}}


@router.delete("/focus-groups/{group_id}")
def delete_focus_group(group_id: str, request: Request) -> dict[str, object]:
    try:
        with store_guard("特别关注分组暂时无法保存", include_runtime=False):
            _pool_store(request).delete_focus_group(group_id)
            groups = _pool_store(request).read_focus_groups()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"groups": groups}}


@router.get("/signals/gap")
def signal_gap(request: Request) -> dict[str, object]:
    """最新扫描日之前、两个目标都还没有归档的已收盘交易日。"""

    with store_guard("暂时无法读取信号缺口"):
        store = _market_store(request)
        latest = resolve_signal_session(store).scanned
        if latest is None:
            return {"data": {"days": [], "latest": None}}
        with store.state_read_session() as connection:
            last_rows = connection.execute(
                "SELECT objective, MAX(trade_date) FROM pool_simulation_signals GROUP BY objective"
            ).fetchall()
            covered_rows = connection.execute(
                "SELECT trade_date, COUNT(DISTINCT objective) FROM pool_simulation_signals"
                " GROUP BY trade_date"
            ).fetchall()
        last_days = [
            row[1] if isinstance(row[1], date) else date.fromisoformat(str(row[1])[:10])
            for row in last_rows
            if row[1] is not None
        ]
        if len(last_days) < 2:
            return {"data": {"days": [], "latest": latest.isoformat()}}
        covered_through = min(last_days)
        covered = {
            (row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])): int(
                row[1]
            )
            for row in covered_rows
        }
        days = [
            day.isoformat()
            for day in store.read_completed_trading_dates()
            if covered_through < day < latest and covered.get(day, 0) < 2
        ]
        return {
            "data": {
                "days": days,
                "latest": latest.isoformat(),
                "covered_through": covered_through.isoformat(),
            }
        }


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
        session = resolve_signal_session(_market_store(request))
        scanned = payload.as_of_date or session.scanned
        replayed = payload.as_of_date is not None
        if payload.as_of_date is not None:
            if session.scanned is None or payload.as_of_date > session.scanned:
                raise HTTPException(status_code=422, detail="回放日期晚于最新已完成行情")
            with _market_store(request).read_session() as connection:
                completed_day = connection.execute(
                    "SELECT 1 FROM daily_bars"
                    " WHERE trade_date = ? AND bar_status = 'completed' LIMIT 1",
                    [payload.as_of_date],
                ).fetchone()
            if completed_day is None:
                raise HTTPException(status_code=422, detail="该日期没有已完成的本地日线，无法回放")
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
        job_payload = payload.model_dump(exclude_none=True)
        job_payload.pop("as_of_date", None)
        job_id = backend.submit(
            TRACKING_OPERATION,
            {
                **job_payload,
                "as_of_date": scanned.isoformat() if scanned else None,
                "data_lag": session.lag,
                "replayed": replayed,
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
            with store_guard("每日扫描记录暂时无法读取", include_runtime=False):
                existing = _job_store(request).latest_pool_signal_attempt(
                    start.timestamp(), (start + timedelta(days=1)).timestamp()
                )
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
        with store_guard("策略池任务暂时无法提交", include_runtime=True):
            job_id = backend.submit(
                "market_strategy_pool",
                job_payload,
                description=JOB_DESCRIPTION,
                worker_count=1,
                metadata={"mode": payload.mode, "phase_label": "排队等待策略 worker"},
            )
        return {"data": {"job_id": job_id, "status": "queued"}}


@router.get("/jobs")
def list_strategy_pool_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    with store_guard("策略池任务历史暂不可用", include_runtime=False):
        return {
            "data": _job_store(request).list_market_strategy_pool_history(
                limit=limit, offset=offset
            )
        }


@router.get("/signal-days")
def list_pool_signal_days(request: Request) -> dict[str, object]:
    """可以回看的信号交易日：每个交易日一份（当天最后一次成功扫描），最多 10 个交易日。

    信号面板默认显示最新那一天，选这里给出的日期就能回看；结果本身由 ``/jobs/{job_id}``
    取，形状和当天那份完全一样。
    """

    with store_guard("信号交易日列表暂不可用", include_runtime=False):
        return {"data": {"items": _job_store(request).pool_signal_days()}}


@router.get("/jobs/stats")
def get_strategy_pool_job_stats(request: Request) -> dict[str, object]:
    """按模式汇总最近终态任务的耗时。

    单独一个接口而不是塞进列表：列表每页只有 20 条且几乎全是十分钟一次的信号
    扫描，平均时长必须按模式各取最近若干条终态任务，跟分页无关。必须注册在
    ``/jobs/{job_id}`` 之前，否则 stats 会被当成 job_id。
    """

    with store_guard("策略池任务统计暂不可用", include_runtime=False):
        return {"data": _job_store(request).market_pool_job_stats()}


@router.get("/jobs/{job_id}")
def get_strategy_pool_job(job_id: str, request: Request) -> dict[str, object]:
    state = _cached_pool_job_state(job_id, request)
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
    with store_guard("策略池取消请求暂不可用", include_runtime=False):
        return {"data": {"cancelled": _job_backend(request).cancel(job_id)}}


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


#: 详情页收益曲线的快捷区间：研究全区间、策略成立以来，以及最近一年 / 今年 / 两年 / 三年。
PoolCurveRange = Literal["1y", "ytd", "2y", "3y", "all", "created"]
POOL_CURVE_RANGES: tuple[PoolCurveRange, ...] = ("1y", "ytd", "2y", "3y", "all", "created")
POOL_CURVE_RANGE_LABELS = {
    "1y": "最近一年",
    "ytd": "今年以来",
    "2y": "最近两年",
    "3y": "最近三年",
    "all": "全部区间",
    "created": "成立以来",
}


def _range_start(kind: PoolCurveRange, end_day: date, baseline: str) -> str:
    """区间起点；空串表示不裁剪。``created`` 用基线日，其余从终点往前推。"""

    if kind == "all":
        return ""
    if kind == "created":
        return baseline
    if kind == "ytd":
        return date(end_day.year, 1, 1).isoformat()
    years = {"1y": 1, "2y": 2, "3y": 3}[kind]
    try:
        start = end_day.replace(year=end_day.year - years)
    except ValueError:
        # 2 月 29 日推到平年：取 2 月 28 日，别让快捷区间整只失效。
        start = end_day.replace(year=end_day.year - years, day=28)
    return start.isoformat()


#: 出场原因：规则卖出、三条风控线、以及「持有到期」的强平。
_EXIT_REASON_LABELS = {
    "rule": "规则卖出",
    "stop_loss": "止损",
    "take_profit": "止盈",
    "trailing_stop": "移动止盈",
    "max_hold": "持有到期",
}
_EXIT_REASON_ORDER = ("rule", "stop_loss", "take_profit", "trailing_stop", "max_hold")


def _window_analysis(
    points: Sequence[Mapping[str, object]], trades: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    max_losses, max_wins = _trade_streaks(trades)
    concentration, cost_total, cost_share = _concentration_and_cost(trades)
    return {
        "exposure": _exposure(points),
        "max_consecutive_losses": max_losses,
        "max_consecutive_wins": max_wins,
        "top3_profit_share": concentration,
        "cost_total": cost_total,
        "cost_share": cost_share,
        "yearly": _yearly_performance(points),
    }


def _exit_reasons(trades: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """按出场原因汇总窗口内的卖出成交：笔数与累计盈亏（净额，已扣成本）。"""

    buckets: dict[str, dict[str, float | int]] = {}
    for trade in trades:
        if str(trade.get("direction")) != "SELL" or trade.get("rejected"):
            continue
        trigger = str(trade.get("risk_trigger") or "")
        if trigger in {"stop_loss", "take_profit", "trailing_stop"}:
            key = trigger
        elif str(trade.get("reason") or "") == "持有到期":
            key = "max_hold"
        else:
            key = "rule"
        bucket = buckets.setdefault(key, {"trades": 0, "pnl": 0.0})
        bucket["trades"] = int(bucket["trades"]) + 1
        pnl = trade.get("pnl")
        if isinstance(pnl, (int, float)) and not isinstance(pnl, bool):
            bucket["pnl"] = float(bucket["pnl"]) + float(pnl)
    return [
        {
            "key": key,
            "label": _EXIT_REASON_LABELS[key],
            "trades": int(buckets[key]["trades"]),
            "pnl": float(buckets[key]["pnl"]),
        }
        for key in _EXIT_REASON_ORDER
        if key in buckets
    ]


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _exposure(points: Sequence[Mapping[str, object]]) -> float | None:
    """在场时间占比：窗口里有持仓的交易日 ÷ 全部交易日。"""

    days = [point for point in points if _number(point.get("total")) is not None]
    if not days:
        return None
    holding = sum(1 for point in days if (_number(point.get("positions_count")) or 0) > 0)
    return holding / len(days)


def _trade_streaks(trades: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    """最长连亏 / 连盈笔数；被拒的卖单不是成交，按平局处理——不打断也不延续。"""

    max_losses = max_wins = current_losses = current_wins = 0
    for trade in sorted(trades, key=lambda item: str(item.get("date") or "")):
        if str(trade.get("direction")) != "SELL" or trade.get("rejected"):
            continue
        pnl = _number(trade.get("pnl"))
        if pnl is None:
            continue
        if pnl < 0:
            current_losses += 1
            current_wins = 0
        elif pnl > 0:
            current_wins += 1
            current_losses = 0
        else:
            current_losses = current_wins = 0
        max_losses = max(max_losses, current_losses)
        max_wins = max(max_wins, current_wins)
    return max_losses, max_wins


def _concentration_and_cost(
    trades: Sequence[Mapping[str, object]],
) -> tuple[float | None, float, float | None]:
    """前 3 笔盈利占净利润的比例，以及交易成本（佣金+印花税+滑点）占毛利的比例。"""

    wins: list[float] = []
    net_pnl = 0.0
    cost = 0.0
    for trade in trades:
        if trade.get("rejected"):
            continue
        for field in ("commission", "stamp_tax", "slippage"):
            value = _number(trade.get(field))
            if value is not None:
                cost += value
        if str(trade.get("direction")) != "SELL":
            continue
        pnl = _number(trade.get("pnl"))
        if pnl is None:
            continue
        net_pnl += pnl
        if pnl > 0:
            wins.append(pnl)
    top3 = sum(sorted(wins, reverse=True)[:3])
    concentration = top3 / net_pnl if net_pnl > 0 else None
    gross = net_pnl + cost
    return concentration, cost, (cost / gross if gross > 0 else None)


def _yearly_performance(points: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """按自然年切窗口净值：年收益与年内最大回撤；首尾年份按区间实际起止日计算。"""

    by_year: dict[int, list[float]] = {}
    days_by_year: dict[int, list[str]] = {}
    for point in points:
        value = _number(point.get("total"))
        day = str(point.get("date") or "")
        if value is None or len(day) < 10:
            continue
        year = int(day[:4])
        by_year.setdefault(year, []).append(value)
        days_by_year.setdefault(year, []).append(day)
    rows: list[dict[str, object]] = []
    previous_close: float | None = None
    for year in sorted(by_year):
        totals = by_year[year]
        days = days_by_year[year]
        base = previous_close if previous_close is not None else totals[0]
        # 年内回撤从上一年收盘起算：跨年第一天下跌也是这一年的回撤，不能从当年首日重新计峰。
        peak = base
        drawdown = 0.0
        for value in totals:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        rows.append(
            {
                "year": year,
                "start_date": days[0],
                "end_date": days[-1],
                "total_return": (totals[-1] / base - 1) if base else None,
                "max_drawdown": drawdown,
            }
        )
        previous_close = totals[-1]
    return rows


def _window_stats(points: Sequence[Mapping[str, object]]) -> tuple[float | None, float | None]:
    """窗口收益与最大回撤：都用账户总净值，和曲线同源。"""

    totals: list[float] = []
    for point in points:
        value = point.get("total")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            totals.append(float(value))
    if len(totals) < 2 or not totals[0]:
        return None, None
    peak = totals[0]
    drawdown = 0.0
    for value in totals:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return totals[-1] / totals[0] - 1, drawdown


@router.get("/entries/{market}/{code}/versions/{version_id}/curve")
def pool_curve(
    market: str,
    code: str,
    version_id: str,
    request: Request,
    range_key: PoolCurveRange = Query("all", alias="range"),
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    if not snapshot:
        return {"data": {"version": version, "backtest": None}}
    cp = snapshot["checkpoint"]
    summary = snapshot["summary"]
    report = snapshot["report"]
    equity = list(cp["equity"])
    end_day = date.fromisoformat(str(equity[-1]["date"])) if equity else date.today()
    start = _range_start(range_key, end_day, summary["baseline_date"]) if equity else ""
    points = [p for p in equity if p["date"] >= start]
    initial_cash = float(version["candidate"].get("config", {}).get("initial_cash", 1000000))
    # 子区间的分母取窗口第一天的总净值：拿初始资金当分母，曲线一开局就是几倍收益。
    baseline = (
        summary["baseline_equity"]
        if range_key == "created"
        else (float(points[0]["total"]) if range_key != "all" and points else initial_cash)
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
    # 曲线标记携带当天成交摘要，不依赖下方交易表的当前页。
    events: dict[str, list[dict[str, object]]] = {}
    for trade in trades:
        events.setdefault(trade["date"], []).append(
            {
                key: trade.get(key)
                for key in ("date", "direction", "price", "size", "pnl", "reason", "signal_date")
            }
        )
    sampled = [
        dict(
            p,
            return_pct=float(p["total"]) / baseline - 1,
            trade_directions=[trade["direction"] for trade in events.get(p["date"], [])],
            trade_events=events.get(p["date"], []),
        )
        for i, p in enumerate(points)
        if i in preserve or i % step == 0
    ]
    report = snapshot["report"]
    strategy_return, window_drawdown = _window_stats(points)
    if range_key == "created":
        strategy_return = summary["created_return"]
    elif range_key == "all":
        strategy_return = report["performance"].get("total_return")
    # 六个快捷区间的收益与回撤一次算全：下面这张表要让用户一眼横向比较，点按钮时不再请求。
    window_ranges: list[dict[str, object]] = []
    for kind in POOL_CURVE_RANGES:
        window_start = _range_start(kind, end_day, summary["baseline_date"])
        window_points = [p for p in equity if p["date"] >= window_start]
        total_return, drawdown = _window_stats(window_points)
        if kind == "created":
            total_return = summary["created_return"]
        elif kind == "all":
            total_return = report["performance"].get("total_return")
        window_ranges.append(
            {
                "key": kind,
                "label": POOL_CURVE_RANGE_LABELS[kind],
                "start_date": window_points[0]["date"] if window_points else None,
                "end_date": window_points[-1]["date"] if window_points else None,
                "total_return": total_return,
                "max_drawdown": drawdown,
            }
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
            "ranges": window_ranges,
            "exits": _exit_reasons(trades),
            "analysis": _window_analysis(points, trades),
            "backtest": {
                **report,
                "equity_curve": sampled,
                "closed_trades": count_closed_trades(trades),
                "start_date": points[0]["date"] if points else report["start_date"],
                "end_date": points[-1]["date"] if points else report["end_date"],
                # 子区间按窗口给交易日数；全部/成立以来沿用整段报告的根数。
                "bars": (
                    len(points) if range_key not in {"all", "created"} else report.get("bars", 0)
                ),
                "trades": [],
                # 子区间只把收益与回撤换成窗口口径；夏普/胜率等仍是整段研究的数字，
                # 页面对子区间不展示这一排指标，避免把两段窗口混着读。
                "performance": {
                    **report["performance"],
                    "total_return": strategy_return,
                    **(
                        {"max_drawdown": window_drawdown}
                        if range_key not in {"all", "created"}
                        else {}
                    ),
                },
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
    range_key: PoolCurveRange = Query("all", alias="range"),
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    trades = snapshot["checkpoint"]["trades"] if snapshot else []
    if snapshot:
        # 成交明细跟着曲线同一个区间走：看「最近一年」时，表里就不该出现更早的成交。
        equity = snapshot["checkpoint"]["equity"]
        end_day = date.fromisoformat(str(equity[-1]["date"])) if equity else date.today()
        start = _range_start(range_key, end_day, snapshot["summary"]["baseline_date"])
        if start:
            trades = [t for t in trades if t["date"] >= start]
    if order == "desc":
        # 默认最近成交在前：详情里要看的是刚发生的买卖，不是 2015 年的第一笔。
        # 排序是稳定的，同一天的买入仍然排在卖出前面。分页按 offset 切片，
        # 所以翻转必须在这里做——前端反转 items 只会把第 2 页变成次旧的 50 笔。
        trades = sorted(trades, key=lambda trade: str(trade["date"]), reverse=True)
    return {"data": {"items": trades[offset : offset + limit], "total": len(trades)}}
