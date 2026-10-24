"""Error-to-JSON translation for the HTTP layer.

全站统一把异常压成 ``{"error": {"code", "message"}}``：FastAPI 默认会把
422 校验错误和 HTTPException 抛成另一种形状，前端按一种形状解析会漏。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class TaskFailedError(Exception):
    """后台任务已经结束但失败了：结果永远不会有。

    它不是「服务器坏了」——任务跑完了，原因写在任务自己的错误里，所以统一回 409；
    真正的服务故障走请求链路上的 5xx。``message`` 由调用方给出（任务错误或该路由的
    兜底文案），``app_errors`` 会把它放进 ``{"error": {"code": "task_failed"}}``。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TaskFailedError)
    async def task_failed(_: Request, exc: TaskFailedError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "task_failed", "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [error.get("msg", "请求参数无效") for error in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "；".join(messages),
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code_by_status = {
            404: "not_found",
            409: "conflict",
            422: "validation_error",
            503: "service_unavailable",
        }
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code_by_status.get(exc.status_code, "request_error"),
                    "message": message,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的 API 异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务器内部错误，请稍后重试"}},
        )
