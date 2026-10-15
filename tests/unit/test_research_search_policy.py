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
    assert MARKET_POOL_ALGORITHM_VERSION == "market-pool-v8"


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
    assert 0 < meta["fidelity_evaluations"]["medium"] <= 120
    assert 0 < meta["fidelity_evaluations"]["full"] <= 40
    assert result.results_by_return


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
    assert result.search_metadata["fidelity_evaluations"]["medium"] == 120
    assert result.search_metadata["fidelity_evaluations"]["full"] == 40


def test_one_thousand_budget_runs_to_completion():
    result = _search(lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10})
    assert result.search_metadata["evaluated_trials"] == 1000
    assert result.search_metadata["stop_reason"] == "completed"


def test_two_thousand_budget_requires_fifteen_hundred_trials():
    result = _search(
        lambda c, stage: {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10}, limit=2000
    )
    assert result.search_metadata["evaluated_trials"] == 1500
