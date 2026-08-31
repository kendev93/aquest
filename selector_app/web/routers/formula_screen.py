"""Formula screening endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from selector_app.formulas.custom import FormulaParseError, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.screening.export import report_to_csv, report_to_json
from selector_app.screening.jobs import ScreenJobRunner
from selector_app.screening.models import ScanReport

from ..schemas import (
    CustomFormulaParseRequest,
    FormulaScreenRequest,
)

router = APIRouter(prefix="/formula-screen", tags=["formula-screen"])


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


@router.get("/metadata")
def metadata() -> dict[str, object]:
    return {
        "data": {
            "indicators": FORMULA_REGISTRY.metadata(),
            "combine_modes": [
                {"value": "all", "label": "全部满足 AND"},
                {"value": "any", "label": "任一满足 OR"},
                {"value": "at_least", "label": "至少满足 N 个"},
            ],
            "condition_operators": [
                {"value": "gt", "label": "大于"},
                {"value": "gte", "label": "大于等于"},
                {"value": "lt", "label": "小于"},
                {"value": "lte", "label": "小于等于"},
                {"value": "eq", "label": "等于"},
                {"value": "neq", "label": "不等于"},
                {"value": "cross_up", "label": "上穿"},
                {"value": "cross_down", "label": "下穿"},
            ],
            "supported_markets": ["SH", "SZ"],
            "supported_universe": [
                {"value": "all", "label": "沪深全部品种"},
                {"value": "sh", "label": "仅上海品种"},
                {"value": "sz", "label": "仅深圳品种"},
                {"value": "custom", "label": "自定义品种列表"},
            ],
            "periods": [{"value": "daily", "label": "日线"}],
            "data_directory_help": (
                "行情统一读取本地 DuckDB；请先在本地行情页面导入通达信 vipdoc 数据。"
            ),
        }
    }


@router.post("/parse")
def parse_custom_formula(payload: CustomFormulaParseRequest) -> JSONResponse:
    try:
        parsed = parse_formula(payload.formula_text)
    except FormulaParseError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "formula_parse_error", "message": str(exc)}},
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": parsed.metadata()})


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
def create_job(payload: FormulaScreenRequest, request: Request) -> JSONResponse:
    try:
        if payload.universe == "custom" and payload.universe_file:
            if not Path(payload.universe_file).expanduser().is_file():
                raise ValueError(f"自定义股票列表文件不存在: {payload.universe_file}")
        job_id = _runner(request).submit(payload.to_config())
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"data": state.snapshot()}


def _completed_report(job_id: str, request: Request) -> ScanReport:
    state = _runner(request).get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if state.status != "completed" or state.report is None:
        if state.status == "failed":
            raise HTTPException(status_code=500, detail="扫描任务失败，请检查任务状态中的错误提示")
        raise HTTPException(status_code=409, detail="扫描尚未完成")
    return state.report


@router.get("/jobs/{job_id}/results")
def job_results(job_id: str, request: Request) -> dict[str, object]:
    report = _completed_report(job_id, request)
    return {"data": [result.to_dict() for result in report.results], "meta": report.summary_dict()}


@router.get("/jobs/{job_id}/export.json")
def export_json(job_id: str, request: Request) -> Response:
    report = _completed_report(job_id, request)
    return Response(content=report_to_json(report), media_type="application/json")


@router.get("/jobs/{job_id}/export.csv")
def export_csv(job_id: str, request: Request) -> Response:
    report = _completed_report(job_id, request)
    return Response(
        content=report_to_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="formula-screen-{job_id}.csv"'},
    )
