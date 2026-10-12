import pytest

from aquest.indicators.precompute import PRECOMPUTE_MAX_BARS, should_defer_precompute


def test_large_indicator_precompute_is_deferred_before_loading_all_bars() -> None:
    assert should_defer_precompute(PRECOMPUTE_MAX_BARS) is False
    assert should_defer_precompute(PRECOMPUTE_MAX_BARS + 1) is True


@pytest.mark.parametrize("value", [-1, True])
def test_indicator_precompute_bar_count_must_be_a_nonnegative_integer(value) -> None:
    with pytest.raises(ValueError):
        should_defer_precompute(value)
