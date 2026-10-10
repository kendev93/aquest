"""可选的 Numba JIT 支持，供回测内核共用。

numba 是可选加速依赖：没安装时内核按纯 Python 执行，结果完全一致、只是不提速。
内核函数统一写成 numba 兼容子集，JIT 与纯 Python 两条路径共用同一份实现，
避免双实现漂移；差分测试负责逐笔比对两条路径。

内核是否真的跑起来，靠 :func:`record_outcome` 埋点：命中记 ``hit``，
回退按原因分别计数并首次告警。守卫回退原本是静默的，排查时看不出内核有没有生效。
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

__all__ = [
    "NUMBA_AVAILABLE",
    "fast_sim_enabled",
    "fast_sim_stats",
    "njit",
    "record_outcome",
]

_F = TypeVar("_F", bound=Callable[..., Any])

_LOGGER = logging.getLogger(__name__)
_OUTCOMES: Counter[str] = Counter()
_WARNED: set[str] = set()

try:  # pragma: no cover - 取决于环境是否安装 numba
    from numba import njit as _numba_njit

    NUMBA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _numba_njit = cast(Callable[..., Any], None)
    NUMBA_AVAILABLE = False


def _identity_jit(*args: Any, **kwargs: Any) -> Any:
    """numba 缺失时的占位装饰器，接受与 numba.njit 相同的调用形式。"""

    del kwargs
    if args and callable(args[0]):
        return args[0]

    def wrap(func: _F) -> _F:
        return func

    return wrap


if TYPE_CHECKING:  # pragma: no cover - 仅为类型检查

    def njit(*args: Any, cache: bool = False, **kwargs: Any) -> Callable[[_F], _F]:
        """与 ``numba.njit`` 同形的装饰器工厂（项目只使用 ``njit(cache=True)`` 形式）。"""

else:
    njit: Callable[..., Any] = (
        cast(Callable[..., Any], _numba_njit) if _numba_njit is not None else _identity_jit
    )


def fast_sim_enabled() -> bool:
    """内核路径总开关；``SELECTOR_FAST_SIM=0`` 可整体关闭（排查问题时用）。"""

    return os.environ.get("SELECTOR_FAST_SIM", "1") != "0"


def record_outcome(path: str, outcome: str) -> None:
    """记一次内核命中/回退。``outcome`` 为 ``hit`` 表示命中，其余为回退原因。

    回退原因首次出现时打一条 warning：内核长期没生效往往就是某个守卫在静默拦截，
    日志里必须留痕，否则只能靠猜。
    """

    key = f"{path}:{outcome}"
    _OUTCOMES[key] += 1
    if outcome == "hit" or key in _WARNED:
        return
    _WARNED.add(key)
    _LOGGER.warning("Numba 内核回退：%s（同类原因只告警一次）", key)


def fast_sim_stats(*, reset: bool = False) -> dict[str, int]:
    """返回各内核路径的命中/回退计数，供排查与基准脚本读取。"""

    snapshot = {key: int(value) for key, value in sorted(_OUTCOMES.items())}
    if reset:
        _OUTCOMES.clear()
    return snapshot
