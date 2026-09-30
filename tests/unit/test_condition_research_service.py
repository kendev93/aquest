from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from selector_app.backtest.models import BacktestReport
from selector_app.condition_research.mining import DuplicateCondition, MinedCondition
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.condition_research.reporting import (
    _daily_hits,
    _resolved_ranking,
    _select_holdout_candidates,
    candidate_constraint_failures,
    candidate_performance_warnings,
)
from selector_app.condition_research.samples import MiningSamples
from selector_app.condition_research.service import (
    ConditionResearchService,
)
from selector_app.condition_research.worker_tasks import _backtest_config, _compact_report
from selector_app.market_data.models import StockRef
from selector_app.screening.jobs import TaskUserError
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup


def test_candidate_must_lift_win_rate_and_preserve_trade_quality() -> None:
    baseline = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.55,
            "payoff_ratio": 1.4,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        }
    }
    candidate = {
        "pooled_trades": {
            "closed_trades": 80,
            "win_rate": 0.62,
            "payoff_ratio": 1.5,
            "mean_trade_return": 0.012,
            "stocks_with_trades": 18,
        }
    }

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=50,
        min_instruments=10,
    )

    assert failures == ()


def test_candidate_fails_when_it_only_improves_win_rate_on_too_few_trades() -> None:
    baseline = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.55,
            "payoff_ratio": 1.4,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        }
    }
    candidate = {
        "pooled_trades": {
            "closed_trades": 12,
            "win_rate": 0.9,
            "payoff_ratio": 0.8,
            "mean_trade_return": -0.02,
            "stocks_with_trades": 2,
        }
    }

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=50,
        min_instruments=10,
    )

    assert "交易样本不足" in failures
    assert "覆盖股票不足" in failures
    assert "盈亏比低于基础规则" in failures
    assert "平均单笔收益低于基础规则" in failures


def test_candidate_without_baseline_win_rate_is_not_claimed_as_improved() -> None:
    baseline = {"pooled_trades": {"win_rate": None}}
    candidate = {"pooled_trades": {"win_rate": 0.8, "closed_trades": 40, "stocks_with_trades": 8}}

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=20,
        min_instruments=5,
    )

    assert "基础规则样本不足，无法比较胜率" in failures


def test_total_return_drop_is_a_warning_not_a_validation_failure() -> None:
    """逐股总收益差于基线需要提示，但应留给留出期和组合回测复核。"""

    baseline = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.50,
            "payoff_ratio": 1.2,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        },
        "per_stock": {"median_total_return": 0.08},
    }
    candidate = {
        "pooled_trades": {
            "closed_trades": 30,
            "win_rate": 0.70,
            "payoff_ratio": 1.6,
            "mean_trade_return": 0.02,
            "stocks_with_trades": 12,
        },
        "per_stock": {"median_total_return": 0.03},
    }

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=20,
        min_instruments=5,
    )

    assert failures == ()
    assert candidate_performance_warnings(candidate, baseline) == ("逐股中位总收益低于基础规则",)
    # 逐笔指标确实都更好了，不该再报那些，否则看不出真正的问题在哪
    assert "胜率未高于基础规则" not in failures
    assert "盈亏比低于基础规则" not in failures


def test_candidate_fails_when_its_lift_is_within_the_noise_threshold() -> None:
    summary = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.60,
            "payoff_ratio": 1.5,
            "mean_trade_return": 0.02,
            "stocks_with_trades": 20,
        }
    }
    baseline = {
        "pooled_trades": {
            "closed_trades": 120,
            "win_rate": 0.50,
            "payoff_ratio": 1.2,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        }
    }

    failures = candidate_constraint_failures(
        summary,
        baseline,
        min_trades=20,
        min_instruments=5,
        significant=False,
    )
    assert "提升未超过噪声门槛" in failures

    # 没做置换检验时不能凭空判一个候选不合格
    without_test = candidate_constraint_failures(
        summary,
        baseline,
        min_trades=20,
        min_instruments=5,
        significant=None,
    )
    assert without_test == ()


class FakeResearchAdapter:
    def __init__(self) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="甲", instrument_type="stock"),
            StockRef("SZ", "000001", Path("/tmp/vipdoc"), name="乙", instrument_type="stock"),
        ]

    def list_stock_refs(
        self,
        vipdoc_path,
        universe,
        universe_file=None,
        instrument_types=None,
        boards=None,
    ):
        del vipdoc_path, universe, universe_file, boards
        return [
            ref
            for ref in self.refs
            if instrument_types is None or ref.instrument_type in instrument_types
        ]


class FakePreparedBacktests:
    def prepare_for_ref(self, config, ref, *, feature_ids=()):
        dates = pd.date_range(config.start_date, config.end_date, freq="D")
        values = [5.0 if index == 4 else 1.0 if index == 19 else 3.0 for index in range(len(dates))]
        features = pd.DataFrame({"date": dates, "custom.score": values})
        return SimpleNamespace(
            config=config,
            ref=ref,
            frame=pd.DataFrame({"date": dates, "open": values, "close": values}),
            feature_values=features,
            data_fingerprint="stable",
        )

    def run_prepared(
        self,
        prepared,
        *,
        buy_filter=None,
        sell_trigger=None,
        start_date=None,
        end_date=None,
        summary_only=False,
    ):
        del sell_trigger, start_date, end_date, summary_only
        ref = prepared.ref
        if buy_filter is not None:
            trades = [
                {"direction": "SELL", "pnl": 10.0, "cost_basis": 100.0, "rejected": False},
                {"direction": "SELL", "pnl": 10.0, "cost_basis": 100.0, "rejected": False},
                {"direction": "SELL", "pnl": -10.0, "cost_basis": 100.0, "rejected": False},
            ]
        else:
            trades = [
                {"direction": "BUY", "signal_date": "2020-01-05", "date": "2020-01-06"},
                {
                    "direction": "SELL",
                    "signal_date": "2020-01-10",
                    "date": "2020-01-11",
                    "pnl": 10.0,
                    "cost_basis": 100.0,
                    "rejected": False,
                },
                {"direction": "BUY", "signal_date": "2020-01-20", "date": "2020-01-21"},
                {
                    "direction": "SELL",
                    "signal_date": "2020-01-25",
                    "date": "2020-01-26",
                    "pnl": -10.0,
                    "cost_basis": 100.0,
                    "rejected": False,
                },
            ]
        return BacktestReport(
            market=ref.market,
            code=ref.code,
            name=ref.name,
            bars=10,
            start_date="2020-01-01",
            end_date="2020-04-30",
            buy_signal=None,
            sell_signal=None,
            performance={
                "total_return": 0.1,
                "annual_return": 0.1,
                "max_drawdown": 0.1,
                "sharpe": 1.0,
            },
            equity_curve=(),
            trades=tuple(trades),
            positions=(),
            configuration={},
        )


def test_service_mines_on_train_and_replays_winner_filter_on_validation_and_holdout() -> None:
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text="BUY:C>0; SELL:C<0; SCORE:C;",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 4, 30),
        min_trades=1,
        min_instruments=1,
        candidate_limit=5,
        holdout_candidates=1,
        # 这个数据集只有两笔交易，置换检验会正确地判定「两笔全赢也可能是运气」，
        # 这里要验证的是挖掘→验证→留出的链路，所以显式关掉检验。
        significance_permutations=0,
    )

    result = service.run(config)

    assert result["processed"] == 2
    assert isinstance(result["data_fingerprint"], str)
    assert len(result["data_fingerprint"]) == 64
    assert result["baseline"]["full_period"]["pooled_trades"]["closed_trades"] > 0
    assert result["candidates"]
    candidate = result["candidates"][0]
    assert candidate["side"] == "buy"
    assert candidate["mining"]["lift_threshold"] is None
    assert candidate["passed_constraints"] is True
    assert candidate["validation"]["pooled_trades"]["win_rate"] == pytest.approx(2 / 3)
    assert candidate["full_period"]["pooled_trades"]["win_rate"] == pytest.approx(2 / 3)
    assert candidate["holdout"] is not None


def test_service_aborts_if_market_data_changes_between_time_split_passes() -> None:
    class ChangingBacktests(FakePreparedBacktests):
        def __init__(self) -> None:
            self.prepare_counts: dict[str, int] = {}

        def prepare_for_ref(self, config, ref, *, feature_ids=()):
            prepared = super().prepare_for_ref(config, ref, feature_ids=feature_ids)
            count = self.prepare_counts.get(ref.code, 0) + 1
            self.prepare_counts[ref.code] = count
            prepared.data_fingerprint = "initial" if count == 1 else "changed"
            return prepared

    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=ChangingBacktests(),  # type: ignore[arg-type]
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text="BUY:C>0; SELL:C<0; SCORE:C;",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 4, 30),
        search_sides="buy",
        min_trades=1,
        min_instruments=1,
        candidate_limit=5,
        holdout_candidates=1,
    )

    with pytest.raises(TaskUserError, match="行情数据在研究期间发生变化"):
        service.run(config)


def _report_with_trades() -> BacktestReport:
    trade = {
        "datetime": pd.Timestamp("2024-01-03"),
        "market": "SH",
        "code": "600000",
        "name": "测试股份",
        "direction": "SELL",
        "size": 100.0,
        "price": 11.0,
        "commission": 0.11,
        "stamp_tax": 1.1,
        "slippage": 0.0,
        "pnl": 90.0,
        "cost_basis": 1000.0,
        "rejected": False,
    }
    return BacktestReport(
        market="SH",
        code="600000",
        name="测试股份",
        bars=5,
        start_date="2024-01-02",
        end_date="2024-01-06",
        buy_signal="custom.buy",
        sell_signal="custom.sell",
        performance={"total_return": 0.09, "win_rate": 1.0},
        equity_curve=({"date": "2024-01-02", "total": 10_000.0},),
        trades=(trade,),
        positions=({"datetime": pd.Timestamp("2024-01-03"), "size": 0.0},),
        configuration={"initial_cash": 10_000.0},
        diagnostic=None,
    )


def test_compact_report_equals_the_full_serialisation_projection() -> None:
    """回归：精简报告必须与「先 to_dict 再裁剪」的旧写法逐字段一致。"""

    report = _report_with_trades()

    legacy = report.to_dict()
    legacy["trades"] = [
        {key: trade.get(key) for key in ("direction", "pnl", "cost_basis", "rejected")}
        for trade in legacy["trades"]
    ]
    legacy["equity_curve"] = []
    legacy["positions"] = []

    compact = _compact_report(report)

    assert compact == legacy
    assert list(compact) == list(legacy)
    assert compact["trades"] == [
        {"direction": "SELL", "pnl": 90.0, "cost_basis": 1000.0, "rejected": False}
    ]


def _service_for_features() -> ConditionResearchService:
    return ConditionResearchService(adapter=SimpleNamespace(), backtest_service=SimpleNamespace())


def _config_with_formula(formula_text: str) -> ConditionResearchConfig:
    return ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=formula_text,
        start_date=date(2020, 1, 1),
        end_date=date(2020, 12, 31),
    )


def test_price_scale_custom_outputs_are_excluded_from_mining() -> None:
    service = _service_for_features()
    # 用 ``:=`` 让 SCORE/RATIO 只作为数值输出，避免信号语句也带出同名输出。
    config = _config_with_formula("SCORE:=C; RATIO:=C/MA(C,20); CROSS(REF(C,1),C);")

    assert service._feature_ids(config) == ("custom.ratio",)


def test_a_purely_price_scale_formula_is_rejected_with_a_readable_error() -> None:
    service = _service_for_features()
    config = _config_with_formula("SCORE:=C; CROSS(REF(C,1),C);")

    with pytest.raises(ValueError, match="价格/成交量量纲"):
        service._feature_ids(config)


class FakeMixedResearchAdapter(FakeResearchAdapter):
    def __init__(self) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="甲", instrument_type="stock"),
            StockRef(
                "SZ",
                "300001",
                Path("/tmp/vipdoc"),
                name="乙创业板",
                instrument_type="stock",
                board="chinext",
            ),
        ]


def _research_config(**overrides: object) -> ConditionResearchConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "buy_rule": RuleGroup(signals=("custom.buy",)),
        "sell_rule": RuleGroup(signals=("custom.sell",)),
        "start_date": date(2020, 1, 1),
        "end_date": date(2020, 12, 31),
        "min_trades": 1,
        "min_instruments": 1,
    }
    values.update(overrides)
    return ConditionResearchConfig(**values)  # type: ignore[arg-type]


def test_resolved_ranking_prefers_the_explicit_choice() -> None:
    """用户手选的排序指标优先，推荐值再好也不能覆盖它。"""

    config = _research_config(ranking_value="technical.score", rank_order="asc")
    assert _resolved_ranking(config, {"recommended": None}) == ("technical.score", "asc")


def test_resolved_ranking_falls_back_to_the_recommendation() -> None:
    """没手选时用推荐值，这样「前 N 名」那一列不会凭空消失。"""

    config = _research_config()
    ranking = {"recommended": {"value_id": "technical.score", "order": "asc"}}
    assert _resolved_ranking(config, ranking) == ("technical.score", "asc")


def test_resolved_ranking_is_empty_without_choice_or_recommendation() -> None:
    config = _research_config()
    assert _resolved_ranking(config, {"recommended": None})[0] is None


def test_research_pool_follows_the_selected_board() -> None:
    service = ConditionResearchService(
        adapter=FakeMixedResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    stock_pool = service._list_stock_refs(_research_config())
    main_pool = service._list_stock_refs(_research_config(boards=("main",)))

    assert [ref.code for ref in stock_pool] == ["600000", "300001"]
    assert [ref.code for ref in main_pool] == ["600000"]


def test_stock_research_uses_stock_costs_with_stamp_tax() -> None:
    config = _research_config()
    stock_ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    stock_backtest = _backtest_config(config, stock_ref)

    assert stock_backtest.commission == 0.0001
    assert stock_backtest.stamp_tax == 0.001


def test_missing_sell_rule_switches_on_hold_to_end() -> None:
    """只给买入条件时，回测必须开启期末平仓，否则持仓永远不产生卖出交易。"""

    config = _research_config(sell_rule=None)
    ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    backtest = _backtest_config(config, ref)

    assert backtest.sell_rule is None
    assert backtest.close_at_end is True


def test_missing_buy_rule_switches_on_always_in_market() -> None:
    """只给卖出条件时，回测必须持续满仓，否则永远不入场、一笔交易也没有。"""

    config = _research_config(buy_rule=None)
    ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    backtest = _backtest_config(config, ref)

    assert backtest.buy_rule is None
    assert backtest.always_in_market is True
    assert backtest.close_at_end is False


def test_explicit_buy_rule_keeps_the_original_entry_behaviour() -> None:
    config = _research_config()
    ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    assert _backtest_config(config, ref).always_in_market is False


def test_both_rules_may_not_be_left_out_at_once() -> None:
    with pytest.raises(ValueError, match="不能同时留空"):
        _research_config(buy_rule=None, sell_rule=None)


def test_explicit_sell_rule_keeps_the_original_close_behaviour() -> None:
    """给了卖出条件就保持原样：期末不平仓，避免悄悄改变既有研究结果。"""

    config = _research_config()
    ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    assert _backtest_config(config, ref).close_at_end is False


def test_sell_rule_may_be_left_out_but_not_left_empty() -> None:
    """「持有到期末」只能用 None 表达：空规则组在规则层就被拒，不会蒙混过关。"""

    with pytest.raises(ValueError, match="至少需要一个信号或指标条件"):
        _research_config(sell_rule=RuleGroup())

    with pytest.raises(ValueError, match="至少需要一个信号或指标条件"):
        _research_config(buy_rule=RuleGroup())


def test_stock_research_keeps_stock_commission_when_stamp_tax_is_cleared() -> None:
    """回归：逐字段回退不能因为改了一项就把其他项退回别的口径。"""

    config = _research_config(stamp_tax=0.0)
    stock_ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")

    stock_backtest = _backtest_config(config, stock_ref)

    assert stock_backtest.commission == 0.0001
    assert stock_backtest.stamp_tax == 0.0


def test_ranking_settings_round_trip_through_the_config_dict() -> None:
    """排序规则要随历史记录一起存下来，否则回看时无法知道当年按什么推荐。"""

    config = _research_config(ranking_value="technical.rsi14", rank_order="asc", ranking_top_n=3)

    payload = config.to_dict()

    assert payload["ranking_value"] == "technical.rsi14"
    assert payload["rank_order"] == "asc"
    assert payload["ranking_top_n"] == 3


def test_ranking_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="排序方向"):
        _research_config(rank_order="sideways")

    with pytest.raises(ValueError, match="排序槽位数"):
        _research_config(ranking_top_n=0)

    with pytest.raises(ValueError, match="排序指标"):
        _research_config(ranking_value="   ")


def _samples_for(dates: list[str]) -> MiningSamples:
    frame = pd.DataFrame({"technical.score": [1.0] * len(dates)})
    return MiningSamples(
        features=frame,
        outcomes=pd.Series([True] * len(dates)),
        instruments=pd.Series(["SH600000"] * len(dates)),
        groups=pd.Series([f"g{index}" for index in range(len(dates))]),
        dates=pd.Series(dates),
        returns=pd.Series([0.1] * len(dates)),
    )


def test_daily_hits_counts_stocks_per_active_day() -> None:
    """日均命中股票数是判断「条件在 N 槽位组合里还有没有意义」的唯一依据。"""

    first = _samples_for(["2024-01-01", "2024-01-01", "2024-01-02"])
    second = _samples_for(["2024-01-02", "2024-01-03"])

    assert _daily_hits([first, second]) == {"hit_days": 3, "daily_hits": pytest.approx(5 / 3)}
    assert _daily_hits([]) == {"hit_days": 0, "daily_hits": None}


def test_result_carries_ranking_advice_and_hit_diagnostics() -> None:
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text="BUY:C>0; SELL:C<0; SCORE:C;",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 4, 30),
        min_trades=1,
        min_instruments=1,
        ranking_value="custom.score",
        rank_order="desc",
        ranking_top_n=5,
    )

    result = service.run(config)

    ranking = result["ranking"]
    assert ranking["top_n"] == 5
    assert ranking["selection"] == {"value": "custom.score", "order": "desc"}
    assert isinstance(ranking["evaluated"], list)
    assert "reason" in ranking
    buy_totals = result["sample_totals"]["buy"]
    assert buy_totals["hit_days"] > 0
    assert buy_totals["daily_hits"] > 0


def _rolling_config(**overrides: object) -> ConditionResearchConfig:
    """五年区间：留出段约 366 天，切 3 窗每窗约 122 天，够 30 天的下限。"""

    payload: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "buy_rule": RuleGroup(signals=("custom.buy",)),
        "sell_rule": RuleGroup(signals=("custom.sell",)),
        "formula_text": "BUY:C>0; SELL:C<0; SCORE:C;",
        "start_date": date(2020, 1, 1),
        "end_date": date(2024, 12, 31),
        "min_trades": 1,
        "min_instruments": 1,
        "significance_permutations": 0,
    }
    payload.update(overrides)
    return ConditionResearchConfig(**payload)  # type: ignore[arg-type]


def test_rolling_windows_replay_the_same_candidates_on_every_slice() -> None:
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config(walk_forward_windows=3))

    assert result["rolling"]["enabled"] is True
    assert result["rolling"]["requested"] == 3
    assert result["rolling"]["used"] == 3
    assert result["rolling"]["note"] is None
    # 基线也要逐窗给，否则候选的胜率没有对照物。
    assert [item["index"] for item in result["baseline"]["windows"]] == [1, 2, 3]

    tracked = [item for item in result["candidates"] if item["windows"]]
    assert tracked, "进了留出期的候选必须带上逐窗结果"
    for candidate in tracked:
        assert len(candidate["windows"]) == 3
        # 逐窗首尾相接，铺满整个留出段，不多不少。
        assert candidate["windows"][0]["start_date"] == result["splits"]["holdout"]["start_date"]
        assert candidate["windows"][-1]["end_date"] == result["splits"]["holdout"]["end_date"]
        consistency = candidate["consistency"]
        assert consistency["evaluated"] == 3
        assert consistency["median_win_rate"] == pytest.approx(2 / 3)


def test_rolling_is_off_by_default_and_leaves_the_result_unchanged() -> None:
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    assert result["rolling"]["enabled"] is False
    assert result["rolling"]["requested"] == 1
    assert result["rolling"]["used"] == 1
    assert result["rolling"]["note"] is None
    assert result["baseline"]["windows"] == []
    assert all(candidate["windows"] == [] for candidate in result["candidates"])
    assert all(candidate["consistency"] is None for candidate in result["candidates"])


def test_rolling_windows_are_downgraded_when_the_holdout_is_too_short() -> None:
    """留出段只有 24 天时切不出 3 个 30 天的窗口，要降级而不是报错。"""

    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(
        _rolling_config(
            start_date=date(2020, 1, 1),
            end_date=date(2020, 4, 30),
            walk_forward_windows=3,
        )
    )

    assert result["rolling"]["requested"] == 3
    assert result["rolling"]["used"] == 1
    assert result["rolling"]["enabled"] is False
    # 降级要说出来，不能悄悄少跑几个窗口。
    assert "切不出 3 个窗口" in (result["rolling"]["note"] or "")


def test_merged_duplicates_reach_the_report() -> None:
    """被合并掉的同义条件必须出现在报告里，不能静默消失。"""

    class FixedCandidates(ConditionResearchService):
        def _mine_candidates(self, config, buy_samples, sell_samples, **kwargs):
            kept = MinedCondition(
                condition=ScreenCondition(
                    left_value="technical.ma5", operator="gte", right_constant=1.0
                ),
                support_count=30,
                support_ratio=0.5,
                instrument_count=5,
                outcome_rate=0.8,
                baseline_rate=0.5,
                lift=1.6,
                duplicates=(
                    DuplicateCondition(
                        condition=ScreenCondition(
                            left_value="technical.ma5", operator="gte", right_constant=1.2
                        ),
                        correlation=0.612,
                        lift=1.55,
                        support_count=28,
                    ),
                ),
            )
            return [("buy-1", "buy", kept)]

    service = FixedCandidates(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    assert result["duplicate_correlation"] == pytest.approx(0.6)
    assert result["merged_duplicate_count"] == 1
    duplicates = result["candidates"][0]["mining"]["duplicates"]
    assert len(duplicates) == 1
    assert duplicates[0]["correlation"] == pytest.approx(0.612)
    assert duplicates[0]["condition"]["right_constant"] == pytest.approx(1.2)


def test_every_candidate_carries_its_threshold_neighbourhood() -> None:
    """邻域曲线挂在每个候选上，不需要等它进留出期；基点必须和挖掘给的 lift 同源。"""

    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    candidates = result["candidates"]
    assert candidates, "这一组假数据应当挖出候选"
    for candidate in candidates:
        sensitivity = candidate["sensitivity"]
        assert sensitivity is not None
        assert len(sensitivity["points"]) == 5
        base = next(point for point in sensitivity["points"] if point["offset"] == 0.0)
        assert base["lift"] == pytest.approx(candidate["mining"]["lift"])
        assert base["retention"] == pytest.approx(1.0)


def test_duplicate_correlation_travels_with_the_configuration() -> None:
    """门槛要能随历史记录还原，否则回看时不知道当年的候选是按什么口径合并的。"""

    assert _rolling_config().to_dict()["duplicate_correlation"] == pytest.approx(0.6)
    assert _rolling_config(duplicate_correlation=0.9).to_dict()["duplicate_correlation"] == (
        pytest.approx(0.9)
    )
    with pytest.raises(ValueError, match="同义条件合并门槛"):
        _rolling_config(duplicate_correlation=0.2)
    with pytest.raises(ValueError, match="同义条件合并门槛"):
        _rolling_config(duplicate_correlation=1.5)


def test_rolling_is_skipped_when_no_candidate_reaches_the_holdout() -> None:
    """没有候选可比时，逐窗基线没有对照对象，不该再跑 K 轮逐股回放。"""

    class NoCandidates(ConditionResearchService):
        def _mine_candidates(self, config, buy_samples, sell_samples, **kwargs):
            return []

    service = NoCandidates(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config(walk_forward_windows=3))

    assert result["candidates"] == []
    assert result["rolling"]["enabled"] is False
    assert result["baseline"]["windows"] == []
    assert "没有候选通过验证" in (result["rolling"]["note"] or "")


def _holdout_row(
    candidate_id: str,
    *,
    win_rate: float,
    improved_stock_ratio: float,
    closed_trades: int,
    lift: float,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "side": "buy",
        "passed_constraints": True,
        "mining": {"lift": lift},
        "validation": {
            "pooled_trades": {"win_rate": win_rate, "closed_trades": closed_trades},
            "per_stock": {"improved_stock_ratio": improved_stock_ratio},
        },
    }


def test_holdout_selection_never_ranks_by_in_sample_lift() -> None:
    """验证段指标打平时，不能拿寻优段的 lift 决定送谁去留出期复验。

    挖掘输出本身就是 lift 降序，所以「并列时保持输入顺序」等于照样按 lift 挑人。
    这里让样本内 lift 最高的恰好是证据最薄的那条：它必须落选。
    """

    thick = _holdout_row(
        "buy-1", win_rate=0.6, improved_stock_ratio=0.6, closed_trades=80, lift=1.2
    )
    thin = _holdout_row("buy-2", win_rate=0.6, improved_stock_ratio=0.6, closed_trades=5, lift=9.9)

    selected = _select_holdout_candidates([thin, thick], 1)

    assert selected == {"buy-1"}


def test_holdout_selection_breaks_a_full_tie_deterministically() -> None:
    """连成交笔数都一样时，用稳定键定格，别的什么都不看。"""

    first = _holdout_row(
        "buy-1", win_rate=0.6, improved_stock_ratio=0.6, closed_trades=20, lift=1.1
    )
    second = _holdout_row(
        "buy-2", win_rate=0.6, improved_stock_ratio=0.6, closed_trades=20, lift=9.9
    )

    assert _select_holdout_candidates([second, first], 1) == {"buy-1"}
    assert _select_holdout_candidates([first, second], 1) == {"buy-1"}


# --------------------------------------------------------------------------
# 累计试验次数校正（候选池 #20）
# --------------------------------------------------------------------------


def _significant_candidate(pvalue: float) -> MinedCondition:
    return MinedCondition(
        condition=ScreenCondition(left_value="technical.ma5", operator="gte", right_constant=1.0),
        support_count=30,
        support_ratio=0.5,
        instrument_count=5,
        outcome_rate=0.8,
        baseline_rate=0.5,
        lift=1.6,
        lift_threshold=1.2,
        permutation_pvalue=pvalue,
        significant=pvalue <= 0.05,
    )


def test_mining_dict_keeps_raw_pvalue_but_judges_on_the_adjusted_one() -> None:
    """判定用校正后的 p，原始 p 必须留下——否则用户看不出被校正了多少。"""

    from selector_app.condition_research.service import _mining_dict

    candidate = _significant_candidate(0.04)

    single = _mining_dict(candidate, trial_count=1, quantile=0.95)
    assert single["permutation_pvalue"] == pytest.approx(0.04)
    # 只跑过一轮：校正恒等，判定不变。
    assert single["adjusted_pvalue"] == pytest.approx(0.04)
    assert single["significant"] is True

    tenth = _mining_dict(candidate, trial_count=10, quantile=0.95)
    assert tenth["permutation_pvalue"] == pytest.approx(0.04)
    assert tenth["adjusted_pvalue"] == pytest.approx(1 - 0.96**10)
    # 第十轮才挖出来的 0.04 已经不值钱了。
    assert tenth["significant"] is False


def test_mining_dict_leaves_unadjusted_when_no_permutation_ran() -> None:
    """没做置换检验时三个字段必须一起是 None，不能凭空编一个"已校正"。"""

    from selector_app.condition_research.service import _mining_dict

    candidate = _significant_candidate(0.04)
    candidate = replace(candidate, permutation_pvalue=None, significant=None)

    payload = _mining_dict(candidate, trial_count=5, quantile=0.95)

    assert payload["permutation_pvalue"] is None
    assert payload["adjusted_pvalue"] is None
    assert payload["significant"] is None


class FakeTrialStore:
    """只实现台账用到的两个方法，其余一概不碰真数据库。"""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = list(rows or [])
        self.saved: list[dict[str, object]] = []
        self.boom = False

    def save_research_trial(
        self,
        history_id: str,
        *,
        data_fingerprint: str,
        start_date: str,
        end_date: str,
        tested_conditions: int,
        created_at: object = None,
    ) -> None:
        if self.boom:
            raise RuntimeError("台账不可用")
        row = {
            "history_id": history_id,
            "created_at": datetime(2026, 9, 22, 10, 0, 0),
            "data_fingerprint": data_fingerprint,
            "start_date": start_date,
            "end_date": end_date,
            "tested_conditions": tested_conditions,
        }
        self.saved.append(row)
        self.rows.append(row)

    def list_research_trials(self) -> list[dict[str, object]]:
        if self.boom:
            raise RuntimeError("台账不可用")
        return list(self.rows)


def test_run_records_the_study_and_reports_trial_context() -> None:
    """跑完一次研究，台账要落一条，报告要带上累计上下文。"""

    store = FakeTrialStore()
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    assert len(store.saved) == 1
    assert store.saved[0]["start_date"] == "2020-01-01"
    assert store.saved[0]["end_date"] == "2024-12-31"
    # 台账里的条件数必须来自挖掘真实枚举的候选池，不是拍一个数。
    assert store.saved[0]["tested_conditions"] > 0

    trials = result["trials"]
    assert trials["trial_count"] == 1
    assert trials["study_count"] == 1
    # 只有一轮时不该宣称"已校正"。
    assert trials["adjusted"] is False
    assert trials["repeated_studies"] == 0


def test_repeated_studies_on_one_window_stay_one_round() -> None:
    """同区间重跑不增加轮数——这是整个记账设计的立足点。

    实测：本地库 9 次研究区间全是 2021-09-11~2026-09-11，统计上是 1 轮。
    若按次数记账会得到 9 轮，把 p=0.01 也误判成不显著。
    """

    store = FakeTrialStore(
        rows=[
            {
                "history_id": "old-1",
                "created_at": datetime(2026, 9, 21, 10, 0, 0),
                "data_fingerprint": "fp",
                "start_date": "2020-01-01",
                "end_date": "2024-12-31",
                "tested_conditions": 900,
            }
        ]
    )
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    trials = result["trials"]
    assert trials["study_count"] == 2
    # 区间完全重合 → 仍是 1 轮，判定不被抬高。
    assert trials["trial_count"] == 1
    assert trials["adjusted"] is False
    # 但"同一段数据挖了两次"必须说出口。
    assert trials["repeated_studies"] == 1


def test_a_disjoint_earlier_window_raises_the_trial_count() -> None:
    """换一段完全不同的数据再挖，才算第二轮，判定要跟着变严。"""

    store = FakeTrialStore(
        rows=[
            {
                "history_id": "old-1",
                "created_at": datetime(2026, 9, 21, 10, 0, 0),
                "data_fingerprint": "fp",
                "start_date": "2010-01-01",
                "end_date": "2014-12-31",
                "tested_conditions": 900,
            }
        ]
    )
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    trials = result["trials"]
    assert trials["trial_count"] == 2
    assert trials["adjusted"] is True
    assert trials["repeated_studies"] == 0


def test_ledger_failure_never_fails_the_study() -> None:
    """台账是审计信息，写不进去只警告，不能让研究失败。"""

    store = FakeTrialStore()
    store.boom = True
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )

    result = service.run(_rolling_config())

    # 研究照常出结果，且退化为"只跑了一轮"（不校正），不会假装校正过。
    assert result["candidates"]
    assert result["trials"]["trial_count"] == 1
    assert result["trials"]["adjusted"] is False
