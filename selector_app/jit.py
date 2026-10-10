"""可选的 Numba JIT 支持，供回测内核共用。

numba 是可选加速依赖：没安装时内核按纯 Python 执行，结果完全一致、只是不提速。
内核函数统一写成 numba 兼容子集，JIT 与纯 Python 两条路径共用同一份实现，
避免双实现漂移；差分测试负责逐笔比对两条路径。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

__all__ = ["NUMBA_AVAILABLE", "fast_sim_enabled", "njit"]

_F = TypeVar("_F", bound=Callable[..., Any])

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
