from datetime import date

from aquest.individual_research.models import IndividualResearchConfig
from aquest.individual_research.service import IndividualStockResearchService
from aquest.strategy_pool.service import MARKET_POOL_ALGORITHM_VERSION, _research_settings


def test_research_defaults_share_expanded_policy():
    config = IndividualResearchConfig(
        "SH", "600000", "", date(2000, 1, 1), date(2026, 1, 1), (), ()
    )
    assert config.candidate_limit == 15
    assert _research_settings({}, 3000)["candidate_limit"] == 15
    assert MARKET_POOL_ALGORITHM_VERSION == "market-pool-v9"


def test_research_risk_search_enables_bounded_adaptive_policy():
    config = IndividualResearchConfig(
        "SH", "600000", "", date(2000, 1, 1), date(2026, 1, 1), (), ()
    )
    windows = {
        p: {"start": config.start_date, "end": config.end_date}
        for p in ("train", "validation", "holdout")
    }
    request = IndividualStockResearchService._risk_discovery_request(
        None, {}, config, windows, seed=7
    )
    assert request.convergence_enabled
    assert request.research_search_policy == "adaptive-v1"

    # 复杂度上限跟着继承来的规则走：规则本身不是风控寻优在搜的东西，写死 3 会让 2+2
    # 的规则对永远过不了约束。
    pair_request = IndividualStockResearchService._risk_discovery_request(
        None,
        {
            "buy_rule": {"signals": ["a"], "conditions": [{}], "required_conditions": []},
            "sell_rule": {"signals": ["b"], "conditions": [{}], "required_conditions": []},
        },
        config,
        windows,
        seed=7,
    )
    assert pair_request.max_complexity == 7


def _search(metrics, *, limit=1000, fidelity=False, batch=1):
    from aquest.screening.rules import RuleGroup
    from aquest.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
    from aquest.strategy_discovery.search import HybridSearchEngine

    counter = iter(range(10000))

    def build(_request, _rng):
        index = next(counter)
        return StrategyCandidate(
            RuleGroup(signals=("entry",)),
            RuleGroup(signals=("exit",)),
            "price.close",
            "desc",
            1,
            stop_loss_pct=(index + 1) / 20000,
        )

    request = DiscoveryRequest(
        base_config={},
        candidate_mode="risk_only",
        max_trials=limit,
        min_trades=1,
        max_drawdown=None,
        convergence_enabled=True,
        research_search_policy="adaptive-v1",
        multifidelity_enabled=fidelity,
    )
    return HybridSearchEngine().search(
        request,
        candidate_builder=build,
        evaluator=metrics,
        fidelity_evaluator=metrics if fidelity else None,
        batch_size=batch,
    )


def test_adaptive_plateau_stops_and_completes_refinement():
    result = _search(
        lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10},
        fidelity=True,
        batch=8,
        limit=3000,
    )
    meta = result.search_metadata
    assert meta["stop_reason"] == "converged"
    assert 1500 <= meta["evaluated_trials"] < 3000
    assert result.results_by_return
    # 风控寻优不做精度粗筛（见 test_risk_only_search_ignores_multifidelity_screening），
    # 所以即使请求里开着多精度，也不该有 coarse/medium/full 的分层评估。
    assert meta["fidelity_evaluations"] == {"coarse": 0, "medium": 0, "full": 0}


def test_risk_only_search_ignores_multifidelity_screening() -> None:
    """风控寻优即使开着多精度也必须按全精度评估，一组都不能被粗筛窗口筛掉。

    粗筛窗口只取训练段前 25%。买入信号没落进那一段的标的（实测 SH600067 最早一笔买入
    在第 28%）会在筛选上被判成"零成交"，六对规则全部跑完也拿不到候选，最后整只标的被记成
    无可用策略。风控寻优只搜三组参数，省这点时间不值当，直接把分层筛选关掉。
    """

    stages: list[str] = []

    def metrics(_candidate, stage):
        stages.append(stage)
        return {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10}

    result = _search(metrics, limit=40, fidelity=True, batch=8)
    assert set(stages) == {"search"}
    assert result.search_metadata["fidelity_evaluations"] == {"coarse": 0, "medium": 0, "full": 0}
    assert result.results_by_return


def test_coarse_screening_does_not_reject_for_missing_signals_in_its_window() -> None:
    """粗筛窗口里没有成交是常态，不能据此把候选判死；终判在 medium/full 与榜单上。"""

    from aquest.strategy_discovery.models import DiscoveryRequest
    from aquest.strategy_discovery.search import _passes_constraints

    flat = {
        "annual_return": 0.0,
        "sharpe": 0.0,
        "total_trades": 0,
        "executed_buy_orders": 0.0,
        "executed_sell_orders": 0.0,
    }
    request = DiscoveryRequest(
        base_config={}, candidate_mode="risk_only", min_trades=0, max_drawdown=None
    )
    assert _passes_constraints(flat, request) is False
    assert _passes_constraints(flat, request, allow_zero_execution=True) is True


def test_adaptive_invalid_candidates_do_not_converge():
    result = _search(lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 0})
    assert result.search_metadata["evaluated_trials"] == 1000
    assert result.search_metadata["stop_reason"] == "completed"


def test_adaptive_cumulative_improvements_continue_search():
    result = _search(
        lambda c, stage: {"annual_return": 0.1 + c.stop_loss_pct, "sharpe": 1.0, "total_trades": 10}
    )
    assert result.search_metadata["evaluated_trials"] == 1000


def test_trigger_dedup_handles_complements_and_constants():
    import numpy as np

    from aquest.individual_research.service import _similar_triggers

    values = np.array([True, False, True, False])
    assert _similar_triggers(values, ~values)
    assert _similar_triggers(np.zeros(4), np.zeros(4))
    assert not _similar_triggers(np.zeros(4), np.ones(4))
    assert not _similar_triggers(values, np.zeros(4))


def test_candidate_quota_does_not_overflow_when_both_types_have_spares():
    from aquest.individual_research.service import _rule_pairs, _select_condition_candidates
    from aquest.screening.rules import RuleGroup

    candidates = [
        {
            "rule": RuleGroup(signals=(f"signal-{i}",)),
            "lift": 40 - i,
            "support_count": 10,
            "display": str(i),
            "kind": "signal" if i < 20 else "condition_combo",
        }
        for i in range(40)
    ]
    selected = _select_condition_candidates(candidates, 15)
    assert len(selected) == 15
    assert sum(c["kind"] == "condition_combo" for c in selected) == 7
    assert len(_rule_pairs(selected, selected)) == 225


def test_adaptive_negative_metrics_and_nan_do_not_break_stop_logic():
    result = _search(
        lambda c, stage: {"annual_return": -0.1, "sharpe": -1.0, "total_trades": 10},
        limit=3000,
    )
    assert result.search_metadata["stop_reason"] == "converged"
    result = _search(
        lambda c, stage: {"annual_return": float("nan"), "sharpe": 1.0, "total_trades": 10}
    )
    assert result.search_metadata["evaluated_trials"] == 1000


def test_three_thousand_budget_respects_minimum_and_batch_boundary():
    result = _search(
        lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10},
        limit=3000,
        fidelity=True,
        batch=8,
    )
    assert result.search_metadata["evaluated_trials"] == 1504
    assert result.search_metadata["convergence_min_trials"] == 1500
    assert result.search_metadata["convergence_patience"] == 1000
    assert result.search_metadata["fidelity_evaluations"] == {"coarse": 0, "medium": 0, "full": 0}


def test_one_thousand_budget_runs_to_completion():
    result = _search(lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10})
    assert result.search_metadata["evaluated_trials"] == 1000
    assert result.search_metadata["stop_reason"] == "completed"


def test_two_thousand_budget_requires_fifteen_hundred_trials():
    result = _search(
        lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10}, limit=2000
    )
    assert result.search_metadata["evaluated_trials"] == 1500
