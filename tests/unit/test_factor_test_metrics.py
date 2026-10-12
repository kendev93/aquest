"""Forward-return factor evidence: what the numbers must say."""

from __future__ import annotations

import numpy as np

from aquest.factor_test.metrics import (
    annotate_significance,
    correlation_gram,
    evaluate_factor,
)
from aquest.factor_test.models import FactorTestConfig

STOCKS = 120
DAYS = 240


def _config(**overrides: object) -> FactorTestConfig:
    defaults: dict[str, object] = {
        "horizon": 5,
        "min_cross_section": 20,
        "permutations": 60,
        "block_size": 20,
        "quantile_count": 5,
    }
    defaults.update(overrides)
    return FactorTestConfig(**defaults).normalized()  # type: ignore[arg-type]


def _panel(
    scale: float = 0.04,
    *,
    noise: float = 0.02,
    seed: int = 7,
) -> tuple[np.ndarray, np.ndarray]:
    """A value that genuinely predicts, and the forward return it predicts."""

    rng = np.random.default_rng(seed)
    signal = rng.normal(size=(STOCKS, DAYS))
    forward = scale * signal + rng.normal(scale=noise, size=(STOCKS, DAYS))
    return signal, forward


def _noise_panel(seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    return rng.normal(size=(STOCKS, DAYS)), rng.normal(size=(STOCKS, DAYS))


def test_a_predictive_value_scores_a_strong_stable_ic() -> None:
    values, forward = _panel()
    evidence = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())

    assert evidence.ic_mean > 0.3
    assert evidence.ic_days == DAYS
    assert evidence.icir is not None and evidence.icir > 1.0
    assert evidence.ic_positive_rate is not None and evidence.ic_positive_rate > 0.9
    assert evidence.pvalue is not None and evidence.pvalue < 0.05


def test_layer_returns_are_monotonic_and_oriented_to_the_ic_sign() -> None:
    """Layer 1 is the group the IC says to buy, so its return is the largest."""

    values, forward = _panel()
    evidence = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())

    assert evidence.order == "high"
    means = [layer.mean_return for layer in evidence.layers]
    assert all(item is not None for item in means)
    ordered = [float(item) for item in means if item is not None]
    assert ordered == sorted(ordered, reverse=True)
    assert evidence.layer_inversions == 0
    assert evidence.long_short.mean_spread is not None
    assert evidence.long_short.mean_spread > 0
    assert evidence.long_short.win_rate is not None
    assert evidence.long_short.win_rate > 0.9


def test_a_negative_ic_is_reported_as_buying_the_low_end() -> None:
    """Upside-down is still a finding; only the direction it is traded flips."""

    values, forward = _panel()
    evidence = evaluate_factor(-values, forward, value_id="f", display_name="f", config=_config())

    assert evidence.order == "low"
    assert evidence.ic_mean > 0.3
    means = [float(item) for item in [layer.mean_return for layer in evidence.layers] if item]
    assert means == sorted(means, reverse=True)
    assert evidence.long_short.mean_spread is not None
    assert evidence.long_short.mean_spread > 0


def test_noise_does_not_clear_the_permutation_floor() -> None:
    values, forward = _noise_panel()
    evidence = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())

    assert evidence.ic_mean < 0.05
    # The whole point of the noise floor: the observed IC should be the same
    # order of magnitude as what reshuffled calendars produce.
    assert evidence.ic_threshold is not None
    assert evidence.ic_mean < evidence.ic_threshold * 3


def test_days_with_too_few_stocks_are_not_scored() -> None:
    values, forward = _panel()
    usable = np.ones(DAYS, dtype=bool)
    usable[:10] = False
    values[:, :10] = np.nan
    evidence = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())

    assert evidence.ic_days == DAYS - 10
    assert evidence.ic_series[:10] == (None,) * 10


def test_a_missing_future_drops_the_day_instead_of_scoring_it_zero() -> None:
    """The last `horizon` days have no label; they must not become zeros."""

    values, forward = _panel()
    forward[:, -8:] = np.nan
    evidence = evaluate_factor(
        values, forward, value_id="f", display_name="f", config=_config(horizon=5)
    )

    assert evidence.ic_days == DAYS - 8
    assert evidence.ic_series[-1] is None


def test_long_short_effective_sample_size_shrinks_with_the_horizon() -> None:
    """Overlapping windows are not independent: a longer horizon is fewer trials."""

    values, forward = _panel()
    short = evaluate_factor(
        values, forward, value_id="f", display_name="f", config=_config(horizon=5)
    )
    long = evaluate_factor(
        values, forward, value_id="f", display_name="f", config=_config(horizon=20)
    )

    assert short.long_short.t_stat is not None
    assert long.long_short.t_stat is not None
    assert abs(long.long_short.t_stat) < abs(short.long_short.t_stat)


def test_halves_only_disagree_when_their_signs_disagree() -> None:
    """Magnitude is not the test: two halves can both be weak yet still agree.

    The halves are stored absolute (the sign is already spent choosing
    ``order``), but "方向相反" is a statement about signs - reading it off
    magnitudes would make the warning unfireable.
    """

    values, forward = _panel()
    forward[:, DAYS // 2 :] = -forward[:, DAYS // 2 :]
    flipped = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())
    assert flipped.ic_first_half is not None and flipped.ic_second_half is not None
    assert flipped.ic_first_half > 0.3
    assert flipped.ic_second_half > 0.3
    assert "前后半段方向相反" in flipped.note

    stable = evaluate_factor(*_panel(), value_id="f", display_name="f", config=_config())
    assert stable.ic_first_half is not None and stable.ic_second_half is not None
    assert "前后半段同向" in stable.note


def test_a_flat_ladder_is_not_counted_as_inverted() -> None:
    """Neighbouring layers differ by less than a basis point on real data.

    Counting every upward tick flags a flat ladder as "收益挤在极端档", and a
    warning that fires on noise is worse than no warning: it still looks like a
    finding. The tolerance is a fraction of the ladder's own span.
    """

    per_layer = STOCKS // 5
    values = np.tile(np.arange(STOCKS, dtype=float)[:, None], (1, DAYS))
    # Layer 1 is the largest end: the first four layers step *up* by 1e-7, far
    # below the 5%-of-span tolerance, and only the last layer really separates.
    ladder = [0.01, 0.0100001, 0.0100002, 0.0100003, 0.0]
    forward = np.zeros((STOCKS, DAYS))
    for layer_index, reward in enumerate(ladder):
        start = STOCKS - (layer_index + 1) * per_layer
        forward[start : start + per_layer, :] = reward
    evidence = evaluate_factor(
        values, forward, value_id="f", display_name="f", config=_config(permutations=0)
    )

    assert evidence.layer_inversions == 0
    assert "倒挂" not in evidence.note


def test_a_coin_flip_spread_is_said_out_loud() -> None:
    """A ranking can clear its noise floor and still not be executable."""

    noise_values, noise_forward = _noise_panel()
    evidence = evaluate_factor(
        noise_values, noise_forward, value_id="f", display_name="f", config=_config()
    )

    assert evidence.long_short.win_rate is not None
    assert evidence.long_short.win_rate < 0.55
    assert "接近抛硬币" in evidence.note


def test_an_empty_panel_reports_why() -> None:
    values, forward = _panel()
    evidence = evaluate_factor(
        values, np.full_like(forward, np.nan), value_id="f", display_name="f", config=_config()
    )

    assert evidence.ic_days == 0
    assert evidence.ic_series == ()
    assert "没有" in evidence.note


def test_layer_sizes_are_reported_so_a_thin_bucket_is_visible() -> None:
    values, forward = _panel()
    evidence = evaluate_factor(values, forward, value_id="f", display_name="f", config=_config())

    assert all(layer.size is not None for layer in evidence.layers)
    assert abs(sum(float(layer.size or 0) for layer in evidence.layers) - STOCKS) < 5


def test_the_correlation_gram_diagonal_is_the_observed_ic() -> None:
    """The permutation test reads other cells of the same matrix."""

    values, forward = _panel()
    correlation, counts, _mask = correlation_gram(values, forward)
    usable = counts >= 20
    assert correlation.shape == (DAYS, DAYS)
    assert np.isfinite(np.diag(correlation)[usable]).all()
    assert abs(float(np.mean(np.diag(correlation)[usable]))) > 0.3


def test_significance_is_controlled_across_the_whole_study() -> None:
    values, forward = _panel()
    noise_values, noise_forward = _noise_panel()
    evidence = [
        evaluate_factor(values, forward, value_id="real", display_name="real", config=_config()),
        evaluate_factor(
            noise_values, noise_forward, value_id="noise", display_name="noise", config=_config()
        ),
    ]
    adjusted = annotate_significance(evidence, alpha=0.05)

    by_id = {item.value_id: item for item in adjusted}
    assert by_id["real"].significant is True
    assert by_id["noise"].significant is False
    assert by_id["real"].qvalue is not None and by_id["real"].qvalue <= 0.05
