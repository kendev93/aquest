"""候选库：入库带走的是证据快照，发布写下去的是一条**离线**策略。

单独盯的护栏：① 发布绝不上线（tracking_status 必须是 off，且没有 launched_at）；
② pending 不能直接发布；③ 撤回已上线的策略要被拒；④ 入库幂等；⑤ 来源研究被
历史淘汰后，入库与发布都要明说而不是静默失败。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import duckdb
import pytest

from aquest.candidate_library.models import (
    STATUS_ADOPTED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_REJECTED,
    can_transition,
)
from aquest.candidate_library.publish import build_publish_plan
from aquest.candidate_library.service import (
    CandidateLibraryError,
    CandidateLibraryService,
)
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import SCHEMA_VERSION
from aquest.market_data.store_history import STRATEGY_TRACKING_ACTIVE

_CONDITION = {"left_value": "technical.bias20", "operator": "lte", "right_constant": -5.0}
_OTHER_CONDITION = {"left_value": "technical.rsi14", "operator": "lte", "right_constant": 30.0}

_RESEARCH_REQUEST: dict[str, Any] = {
    "universe": "all",
    "boards": ["main"],
    "instrument_types": ["stock"],
    "buy_rule": {
        "signals": [],
        "conditions": [dict(_OTHER_CONDITION)],
        "required_conditions": [],
        "combine_mode": "all",
        "minimum_matches": None,
    },
    "sell_rule": None,
    "date_mode": "custom",
    "start_date": "2024-01-02",
    "end_date": "2026-09-10",
    "initial_cash": 100_000.0,
    "commission": 0.0001,
    "min_commission": 0.1,
    "stamp_tax": 0.001,
    "slippage": 0.0,
    "execution": "next_open",
    "position_mode": "full",
    "fixed_size": None,
    "ranking_value": "technical.rsrs_zscore",
    "rank_order": "desc",
    "ranking_top_n": 5,
    "min_trades": 20,
    "significance_permutations": 200,
    "formula_text": None,
    "formula_parameters": {},
}


def _candidate(candidate_id: str = "buy-1", *, side: str = "buy") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "side": side,
        "condition": dict(_CONDITION),
        "mining": {
            "lift": 1.8,
            "support_count": 320,
            "instrument_count": 120,
            "outcome_rate": 0.62,
            "baseline_rate": 0.5,
            "support_ratio": 0.08,
            "permutation_pvalue": 0.02,
            "significant": True,
            "lift_threshold": 1.5,
            "top_n_outcome_rate": 0.6,
            "top_n_filtered_days": 40,
        },
        "sensitivity": {"median_retention": 0.7, "worst_retention": 0.4},
        "validation": {
            "pooled_trades": {"trade_count": 320, "win_rate": 0.62},
            "improved_win_rate": True,
            "per_stock": {"improved_stock_ratio": 0.7},
        },
        "holdout": {
            "pooled_trades": {"trade_count": 90, "win_rate": 0.58},
            "improved_win_rate": True,
            "per_stock": {},
        },
        "consistency": {
            "requested": 3,
            "used": 3,
            "evaluated": 3,
            "better_than_baseline": 2,
            "note": None,
        },
        "passed_constraints": True,
        "constraint_failures": [],
    }


def _history(
    history_id: str = "study-1",
    *,
    request: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    backtest_type: str = "condition_research",
) -> dict[str, Any]:
    return {
        "history_id": history_id,
        "backtest_type": backtest_type,
        "created_at": "2026-09-20 10:00:00",
        "request": dict(request if request is not None else _RESEARCH_REQUEST),
        "result": {
            "candidates": [_candidate()] if candidates is None else candidates,
            "processed": 5045,
        },
    }


class FakeStore:
    """内存版 store：service 的编排逻辑不需要真 duckdb 来验。"""

    def __init__(self, histories: list[dict[str, Any]] | None = None) -> None:
        self.histories = {item["history_id"]: item for item in (histories or [])}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.strategies: dict[str, dict[str, Any]] = {}

    # -- 历史 ----------------------------------------------------------
    def read_backtest_history(self, history_id: str) -> dict[str, Any] | None:
        return self.histories.get(str(history_id).strip())

    # -- 候选 ----------------------------------------------------------
    def find_candidate_by_source(
        self, source_history_id: str, source_candidate_id: str
    ) -> dict[str, Any] | None:
        for record in self.candidates.values():
            if (
                record["source_history_id"] == str(source_history_id).strip()
                and record["source_candidate_id"] == str(source_candidate_id).strip()
            ):
                return dict(record)
        return None

    def save_candidate(self, candidate_id: str, **fields: Any) -> dict[str, Any]:
        record = {"candidate_id": candidate_id, **fields}
        self.candidates[candidate_id] = record
        return dict(record)

    def list_candidates(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.candidates.values())
        if status:
            rows = [row for row in rows if row["status"] == status]
        return [dict(row) for row in rows]

    def read_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        record = self.candidates.get(str(candidate_id).strip())
        return dict(record) if record else None

    def update_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        note: str | None = None,
        published_strategy_id: str | None = None,
        publish: bool = False,
    ) -> dict[str, Any] | None:
        record = self.candidates.get(str(candidate_id).strip())
        if record is None:
            return None
        if status is not None:
            record["status"] = status
        if note is not None:
            record["note"] = note
        if publish:
            record["published_strategy_id"] = published_strategy_id
        return dict(record)

    def delete_candidate(self, candidate_id: str) -> bool:
        return self.candidates.pop(str(candidate_id).strip(), None) is not None

    # -- 策略 ----------------------------------------------------------
    def save_strategy(
        self,
        strategy_id: str,
        *,
        name: str,
        request: dict[str, Any],
        summary: dict[str, Any],
        note: str = "",
        source_history_id: str | None = None,
        created_at: Any = None,
    ) -> dict[str, Any]:
        record = {
            "strategy_id": strategy_id,
            "name": name,
            "note": note,
            "request": dict(request),
            "summary": dict(summary),
            "source_history_id": source_history_id,
            "tracking_status": "off",
            "launched_at": None,
        }
        self.strategies[strategy_id] = record
        return dict(record)

    def publish_candidate_strategy(
        self,
        candidate_id: str,
        *,
        expected_updated_at: object,
        strategy_id: str,
        name: str,
        note: str,
        request: dict[str, Any],
        summary: dict[str, Any],
        source_history_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        candidate = self.candidates.get(candidate_id)
        if (
            candidate is None
            or candidate.get("status") != STATUS_ADOPTED
            or candidate.get("updated_at") != expected_updated_at
        ):
            from aquest.market_data.store_history_candidates import CandidatePublishConflict

            raise CandidatePublishConflict("候选状态或内容已变化，请刷新后重新发布")
        strategy = self.save_strategy(
            strategy_id,
            name=name,
            note=note,
            request=request,
            summary=summary,
            source_history_id=source_history_id,
        )
        candidate = self.update_candidate(
            candidate_id,
            status=STATUS_PUBLISHED,
            published_strategy_id=strategy_id,
            publish=True,
        )
        assert candidate is not None
        return candidate, strategy

    def read_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        record = self.strategies.get(str(strategy_id).strip())
        return dict(record) if record else None

    def delete_strategy(self, strategy_id: str) -> bool:
        return self.strategies.pop(str(strategy_id).strip(), None) is not None


def _service(store: FakeStore) -> CandidateLibraryService:
    return CandidateLibraryService(store)


#: 默认的研究请求里 sell_rule 是 None（研究用「持有到期末」当卖出基准），所以发布
#: 买入候选时必须自己给一个离场规则——这条约束本身有单独的测试盯着。
def _publish(
    service: CandidateLibraryService,
    candidate_id: str,
    name: str = "甲策略",
    **overrides: Any,
) -> dict[str, Any]:
    return service.publish(candidate_id, name=name, overrides={"stop_loss_pct": 0.1, **overrides})


def _imported(
    store: FakeStore | None = None, **history_kwargs: Any
) -> tuple[CandidateLibraryService, dict[str, Any]]:
    target = store or FakeStore([_history(**history_kwargs)])
    service = _service(target)
    outcome = service.import_candidates(history_id="study-1", candidate_ids=["buy-1"])
    return service, outcome["added"][0]


# ----------------------------------------------------------------------
# 状态机
# ----------------------------------------------------------------------


def test_pending_cannot_be_published_directly() -> None:
    assert not can_transition(STATUS_PENDING, STATUS_PUBLISHED)
    assert can_transition(STATUS_PENDING, STATUS_ADOPTED)
    assert can_transition(STATUS_ADOPTED, STATUS_PUBLISHED)
    assert can_transition(STATUS_PUBLISHED, STATUS_ADOPTED)
    assert not can_transition(STATUS_REJECTED, STATUS_ADOPTED)


def test_publishing_a_pending_candidate_is_refused() -> None:
    service, candidate = _imported()

    with pytest.raises(CandidateLibraryError, match="先采纳"):
        _publish(service, str(candidate["candidate_id"]))


def test_rejected_candidate_must_be_reopened_before_anything_else() -> None:
    service, candidate = _imported()
    service.set_status(str(candidate["candidate_id"]), status=STATUS_REJECTED, note="样本太薄")

    with pytest.raises(CandidateLibraryError, match="已被否决"):
        service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    reopened = service.set_status(str(candidate["candidate_id"]), status=STATUS_PENDING)
    assert reopened["status"] == STATUS_PENDING


# ----------------------------------------------------------------------
# 入库
# ----------------------------------------------------------------------


def test_import_carries_the_evidence_snapshot() -> None:
    _, candidate = _imported()

    evidence = candidate["evidence"]
    assert evidence["lift"] == 1.8
    assert evidence["permutation_pvalue"] == 0.02
    assert evidence["significant"] is True
    assert evidence["support_count"] == 320
    assert evidence["holdout"]["win_rate"] == 0.58
    assert evidence["consistency"]["better_than_baseline"] == 2
    # 溯源：证据是快照，但必须能追回它来自哪次研究、什么区间、扫了多少只。
    assert evidence["study"]["start_date"] == "2024-01-02"
    assert evidence["study"]["end_date"] == "2026-09-10"
    assert evidence["study"]["scanned_stocks"] == 5045
    assert candidate["status"] == STATUS_PENDING
    assert candidate["label"]


def test_import_is_idempotent() -> None:
    service, _ = _imported()

    outcome = service.import_candidates(history_id="study-1", candidate_ids=["buy-1"])

    assert outcome["added"] == []
    assert outcome["skipped"][0]["reason"] == "已经在候选库里了"


def test_import_reports_unknown_candidate_ids() -> None:
    service = _service(FakeStore([_history()]))

    outcome = service.import_candidates(history_id="study-1", candidate_ids=["nope"])

    assert outcome["added"] == []
    assert "没有这条候选" in outcome["skipped"][0]["reason"]


def test_import_refuses_when_the_study_has_been_evicted() -> None:
    """历史只留最近 20 条：来源研究没了就是没了，不能静默建一条没有证据的候选。"""

    service = _service(FakeStore([]))

    with pytest.raises(CandidateLibraryError, match="不在历史列表"):
        service.import_candidates(history_id="gone", candidate_ids=["buy-1"])


def test_import_refuses_other_backtest_types() -> None:
    service = _service(FakeStore([_history(backtest_type="factor_test")]))

    with pytest.raises(CandidateLibraryError, match="只有买卖条件研究"):
        service.import_candidates(history_id="study-1", candidate_ids=["buy-1"])


def test_import_requires_a_selection() -> None:
    service = _service(FakeStore([_history()]))

    with pytest.raises(CandidateLibraryError, match="请先选择"):
        service.import_candidates(history_id="study-1", candidate_ids=[])


# ----------------------------------------------------------------------
# 发布：永不自动上线
# ----------------------------------------------------------------------


def test_publish_writes_an_offline_strategy() -> None:
    store = FakeStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = _publish(service, str(candidate["candidate_id"]))

    strategy = outcome["strategy"]
    # 这三行就是「永不自动上线」的可断言形式。
    assert strategy["tracking_status"] == "off"
    assert strategy["launched_at"] is None
    assert outcome["online"] is False
    assert "未上线" in outcome["message"]
    assert outcome["candidate"]["status"] == STATUS_PUBLISHED
    assert outcome["candidate"]["published_strategy_id"] == strategy["strategy_id"]


def test_published_request_carries_the_condition_on_its_own_side() -> None:
    store = FakeStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = _publish(service, str(candidate["candidate_id"]))
    request = outcome["strategy"]["request"]

    conditions = request["buy_rule"]["conditions"]
    assert [_condition_key(item) for item in conditions] == [
        _condition_key(_OTHER_CONDITION),
        _condition_key(_CONDITION),
    ]
    # 买入保持来源研究的组合方式（收窄入场），不擅自改成 any。
    assert request["buy_rule"]["combine_mode"] == "all"
    assert request["ranking_value"] == "technical.rsrs_zscore"
    assert request["max_positions"] == 5
    assert request["start_date"] == "2024-01-02"


def test_publish_merges_sell_candidates_with_or_semantics() -> None:
    history = _history(
        candidates=[_candidate("sell-1", side="sell")],
        request={
            **_RESEARCH_REQUEST,
            "sell_rule": {
                "signals": [],
                "conditions": [dict(_OTHER_CONDITION)],
                "required_conditions": [],
                "combine_mode": "all",
                "minimum_matches": None,
            },
        },
    )
    store = FakeStore([history])
    service = _service(store)
    candidate = service.import_candidates(history_id="study-1", candidate_ids=["sell-1"])["added"][
        0
    ]
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = service.publish(str(candidate["candidate_id"]), name="乙策略")

    sell_rule = outcome["strategy"]["request"]["sell_rule"]
    assert len(sell_rule["conditions"]) == 2
    # 卖出是「多一个离场触发」，不是要求同时满足。
    assert sell_rule["combine_mode"] == "any"
    assert any("任一满足" in note for note in outcome["notes"])


def test_publish_replaces_an_absent_rule_instead_of_dropping_the_condition() -> None:
    """来源研究当初是「持有到期末/持续满仓」时，那条条件只能直接成为规则。"""

    history = _history(candidates=[_candidate("sell-1", side="sell")])
    service = _service(FakeStore([history]))
    candidate = service.import_candidates(history_id="study-1", candidate_ids=["sell-1"])["added"][
        0
    ]
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = _publish(service, str(candidate["candidate_id"]), "丙策略")

    # 校验器会把 right_value 补成 None，所以比条件本身而不是整份字典。
    assert [
        _condition_key(item) for item in outcome["strategy"]["request"]["sell_rule"]["conditions"]
    ] == [_condition_key(_CONDITION)]
    assert any("持有到期末" in note for note in outcome["notes"])


def test_publish_does_not_add_a_condition_twice() -> None:
    history = _history(
        candidates=[_candidate()],
        request={
            **_RESEARCH_REQUEST,
            "buy_rule": {
                "signals": [],
                "conditions": [dict(_CONDITION)],
                "required_conditions": [],
                "combine_mode": "all",
                "minimum_matches": None,
            },
        },
    )
    service = _service(FakeStore([history]))
    candidate = service.import_candidates(history_id="study-1", candidate_ids=["buy-1"])["added"][0]
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = _publish(service, str(candidate["candidate_id"]), "丁策略")

    assert [
        _condition_key(item) for item in outcome["strategy"]["request"]["buy_rule"]["conditions"]
    ] == [_condition_key(_CONDITION)]
    assert any("不再重复添加" in note for note in outcome["notes"])


def test_publish_blocks_when_the_study_had_no_ranking_rule() -> None:
    """猜不出来就报错让人补，不悄悄填一个默认值。"""

    history = _history(request={**_RESEARCH_REQUEST, "ranking_value": None})
    service = _service(FakeStore([history]))
    candidate = service.import_candidates(history_id="study-1", candidate_ids=["buy-1"])["added"][0]
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    preview = service.publish_preview(str(candidate["candidate_id"]))
    assert preview["blocked"] is True
    assert preview["ready"] is False
    assert any("排序规则" in item for item in preview["blockers"])

    with pytest.raises(CandidateLibraryError, match="排序规则"):
        _publish(service, str(candidate["candidate_id"]))

    outcome = _publish(
        service,
        str(candidate["candidate_id"]),
        ranking_value="technical.rsrs_zscore",
    )
    assert outcome["strategy"]["request"]["ranking_value"] == "technical.rsrs_zscore"


def test_publish_refuses_an_empty_name() -> None:
    service, candidate = _imported()
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    with pytest.raises(CandidateLibraryError, match="起个名字"):
        service.publish(str(candidate["candidate_id"]), name="  ")


def test_publish_refuses_when_the_source_study_is_gone() -> None:
    store = FakeStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)
    store.histories.clear()

    with pytest.raises(CandidateLibraryError, match="不在历史列表"):
        _publish(service, str(candidate["candidate_id"]))


def test_published_strategy_summary_has_no_performance() -> None:
    """发布不跑回测，写 0 进去会看起来像"这个策略一分钱没赚"。"""

    service, candidate = _imported()
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    outcome = _publish(service, str(candidate["candidate_id"]))

    assert "performance" not in outcome["strategy"]["summary"]
    assert outcome["strategy"]["summary"]["universe"] == "all"


def test_publish_blocks_when_the_study_had_no_exit_rule() -> None:
    """研究可以拿「持有到期末」当卖出基准，策略不行：不设离场等于只买不卖。

    这条拦着而不是替人填一个止损比例——离场点怎么定本身就是策略的一部分。
    """

    service, candidate = _imported()
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    preview = service.publish_preview(str(candidate["candidate_id"]))
    assert preview["blocked"] is True
    assert any("离场" in item for item in preview["blockers"])

    outcome = _publish(service, str(candidate["candidate_id"]))
    assert outcome["strategy"]["request"]["stop_loss_pct"] == 0.1


# ----------------------------------------------------------------------
# 撤回与删除
# ----------------------------------------------------------------------


def test_withdrawing_a_publication_removes_the_strategy_it_created() -> None:
    store = FakeStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)
    outcome = _publish(service, str(candidate["candidate_id"]))

    withdrawn = service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    assert withdrawn["status"] == STATUS_ADOPTED
    assert withdrawn["published_strategy_id"] is None
    assert store.read_strategy(str(outcome["strategy"]["strategy_id"])) is None


def test_withdrawing_an_online_strategy_is_refused() -> None:
    store = FakeStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)
    outcome = _publish(service, str(candidate["candidate_id"]))
    store.strategies[str(outcome["strategy"]["strategy_id"])]["tracking_status"] = (
        STRATEGY_TRACKING_ACTIVE
    )

    with pytest.raises(CandidateLibraryError, match="已上线"):
        service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)

    assert store.read_strategy(str(outcome["strategy"]["strategy_id"])) is not None


def test_deleting_a_published_candidate_is_refused() -> None:
    service, candidate = _imported()
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)
    _publish(service, str(candidate["candidate_id"]))

    with pytest.raises(CandidateLibraryError, match="先撤回"):
        service.delete_candidate(str(candidate["candidate_id"]))


def test_deleting_an_unpublished_candidate_works() -> None:
    service, candidate = _imported()

    assert service.delete_candidate(str(candidate["candidate_id"])) is True
    assert service.read_candidate(str(candidate["candidate_id"])) is None


# ----------------------------------------------------------------------
# 配置拼装（publish plan 本身）
# ----------------------------------------------------------------------


def test_build_publish_plan_keeps_the_study_cost_settings() -> None:
    plan = build_publish_plan(
        research_request=_RESEARCH_REQUEST,
        condition=_CONDITION,
        side="buy",
        overrides={"stop_loss_pct": 0.1},
    )

    assert plan.request["commission"] == 0.0001
    assert plan.request["stamp_tax"] == 0.001
    assert plan.request["initial_cash"] == 100_000.0
    assert not plan.blockers


def test_missing_source_study_is_reported_not_guessed() -> None:
    service = _service(FakeStore([]))

    with pytest.raises(CandidateLibraryError, match="候选不存在"):
        service.publish_preview("nope")


# ----------------------------------------------------------------------
# 存储：真实 duckdb 与迁移
# ----------------------------------------------------------------------


def test_legacy_v16_database_gains_the_candidate_table(tmp_path) -> None:
    """新表靠 _initialize 的 CREATE TABLE IF NOT EXISTS 补建：旧库打开就得有。"""

    path = tmp_path / "market.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS data_meta ("
            "key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL)"
        )
        connection.execute(
            "INSERT INTO data_meta (key, value, updated_at) VALUES ('schema_version', '16', ?)",
            ["2026-09-01 09:00:00"],
        )

    store = DuckDbMarketDataStore(path)
    record = store.save_candidate(
        uuid.uuid4().hex,
        source="condition_research",
        source_history_id="study-1",
        source_candidate_id="buy-1",
        side="buy",
        label="乖离率20 ≤ -5",
        condition=dict(_CONDITION),
        evidence={"lift": 1.8},
        status=STATUS_PENDING,
    )

    assert store.status().schema_version == SCHEMA_VERSION
    assert store.read_candidate(str(record["candidate_id"])) is not None
    assert store.list_candidates("pending")[0]["source_candidate_id"] == "buy-1"


def test_duplicate_source_is_rejected_by_the_store(tmp_path) -> None:
    """同一研究的同一候选在库里只能有一条——重复入库不该靠应用层自觉。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    arguments = {
        "source": "condition_research",
        "source_history_id": "study-1",
        "source_candidate_id": "buy-1",
        "side": "buy",
        "label": "乖离率20 ≤ -5",
        "condition": dict(_CONDITION),
        "evidence": {"lift": 1.8},
        "status": STATUS_PENDING,
    }
    store.save_candidate("first", **arguments)

    with pytest.raises(Exception):
        store.save_candidate("second", **arguments)


def _real_adopted_candidate(store: DuckDbMarketDataStore, candidate_id: str = "candidate"):
    store.save_backtest_history(
        "study-1",
        backtest_type="condition_research",
        request=_RESEARCH_REQUEST,
        result={"candidates": [_candidate()]},
        started_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    return store.save_candidate(
        candidate_id,
        source="condition_research",
        source_history_id="study-1",
        source_candidate_id="buy-1",
        side="buy",
        label="候选",
        condition=_CONDITION,
        evidence={},
        status=STATUS_ADOPTED,
    )


def test_real_store_publish_is_atomic_and_returns_both_records(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _real_adopted_candidate(store)
    service = CandidateLibraryService(store)

    outcome = service.publish("candidate", name="atomic publish", overrides={"stop_loss_pct": 0.1})

    assert outcome["candidate"] is not None
    assert outcome["candidate"]["status"] == STATUS_PUBLISHED
    assert outcome["candidate"]["published_strategy_id"] == outcome["strategy"]["strategy_id"]
    assert outcome["strategy"]["tracking_status"] == "off"
    assert outcome["strategy"]["launched_at"] is None


def test_real_store_publish_conflict_does_not_leave_orphan_strategy(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    candidate = _real_adopted_candidate(store)
    expected_revision = candidate["updated_at"]
    store.update_candidate("candidate", status=STATUS_REJECTED)

    from aquest.market_data.store_history_candidates import CandidatePublishConflict

    with pytest.raises(CandidatePublishConflict):
        store.publish_candidate_strategy(
            "candidate",
            expected_updated_at=expected_revision,
            strategy_id="orphan",
            name="must rollback",
            note="",
            request={},
            summary={},
            source_history_id="study-1",
        )

    assert store.read_strategy("orphan") is None
    assert store.read_candidate("candidate")["status"] == STATUS_REJECTED


def test_real_store_publish_insert_failure_rolls_back_candidate_transition(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    candidate = _real_adopted_candidate(store)
    store.save_strategy(
        "duplicate-id",
        name="existing",
        request={},
        summary={},
    )

    with pytest.raises(Exception):
        store.publish_candidate_strategy(
            "candidate",
            expected_updated_at=candidate["updated_at"],
            strategy_id="duplicate-id",
            name="would-be-publish",
            note="",
            request={},
            summary={},
            source_history_id="study-1",
        )

    assert store.read_candidate("candidate")["status"] == STATUS_ADOPTED
    assert [strategy["name"] for strategy in store.list_strategies()] == ["existing"]


def test_real_store_duplicate_publish_is_rejected_by_revision_cas(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _real_adopted_candidate(store)
    service = CandidateLibraryService(store)
    first = service.publish("candidate", name="one", overrides={"stop_loss_pct": 0.1})

    with pytest.raises(CandidateLibraryError, match="已经是.*已发布"):
        service.publish("candidate", name="two", overrides={"stop_loss_pct": 0.1})

    assert len(store.list_strategies()) == 1
    assert (
        store.read_candidate("candidate")["published_strategy_id"]
        == first["strategy"]["strategy_id"]
    )


def test_publish_refuses_candidate_changed_after_plan_was_prepared() -> None:
    class RacingStore(FakeStore):
        race = False

        def read_backtest_history(self, history_id: str) -> dict[str, Any] | None:
            record = super().read_backtest_history(history_id)
            if self.race:
                self.race = False
                candidate = next(iter(self.candidates.values()))
                candidate["status"] = STATUS_PENDING
                candidate["updated_at"] = "changed-during-plan"
            return record

    store = RacingStore([_history()])
    service, candidate = _imported(store)
    service.set_status(str(candidate["candidate_id"]), status=STATUS_ADOPTED)
    store.race = True

    with pytest.raises(CandidateLibraryError, match="已变化"):
        _publish(service, str(candidate["candidate_id"]))

    assert store.strategies == {}
    assert store.read_candidate(str(candidate["candidate_id"]))["status"] == STATUS_PENDING


def _condition_key(condition: dict[str, Any]) -> tuple[Any, ...]:
    return (
        condition["left_value"],
        condition["operator"],
        condition.get("right_value"),
        condition.get("right_constant"),
    )
