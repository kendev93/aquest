"""Environment-driven settings for the application factory.

这些开关的取值规则（显式入参 → 环境变量 → 默认值）与业务装配无关，单独放一
层，免得 ``create_app`` 里几百行里只有几行是真正的选择逻辑。
"""

from __future__ import annotations

import logging
import math
import os
import secrets
from pathlib import Path

from selector_app.market_data.auto_sync import DEFAULT_AUTO_SYNC_INTERVAL_SECONDS
from selector_app.portfolio_backtest.auto_tracking import DEFAULT_TRACKING_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


def _resolve_strategy_worker_token(value: str | None) -> str | None:
    if value is not None:
        return value.strip() or None
    token_path = str(os.getenv("SELECTOR_STRATEGY_WORKER_TOKEN_FILE") or "").strip()
    if not token_path:
        return None
    path = Path(token_path).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _generate_strategy_worker_token(path)
    except OSError:
        logger.exception("策略 worker token 文件不可读")
        return None
    return token or None


def _generate_strategy_worker_token(path: Path) -> str | None:
    """Create the worker token on first boot of a self-hosted installation.

    Generation stays opt-in: a deployment that mounts a token from elsewhere
    should fail loudly when the file is missing rather than silently start with
    a different secret than its worker.
    """

    if str(os.getenv("SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        logger.warning("策略 worker token 文件不存在: %s", path)
        return None
    if not path.parent.is_dir():
        logger.warning("策略 worker token 目录不存在，无法生成: %s", path.parent)
        return None
    token = secrets.token_hex(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
    except OSError:
        logger.exception("策略 worker token 生成失败: %s", path)
        return None
    logger.info("已生成策略 worker token: %s", path)
    return token


def _resolve_strategy_tracking_enabled(value: bool | None) -> bool:
    if value is not None:
        if not isinstance(value, bool):
            raise ValueError("策略追踪开关必须是布尔值")
        return value
    raw = os.getenv("SELECTOR_STRATEGY_TRACKING_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_strategy_tracking_interval(value: float | None) -> float:
    if value is None:
        raw = os.getenv("SELECTOR_STRATEGY_TRACKING_CHECK_INTERVAL_SECONDS")
        if raw is None:
            return float(DEFAULT_TRACKING_INTERVAL_SECONDS)
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("策略追踪检查间隔必须是数字") from exc
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("策略追踪检查间隔必须大于 0")
    return float(value)


def _resolve_auto_sync_enabled(value: bool | None) -> bool:
    if value is not None:
        if not isinstance(value, bool):
            raise ValueError("自动行情同步开关必须是布尔值")
        return value
    raw = os.getenv("SELECTOR_AUTO_SYNC_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_auto_sync_interval(value: float | None) -> float:
    if value is None:
        raw = os.getenv("SELECTOR_AUTO_SYNC_CHECK_INTERVAL_SECONDS")
        if raw is None:
            return float(DEFAULT_AUTO_SYNC_INTERVAL_SECONDS)
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("自动行情同步检查间隔必须是数字") from exc
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("自动行情同步检查间隔必须大于 0")
    return float(value)
