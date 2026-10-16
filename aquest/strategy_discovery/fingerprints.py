"""候选成交指纹：寻优与回放必须算出同一个字符串。

这两件工具原先在 `service.py`（寻优）与 `replay.py`（回放复核）各抄了一份，并且
已经漂移：寻优那份对「不是列表」的输入返回空串，回放那份直接迭代、遇到 None 就抛
异常。同一件事留两份实现，意味着「回放结果与寻优一致」这类比较有可能其实在比两套
口径，所以只留这一处。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

#: 参与指纹的成交字段。增删字段会改变所有历史条目的指纹，属于口径变更。
TRADE_FINGERPRINT_FIELDS = (
    "board",
    "code",
    "commission",
    "cooldown_days",
    "cost_basis",
    "date",
    "direction",
    "instrument_type",
    "market",
    "pnl",
    "price",
    "reason",
    "risk_trigger",
    "signal_date",
    "size",
    "slippage",
    "stamp_tax",
)


def fingerprint_value(value: object) -> object:
    """数值统一成 15 位有效数字的文本，避免浮点末位差异被当成另一笔成交。"""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return format(value, ".15g")
    return str(value)


def trade_fingerprint(trades: object) -> str:
    """按成交顺序折叠指纹；拿不到成交列表就是「没有可比对的成交」，返回空串。"""

    if not isinstance(trades, (list, tuple)):
        return ""
    rows: list[dict[str, object]] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        rows.append(
            {field: fingerprint_value(trade.get(field)) for field in TRADE_FINGERPRINT_FIELDS}
        )
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
