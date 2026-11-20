"""LTV预测引擎：基于用户分群和历史行为预测生命周期价值。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.schemas import LtvResult
from app.services.retention_calc import (
    get_retention_result,
    get_cohort_detail,
    get_cohort_members,
)


def predict_ltv(model_id: str) -> LtvResult:
    """执行LTV预测计算。"""
    model = store.find_by_field("lifetime_value", "model_id", model_id)
    if model is None:
        raise ValueError(f"LTV model not found: {model_id}")

    segment_id = model["segment_id"]
    value_metric = model["value_metric"]
    predict_window = model["predict_window"]
    decay_factor = model["decay_factor"]
    history_baseline = model["history_baseline"]

    # 获取分群信息
    segment = store.find_by_field("user_segment", "segment_id", segment_id)
    if segment is None:
        raise ValueError(f"Segment not found: {segment_id}")

    # 模拟历史基线数据
    base_size = segment.get("size_estimate", 1000)

    # 生成预测值
    predicted_values: list[dict[str, Any]] = []
    total_ltv = 0.0

    for user_idx in range(1, min(base_size + 1, 101)):  # 限制计算量
        user_id = f"U{user_idx:05d}"
        # 基于历史基线生成初始值
        if value_metric == "revenue":
            base_value = 50.0 + (user_idx % 20) * 10.0
        elif value_metric == "order_count":
            base_value = 1.0 + (user_idx % 10)
        else:  # active_days
            base_value = 3.0 + (user_idx % 15)

        # 应用衰减因子预测未来价值
        predicted = base_value * (decay_factor ** (predict_window / 30))
        predicted_values.append({
            "user_id": user_id,
            "base_value": round(base_value, 2),
            "predicted_value": round(predicted, 2),
            "predict_window_days": predict_window,
        })
        total_ltv += predicted

    avg_ltv = total_ltv / len(predicted_values) if predicted_values else 0.0

    # 模拟校准误差
    calibration_error = 0.05 + (1.0 - decay_factor) * 0.2

    result = LtvResult(
        model_id=model_id,
        segment_id=segment_id,
        predict_window=predict_window,
        predicted_values=predicted_values,
        avg_ltv=round(avg_ltv, 2),
        calibration_error=round(calibration_error, 4),
    )

    # 存储产物
    store.set_artifact("lifetime_value", model_id, result.model_dump())

    # 更新状态
    store.update("lifetime_value", model["id"], {
        "status": "已就绪",
        "pending": False,
    })

    return result


def get_ltv_result(model_id: str) -> dict[str, Any] | None:
    """获取LTV预测结果。"""
    artifact = store.get_artifact("lifetime_value", model_id)
    if artifact:
        return artifact
    try:
        result = predict_ltv(model_id)
        return result.model_dump()
    except ValueError:
        return None


def ltv_with_retention(model_id: str, retention_config_id: str) -> dict[str, Any]:
    """结合留存率校准LTV预测。"""
    ltv_result = get_ltv_result(model_id)
    retention_result = get_retention_result(retention_config_id)

    if ltv_result is None or retention_result is None:
        raise ValueError("Result not found")

    # 使用留存率调整预测
    retention_rates = retention_result.get("retention_rates", {})
    avg_retention = 0.0
    if retention_rates:
        first_key = list(retention_rates.keys())[0]
        rates = retention_rates[first_key]
        avg_retention = sum(rates) / len(rates) if rates else 0.0

    adjusted_ltv = ltv_result["avg_ltv"] * (0.5 + 0.5 * avg_retention)

    return {
        "model_id": model_id,
        "retention_config_id": retention_config_id,
        "original_ltv": ltv_result["avg_ltv"],
        "retention_adjusted_ltv": round(adjusted_ltv, 2),
        "avg_retention_rate": round(avg_retention, 4),
    }


# ========== 基于留存队列明细的校准 ==========


def _member_base_value(value_metric: str, member_idx: int) -> float:
    """与 predict_ltv 一致的会员基线价值公式。"""
    if value_metric == "revenue":
        return 50.0 + (member_idx % 20) * 10.0
    if value_metric == "order_count":
        return 1.0 + (member_idx % 10)
    return 3.0 + (member_idx % 15)  # active_days


def calibrate_ltv_with_cohorts(model_id: str, retention_config_id: str,
                               as_of_date: str | None = None,
                               basis_day_age: int | None = None,
                               version: str | None = None) -> dict[str, Any]:
    """消费留存队列明细校准LTV预测。

    返回实际参与估值的组别、观察区间和对应分母：
    - 只有 basis_day_age（默认留存窗口，即“第七天”）之前全部天龄都已观察
      到的组别才参与估值（status=included）；
    - 观察不足的组别列入 pending_groups（awaiting_revisit），其未知天龄
      不计成零回访，估值等待后续回访；
    - 分母（入组人数）按版本回查确认过的记录。
    """
    model = store.find_by_field("lifetime_value", "model_id", model_id)
    if model is None:
        raise ValueError(f"LTV model not found: {model_id}")
    ltv_result = get_ltv_result(model_id)
    if ltv_result is None:
        raise ValueError(f"LTV result not found: {model_id}")

    detail = get_cohort_detail(retention_config_id, as_of_date=as_of_date, version=version)
    basis = basis_day_age if basis_day_age is not None else int(detail["basis_day_age"])
    value_metric = model["value_metric"]

    # 按入组批次（入组日 × 渠道）归集天龄网格
    group_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in detail["items"]:
        if row["day_age"] > basis:
            continue
        group_rows.setdefault((row["cohort_date"], row["channel"]), []).append(row)

    groups: list[dict[str, Any]] = []
    pending_groups: list[dict[str, Any]] = []
    user_valuations: list[dict[str, Any]] = []

    for (cohort_date, channel) in sorted(group_rows):
        rows = sorted(group_rows[(cohort_date, channel)], key=lambda r: r["day_age"])
        observed = [r for r in rows if r["status"] == "observed"]
        observed_day_ages = [r["day_age"] for r in observed]
        missing_day_ages = [r["day_age"] for r in rows if r["status"] == "unobserved"]
        denominator = rows[0]["cohort_size"]
        size_confirmed = rows[0]["size_confirmed"]
        # 仅按已观察天龄取平均，未知天龄不计成零回访
        avg_retention = (
            round(sum(r["retention_rate"] for r in observed) / len(observed), 4)
            if observed else None
        )
        basis_covered = not missing_day_ages and (observed_day_ages and max(observed_day_ages) >= basis)
        observation_window = {
            "start": cohort_date,
            "end": max((r["observe_date"] for r in observed), default=None),
            "max_day_age": max(observed_day_ages) if observed_day_ages else None,
            "observed_day_ages": observed_day_ages,
            "unobserved_day_ages": missing_day_ages,
        }
        entry = {
            "cohort_date": cohort_date,
            "channel": channel,
            "version": detail["version"],
            "denominator": denominator,
            "size_confirmed": size_confirmed,
            "observation_window": observation_window,
            "basis_covered": basis_covered,
            "observed_rates": [
                {"day_age": r["day_age"], "retained_users": r["retained_users"],
                 "retention_rate": r["retention_rate"]}
                for r in observed
            ],
            "avg_retention_rate": avg_retention,
        }
        if basis_covered and avg_retention is not None:
            factor = 0.5 + 0.5 * avg_retention
            members = get_cohort_members(retention_config_id, cohort_date, channel)
            adjusted_sum = 0.0
            for idx, uid in enumerate(members, start=1):
                base_value = _member_base_value(value_metric, idx)
                adjusted = round(base_value * factor, 2)
                adjusted_sum += adjusted
                user_valuations.append({
                    "user_id": uid,
                    "cohort_date": cohort_date,
                    "channel": channel,
                    "base_value": round(base_value, 2),
                    "adjusted_value": adjusted,
                })
            entry["status"] = "included"
            entry["adjusted_avg_value"] = round(adjusted_sum / len(members), 2) if members else None
            entry["valued_user_count"] = len(members)
            groups.append(entry)
        else:
            # 估值仍要等后续回访：不出具该组估值
            entry["status"] = "awaiting_revisit"
            entry["missing_day_ages"] = missing_day_ages
            entry["adjusted_avg_value"] = None
            entry["valued_user_count"] = 0
            pending_groups.append(entry)

    total_denominator = sum(g["denominator"] for g in groups)
    if total_denominator:
        avg_retention_rate = round(
            sum(g["avg_retention_rate"] * g["denominator"] for g in groups) / total_denominator, 4)
        retention_adjusted_ltv = round(
            sum(g["adjusted_avg_value"] * g["denominator"] for g in groups) / total_denominator, 2)
    else:
        avg_retention_rate = 0.0
        retention_adjusted_ltv = ltv_result["avg_ltv"]

    return {
        "model_id": model_id,
        "retention_config_id": retention_config_id,
        "segment_id": model["segment_id"],
        "predict_window": model["predict_window"],
        "as_of_date": detail["as_of_date"],
        "basis_day_age": basis,
        "version": detail["version"],
        "original_ltv": ltv_result["avg_ltv"],
        "retention_adjusted_ltv": retention_adjusted_ltv,
        "avg_retention_rate": avg_retention_rate,
        "groups": groups,
        "pending_groups": pending_groups,
        "user_valuations": user_valuations,
        "covered_user_count": len(user_valuations),
        "pending_user_count": sum(g["denominator"] for g in pending_groups),
    }
