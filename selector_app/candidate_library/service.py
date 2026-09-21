"""Orchestration for the candidate library.

Two rules shape everything here:

* **Nothing goes online on its own.** Publishing writes a strategy with
  ``tracking_status='off'`` and stops there. Going online freezes the research
  baseline, so it stays a separate, explicit action on the tracking page.
* **Adoption is a human verdict.** A candidate that passed a permutation test is
  not automatically "adopted" - statistical significance and "I believe this"
  are different claims, and conflating them is how a library turns into a
  recommendation engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from selector_app.candidate_library.evidence import build_evidence, build_label
from selector_app.candidate_library.models import (
    MAX_IMPORT_PER_REQUEST,
    NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SOURCE_CONDITION_RESEARCH,
    STATUS_ADOPTED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    PublishPlan,
    as_mapping,
    can_transition,
    transition_reason,
)
from selector_app.candidate_library.publish import OVERRIDE_FIELDS, build_publish_plan
from selector_app.market_data.store_history import (
    CANDIDATE_STATUS_PUBLISHED,
    STRATEGY_TRACKING_ACTIVE,
)


class CandidateLibraryError(ValueError):
    """A refusal the caller can act on (wrong status, missing study, ...)."""


class CandidateNotFoundError(CandidateLibraryError):
    """The candidate simply is not in the library - a 404, not a conflict."""


class CandidateLibraryService:
    """Store, judge and publish mined conditions."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_candidates(self, status: str | None = None) -> list[dict[str, object]]:
        items: list[dict[str, object]] = self._store.list_candidates(status)
        return items

    def read_candidate(self, candidate_id: str) -> dict[str, object] | None:
        record: dict[str, object] | None = self._store.read_candidate(candidate_id)
        return record

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------

    def import_candidates(
        self,
        *,
        history_id: str,
        candidate_ids: Iterable[str],
        note: str = "",
    ) -> dict[str, object]:
        """Copy candidates out of one study into the library.

        证据整份复制进来，不回指历史记录：历史只留最近 20 条，第 21 次研究跑完，
        最早那批候选的证据就没了。
        """

        wanted = [str(item).strip() for item in candidate_ids if str(item).strip()]
        if not wanted:
            raise CandidateLibraryError("请先选择要入库的候选条件")
        if len(wanted) > MAX_IMPORT_PER_REQUEST:
            raise CandidateLibraryError(f"一次最多入库 {MAX_IMPORT_PER_REQUEST} 条候选")
        record = self._store.read_backtest_history(str(history_id).strip())
        if record is None:
            raise CandidateLibraryError(
                "这次研究的记录已不在历史列表里（只保留最近 20 条），无法入库。"
            )
        backtest_type = str(record.get("backtest_type") or "")
        if backtest_type != "condition_research":
            raise CandidateLibraryError("只有买卖条件研究的结果可以入库")
        request = as_mapping(record.get("request"))
        result = as_mapping(record.get("result"))
        raw_candidates = result.get("candidates")
        candidate_rows = raw_candidates if isinstance(raw_candidates, (list, tuple)) else ()
        rows = [item for item in candidate_rows if isinstance(item, Mapping)]
        created_at = str(record.get("created_at") or "")
        added: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for candidate_id in wanted:
            item = next(
                (row for row in rows if str(row.get("candidate_id")) == candidate_id),
                None,
            )
            if item is None:
                skipped.append({"candidate_id": candidate_id, "reason": "这次研究里没有这条候选"})
                continue
            existing = self._store.find_candidate_by_source(str(history_id).strip(), candidate_id)
            if existing is not None:
                skipped.append({"candidate_id": candidate_id, "reason": "已经在候选库里了"})
                continue
            condition = as_mapping(item.get("condition"))
            if not condition:
                skipped.append({"candidate_id": candidate_id, "reason": "候选条件内容缺失"})
                continue
            added.append(
                self._store.save_candidate(
                    uuid.uuid4().hex,
                    source=SOURCE_CONDITION_RESEARCH,
                    source_history_id=str(history_id).strip(),
                    source_candidate_id=candidate_id,
                    side=str(item.get("side") or ""),
                    label=build_label(condition),
                    condition=condition,
                    evidence=build_evidence(
                        item,
                        request=request,
                        result=result,
                        created_at=created_at,
                    ),
                    status=STATUS_PENDING,
                    note=note.strip()[:NOTE_MAX_LENGTH],
                )
            )
        return {"added": added, "skipped": skipped}

    # ------------------------------------------------------------------
    # 采纳 / 否决 / 撤回
    # ------------------------------------------------------------------

    def set_status(
        self,
        candidate_id: str,
        *,
        status: str,
        note: str | None = None,
    ) -> dict[str, object]:
        current = self._require_candidate(candidate_id)
        target = str(status).strip()
        if not can_transition(str(current["status"]), target):
            raise CandidateLibraryError(transition_reason(str(current["status"]), target))
        normalized_note = note.strip() if isinstance(note, str) else None
        updated: dict[str, object] | None
        if target == STATUS_ADOPTED and str(current["status"]) == STATUS_PUBLISHED:
            self._drop_published_strategy(current)
            updated = self._store.update_candidate(
                candidate_id,
                status=target,
                note=normalized_note,
                published_strategy_id=None,
                publish=True,
            )
        else:
            updated = self._store.update_candidate(
                candidate_id, status=target, note=normalized_note
            )
        if updated is None:  # pragma: no cover - 上一步刚读到过
            raise CandidateLibraryError("候选不存在")
        return updated

    def delete_candidate(self, candidate_id: str) -> bool:
        current = self._require_candidate(candidate_id)
        if str(current["status"]) == CANDIDATE_STATUS_PUBLISHED:
            raise CandidateLibraryError(
                "已发布的候选不能直接删除：先撤回发布（会一并删掉由它生成的策略），"
                "再删除，否则策略库里会留下一条没人认领的策略。"
            )
        deleted: bool = self._store.delete_candidate(candidate_id)
        return deleted

    def _drop_published_strategy(self, candidate: Mapping[str, object]) -> None:
        """Remove the strategy a publish created, refusing once it went online."""

        strategy_id = candidate.get("published_strategy_id")
        if not strategy_id:
            return
        strategy: dict[str, object] | None = self._store.read_strategy(str(strategy_id))
        if strategy is None:
            return
        if str(strategy.get("tracking_status")) == STRATEGY_TRACKING_ACTIVE:
            raise CandidateLibraryError(
                "这条候选生成的策略已上线，不能撤回：先在策略追踪页停止它，"
                "否则观察段的记录会失去归属。"
            )
        self._store.delete_strategy(str(strategy_id))

    def _require_candidate(self, candidate_id: str) -> dict[str, object]:
        candidate: dict[str, object] | None = self._store.read_candidate(str(candidate_id).strip())
        if candidate is None:
            raise CandidateNotFoundError("候选不存在或已被删除")
        return candidate

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def publish_preview(self, candidate_id: str) -> dict[str, object]:
        """Show what publishing would write, before anything is written.

        「显式确认」不是弹个窗问「确定吗」，而是让人看见将要落库的是什么。
        """

        candidate = self._require_candidate(candidate_id)
        plan = self._build_plan(candidate)
        return {
            "candidate_id": str(candidate["candidate_id"]),
            "label": str(candidate["label"]),
            "side": str(candidate["side"]),
            "status": str(candidate["status"]),
            # 校验没过时 request 是空的，但已经拼出来的配置照样要给人看——否则
            # 预览只会说"不行"，而说不出"差在哪一项"。
            "request": plan.request or plan.base_request,
            "validated": not plan.blocked,
            "notes": list(plan.notes),
            "blockers": list(plan.blockers),
            "blocked": plan.blocked,
            "ready": str(candidate["status"]) == STATUS_ADOPTED and not plan.blocked,
        }

    def publish(
        self,
        candidate_id: str,
        *,
        name: str,
        note: str = "",
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Write one strategy for one candidate - and leave it offline."""

        candidate = self._require_candidate(candidate_id)
        if str(candidate["status"]) != STATUS_ADOPTED:
            raise CandidateLibraryError(
                transition_reason(str(candidate["status"]), STATUS_PUBLISHED)
            )
        normalized_name = str(name).strip()
        if not normalized_name:
            raise CandidateLibraryError("请给要发布的策略起个名字")
        if len(normalized_name) > NAME_MAX_LENGTH:
            raise CandidateLibraryError(f"策略名称不能超过 {NAME_MAX_LENGTH} 个字符")
        plan = self._build_plan(candidate, overrides)
        if plan.blocked:
            raise CandidateLibraryError("；".join(plan.blockers))
        request = plan.request
        strategy: dict[str, object] = self._store.save_strategy(
            uuid.uuid4().hex,
            name=normalized_name,
            note=str(note).strip()[:NOTE_MAX_LENGTH],
            request=request,
            summary=_publish_summary(request),
            source_history_id=str(candidate["source_history_id"]) or None,
        )
        updated: dict[str, object] | None = self._store.update_candidate(
            candidate_id,
            status=STATUS_PUBLISHED,
            published_strategy_id=str(strategy["strategy_id"]),
            publish=True,
        )
        return {
            "candidate": updated,
            "strategy": strategy,
            # 永不自动上线：这一行是给前端和读代码的人看的承诺，不是状态字段的
            # 回声——发布路径里没有任何地方会调用 launch。
            "online": False,
            "notes": list(plan.notes),
            "message": (
                f"已发布为策略「{normalized_name}」，未上线。"
                "要让它上线并开始观察，请到策略追踪页手动启动。"
            ),
        }

    def _build_plan(
        self,
        candidate: Mapping[str, object],
        overrides: Mapping[str, object] | None = None,
    ) -> PublishPlan:
        history_id = str(candidate.get("source_history_id") or "").strip()
        record: dict[str, object] | None = self._store.read_backtest_history(history_id)
        if record is None:
            raise CandidateLibraryError(
                "这条候选的来源研究已不在历史列表里（只保留最近 20 条），"
                "无法还原它的配置，也就无法发布。"
            )
        return build_publish_plan(
            research_request=as_mapping(record.get("request")),
            condition=as_mapping(candidate.get("condition")),
            side=str(candidate.get("side") or ""),
            overrides={
                key: value for key, value in as_mapping(overrides).items() if key in OVERRIDE_FIELDS
            },
        )


def _publish_summary(request: Mapping[str, object]) -> dict[str, object]:
    """The descriptive half of a strategy snapshot.

    Performance is deliberately absent: publishing does not run a backtest, and
    writing zeros here would look like a strategy that made nothing.
    """

    summary: dict[str, object] = {}
    for key in (
        "start_date",
        "end_date",
        "universe",
        "max_positions",
        "ranking_value",
        "rank_order",
    ):
        value = request.get(key)
        if value is not None:
            summary[key] = value
    return summary
