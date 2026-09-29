"""按股票名称识别 ST / *ST / 退市标的。

行情库只保存**当前**的股票名称，没有历史名称快照，所以任何"某一天它戴没戴帽"
的判定都做不到。这里退而求其次：名字里带 ``ST`` / ``*ST`` / ``SST`` / ``PT``
或"退"字的，一律视为风险标的。

* 对**实盘**（当下选股、当下买入）这是准确的——现在戴帽现在就不该买。
* 对**历史回测**它偏严格：会把"当时还没戴帽、后来才变成 ST"的股票一起排除。
  这是有意为之——宁可少测一只，也不要让一只迟早要戴帽的股票去美化收益曲线。
  代价是样本会变少，且无法还原"戴帽当天"的真实冲击。
"""

from __future__ import annotations

from typing import Protocol

#: 风险警示前缀（去掉星号后比较，``*ST`` 因此也能命中）。
_RISK_PREFIXES: tuple[str, ...] = ("ST", "SST", "PT")
#: 退市整理期与已退市标的常用后缀（``XX退`` / ``退市XX``）。
_DELISTED_HINTS: tuple[str, ...] = ("退",)


class NamedSymbol(Protocol):
    """任何带 ``name`` 的标的引用（:class:`StockRef`、行情库行对象等）。"""

    @property
    def name(self) -> str | None:
        ...


def is_risk_warning_name(name: str | None) -> bool:
    """判断名称是否属于 ST / *ST / 退市标的。

    名称缺失（``None`` / 空串）时返回 ``False``：宁可放进候选池让策略自己
    淘汰，也不要因为拿不到名字就悄悄丢掉一只股票。
    """

    if not name:
        return False
    text = name.strip().upper().replace(" ", "").replace("*", "")
    if not text:
        return False
    if text.startswith(_RISK_PREFIXES):
        return True
    return any(hint in text for hint in _DELISTED_HINTS)


def is_risk_warning_symbol(symbol: NamedSymbol | None) -> bool:
    """对带 ``name`` 的对象做同样的判定，缺 ``name`` 属性时放行。"""

    if symbol is None:
        return False
    return is_risk_warning_name(getattr(symbol, "name", None))
