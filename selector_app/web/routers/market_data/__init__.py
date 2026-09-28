"""Online market-data synchronization endpoints.

按职责拆成了几个子模块，``app.py`` 仍然只从本包取 ``router``：

- ``indicator_cache`` 指标缓存的查看与清空
- ``precompute``    指标预热任务 + 写路径共用的预热步骤
- ``status``        capabilities / store 两个只读描述接口
- ``jobs``          行情任务的轮询与取消
- ``sync``          在线同步、回填、强制全量重刷及兼容别名
- ``maintenance``   删除已导入的行情
- ``health``        行情体检报告与一键修复
- ``local_import``  本地 vipdoc 导入
- ``local_data``    本地 vipdoc 的只读预览
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    health,
    indicator_cache,
    jobs,
    local_data,
    local_import,
    maintenance,
    precompute,
    status,
    sync,
)
from ._shared import LocalMarketDataServiceLike, MarketSyncService
from .health import create_health_check_job, create_health_repair_job
from .indicator_cache import clear_indicator_cache, indicator_cache_status
from .jobs import (
    active_market_data_jobs,
    cancel_market_data_job,
    market_data_job_status,
)
from .local_data import local_instruments, local_market_bars
from .local_import import create_local_import_job, detect_local_vipdoc
from .maintenance import delete_market_bars
from .precompute import create_indicator_precompute_job
from .status import market_data_capabilities, market_data_store_status
from .sync import (
    create_eastmoney_backfill_job,
    create_full_refresh_job,
    create_online_sync_job,
    create_sync_job,
    sync_job_status,
)

router = APIRouter(prefix="/market-data")

# 顺序沿用了拆分前的文件内顺序：/jobs/active 必须排在 /jobs/{job_id} 之前，
# 否则 "active" 会被当成 job_id 命中后面那条路径。保留显式清单供注册守卫测试，
# 不依赖 FastAPI 的内部 APIRoute 元数据来追溯子路由来源。
_SUBROUTERS = (
    indicator_cache.router,
    status.router,
    precompute.router,
    jobs.router,
    sync.router,
    maintenance.router,
    health.router,
    local_import.router,
    local_data.router,
)
for _sub_router in _SUBROUTERS:
    router.include_router(_sub_router)

del _sub_router

__all__ = [
    "LocalMarketDataServiceLike",
    "MarketSyncService",
    "active_market_data_jobs",
    "cancel_market_data_job",
    "clear_indicator_cache",
    "create_eastmoney_backfill_job",
    "create_full_refresh_job",
    "create_health_check_job",
    "create_health_repair_job",
    "create_indicator_precompute_job",
    "create_local_import_job",
    "create_online_sync_job",
    "create_sync_job",
    "delete_market_bars",
    "detect_local_vipdoc",
    "indicator_cache_status",
    "local_instruments",
    "local_market_bars",
    "market_data_capabilities",
    "market_data_job_status",
    "market_data_store_status",
    "router",
    "sync_job_status",
]
