"""Call online market-data sync services across protocol variants.

自动调度、手动路由与健康修复拿到的服务实现不完全相同：有的 ``sync`` 接受
``abort_check``，有的只接受进度回调。调用方只关心结果，不应该各自复制一份
签名探测。
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, signature
from typing import Protocol

from aquest.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    MarketSyncConfig,
    MarketSyncReport,
)


class MarketSyncService(Protocol):
    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport | dict[str, object]: ...


def supports_abort_check(method: object, *, accept_var_keyword: bool = False) -> bool:
    """Whether a callable's signature can receive ``abort_check``."""

    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "abort_check"
        and (
            parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
            or (accept_var_keyword and parameter.kind is Parameter.VAR_KEYWORD)
        )
        for parameter in parameters
    )


def call_market_sync(
    service: MarketSyncService,
    config: MarketSyncConfig,
    progress: Callable[[int, int], None],
    *,
    detail_callback: DetailCallback,
    abort_check: AbortCheck | None,
) -> MarketSyncReport | dict[str, object]:
    """Call ``service.sync`` with the strongest supported arguments."""

    if abort_check is not None and supports_abort_check(service.sync):
        return service.sync(
            config,
            progress,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )
    return service.sync(config, progress, detail_callback=detail_callback)
