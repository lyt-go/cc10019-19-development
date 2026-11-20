"""生命周期价值路由：执行LTV预测并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.lifetime_value import (
    predict_ltv,
    get_ltv_result,
    ltv_with_retention,
    calibrate_ltv_with_cohorts,
)
from app.store import store

router = APIRouter(prefix="/api/lifetime-value", tags=["lifetime_value"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("lifetime_value")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("lifetime_value")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("lifetime_value", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{model_id}/run")
def run(model_id: str) -> ActionResult:
    try:
        result = predict_ltv(model_id)
        return ActionResult(ok=True, message="Predicted", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{model_id}/result")
def result(model_id: str) -> dict[str, Any]:
    res = get_ltv_result(model_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{model_id}/calibrate")
def calibrate(model_id: str, payload: dict[str, Any]) -> ActionResult:
    retention_config_id = payload.get("retention_config_id", "")
    try:
        result = calibrate_ltv_with_cohorts(
            model_id,
            retention_config_id,
            as_of_date=payload.get("as_of_date") or None,
            basis_day_age=payload.get("basis_day_age"),
            version=payload.get("version") or None,
        )
        return ActionResult(ok=True, message="Calibrated", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("lifetime_value", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
