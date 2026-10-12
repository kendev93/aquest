"""回测历史类型散在三个模块，新增一种时必须同步。

这个测试存在的唯一理由：让「加了一种回测类型但漏改某一处」在 CI 阶段失败，
而不是等到运行时才发现历史存得进去、读不出来，或者界面上显示原始英文 key。
"""

from typing import get_args

from aquest.history_compare.models import TYPE_LABELS
from aquest.market_data.models import BacktestHistoryType
from aquest.market_data.store_base import _BACKTEST_HISTORY_TYPES
from aquest.web.backtest_history import BacktestHistoryType as WebBacktestHistoryType


def test_store_set_is_derived_from_the_literal() -> None:
    """store 的校验集合必须等于类型本体，不能是抄下来的第二份字面量。"""

    assert _BACKTEST_HISTORY_TYPES == set(get_args(BacktestHistoryType))


def test_web_reexports_the_shared_literal() -> None:
    """web 层不许自己再写一份 Literal，否则两边会各自漂移。"""

    assert WebBacktestHistoryType is BacktestHistoryType


def test_history_compare_labels_every_type() -> None:
    """每种类型都要有中文名，缺一个界面就会漏出英文 key。"""

    assert set(TYPE_LABELS) == _BACKTEST_HISTORY_TYPES
    assert all(value.strip() for value in TYPE_LABELS.values())
