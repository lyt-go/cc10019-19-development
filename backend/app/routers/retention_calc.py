"""留存计算路由：执行留存计算并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.retention_calc import (
    calculate_retention,
    get_retention_result,
    get_cohort_detail,
    confirm_cohort_size,
)
from app.store import store

router = APIRouter(prefix="/api/retention-calc", tags=["retention_calc"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("retention_calc")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("retention_calc")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("retention_calc", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/run")
def run(config_id: str) -> ActionResult:
    try:
        result = calculate_retention(config_id)
        return ActionResult(ok=True, message="Calculated", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_retention_result(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{config_id}/cohort-detail")
def cohort_detail(config_id: str, as_of_date: str | None = None,
                  cohort_date: str | None = None, channel: str | None = None,
                  day_age: int | None = None, version: str | None = None) -> dict[str, Any]:
    """按观察天龄查询留存队列明细：跨入组批次时间对齐，区分未观察到与确认的零留存。"""
    try:
        return get_cohort_detail(config_id, as_of_date=as_of_date, cohort_date=cohort_date,
                                 channel=channel, day_age=day_age, version=version)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{config_id}/cohort-size/confirm")
def confirm_size(config_id: str, payload: dict[str, Any]) -> ActionResult:
    """确认一个入组批次的入组人数，按版本登记，供明细查询回查。"""
    try:
        snapshot = confirm_cohort_size(
            config_id,
            cohort_date=payload.get("cohort_date", ""),
            channel=payload.get("channel", ""),
            cohort_size=int(payload.get("cohort_size", 0)),
            version=payload.get("version", ""),
        )
        return ActionResult(ok=True, message="Confirmed", entry=snapshot)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("retention_calc", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
