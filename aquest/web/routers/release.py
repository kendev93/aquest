"""上游版本状态：顶栏那条可关闭提示的唯一数据来源。

检查由后台任务按天做，这里只把缓存的答案交出去——一次页面加载不该为出网等待，
更不该因为 GitHub 不可达就在页面上亮起一条错误。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from aquest import __version__
from aquest.release_check import (
    DEFAULT_RELEASE_REPOSITORY,
    ReleaseChecker,
    ReleaseStatus,
)

router = APIRouter(tags=["release"])


@router.get("/release-status")
def release_status(request: Request) -> dict[str, object]:
    """当前版本、上游最新版本，以及要不要提示升级。"""

    checker = getattr(request.app.state, "release_checker", None)
    if not isinstance(checker, ReleaseChecker):
        # worker 进程或嵌入了 app 但没有装配检查器的调用方：说清「这台机器没在做检查」。
        return {
            "data": ReleaseStatus(
                current=__version__,
                repository=DEFAULT_RELEASE_REPOSITORY,
                enabled=False,
            ).to_dict()
        }
    return {"data": checker.status().to_dict()}
