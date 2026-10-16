"""成交指纹只有一份实现：寻优与回放必须算出同一个串。

这两件工具曾经在寻优与回放两侧各抄一份并且已经漂移（回放那份遇到非列表输入直接
抛异常），而「回放与寻优一致」的判定就建立在这个串上。这里钉住「同一实现」与
「口径本身」。
"""

from __future__ import annotations

from aquest.strategy_discovery import replay, service
from aquest.strategy_discovery.fingerprints import trade_fingerprint


def test_search_and_replay_share_one_fingerprint_implementation() -> None:
    assert service.trade_fingerprint is trade_fingerprint
    assert replay.trade_fingerprint is trade_fingerprint


def test_field_order_inside_a_trade_does_not_change_the_fingerprint() -> None:
    left = [{"date": 20240105, "direction": "BUY", "size": 100, "price": 10.0}]
    right = [{"date": 20240105, "price": 10.0, "size": 100, "direction": "BUY"}]

    assert trade_fingerprint(left) == trade_fingerprint(right)


def test_trade_sequence_still_changes_the_fingerprint() -> None:
    one = [{"date": 20240105, "direction": "BUY", "size": 100, "price": 10.0}]
    other = [{"date": 20240108, "direction": "SELL", "size": 100, "price": 11.0}]

    assert trade_fingerprint(one + other) != trade_fingerprint(other + one)


def test_float_tail_digits_are_not_a_different_trade() -> None:
    assert trade_fingerprint([{"price": 10.0}]) == trade_fingerprint([{"price": 10.0 + 1e-15}])


def test_missing_trade_list_is_empty_not_a_crash() -> None:
    """回放侧原来是 `for trade in trades` 直接迭代：None 会抛异常。"""

    assert trade_fingerprint(None) == ""
    assert trade_fingerprint({"date": 20240105}) == ""


def test_non_mapping_rows_are_skipped_rather_than_hashed() -> None:
    base = [{"date": 20240105}]

    assert trade_fingerprint(base + ["脏行"]) == trade_fingerprint(base)
