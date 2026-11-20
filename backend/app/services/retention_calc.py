"""留存计算引擎：计算用户留存率，支持按维度分段。"""
from __future__ import annotations

import datetime
import zlib
from typing import Any
from collections import defaultdict

from app.store import store
from app.schemas import RetentionResult

# 队列明细数据版本：确认过的入组人数按该版本回查
COHORT_DETAIL_VERSION = "v1.0"


def _generate_retention_events(event_id: str, base_date: str, days: int) -> list[dict[str, Any]]:
    """生成留存分析所需的模拟事件数据。"""
    events = []
    import datetime
    base = datetime.datetime.strptime(base_date, "%Y-%m-%d")
    base_ts = int(base.timestamp())

    for day in range(days):
        # 每天的用户数递减模拟留存
        num_users = max(1000 - day * 80, 200)
        for user_idx in range(1, num_users + 1):
            user_id = f"U{user_idx:05d}"
            # 用户在不同维度上的分布
            attr_001 = ["app", "web", "miniapp", "h5"][user_idx % 4]
            attr_005 = ["普通", "银卡", "金卡", "钻石"][user_idx % 4]
            events.append({
                "user_id": user_id,
                "event_id": event_id,
                "timestamp": base_ts + day * 86400 + (user_idx % 3600),
                "ATTR-001": attr_001,
                "ATTR-005": attr_005,
                "date": (base + datetime.timedelta(days=day)).strftime("%Y-%m-%d"),
            })
    return events


def calculate_retention(config_id: str) -> RetentionResult:
    """执行留存计算。"""
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None:
        raise ValueError(f"Retention config not found: {config_id}")

    init_event = config["init_event"]
    return_event = config["return_event"]
    calc_period = config.get("calc_period", "day")
    segment_dim = config.get("segment_dim", "")
    base_date = config["base_date"]
    retention_window = config.get("retention_window", 30)

    # 生成事件数据
    events = _generate_retention_events(init_event, base_date, retention_window)

    # 按日期和用户分组
    date_users: dict[str, set[str]] = defaultdict(set)
    dim_date_users: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for e in events:
        date_users[e["date"]].add(e["user_id"])
        if segment_dim:
            dim_val = e.get(segment_dim, "unknown")
            dim_date_users[dim_val][e["date"]].add(e["user_id"])

    # 计算留存率
    cohort_sizes: dict[str, int] = {}
    retention_rates: dict[str, list[float]] = {}

    # 基准日期的用户作为初始队列
    init_date = base_date
    init_users = date_users.get(init_date, set())
    cohort_sizes[init_date] = len(init_users)
    retention_rates[init_date] = []

    for day in range(retention_window):
        target_date = _add_days(base_date, day)
        target_users = date_users.get(target_date, set())
        retained = init_users & target_users
        rate = len(retained) / len(init_users) if init_users else 0.0
        retention_rates[init_date].append(round(rate, 4))

    # 按维度分段
    if segment_dim:
        for dim_val, dim_data in dim_date_users.items():
            dim_init_users = dim_data.get(init_date, set())
            key = f"{segment_dim}:{dim_val}"
            cohort_sizes[key] = len(dim_init_users)
            retention_rates[key] = []
            for day in range(retention_window):
                target_date = _add_days(base_date, day)
                target_users = dim_data.get(target_date, set())
                retained = dim_init_users & target_users
                rate = len(retained) / len(dim_init_users) if dim_init_users else 0.0
                retention_rates[key].append(round(rate, 4))

    result = RetentionResult(
        config_id=config_id,
        init_event=init_event,
        return_event=return_event,
        base_date=base_date,
        cohort_sizes=cohort_sizes,
        retention_rates=retention_rates,
        result_table=config.get("result_table", ""),
    )

    # 存储产物
    store.set_artifact("retention_calc", config_id, result.model_dump())

    # 更新状态
    store.update("retention_calc", config["id"], {
        "status": "已完成",
        "pending": False,
    })

    return result


def _add_days(date_str: str, days: int) -> str:
    """日期加减。"""
    import datetime
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return (d + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def get_retention_result(config_id: str) -> dict[str, Any] | None:
    """获取留存计算结果。"""
    artifact = store.get_artifact("retention_calc", config_id)
    if artifact:
        return artifact
    try:
        result = calculate_retention(config_id)
        return result.model_dump()
    except ValueError:
        return None


# ========== 留存队列明细（按观察天龄对齐） ==========
#
# 跨入组批次的时间对齐查询：每个入组批次（入组日 × 渠道）按观察天龄
# （day_age = 观察日期 - 入组日）对齐到同一网格，便于比较不同入组日和
# 渠道的回访。网格上天龄未到达的单元标记为 unobserved（未观察到），
# 与已观察但回访为 0 的“确认的零留存”严格区分，未知天数不计成零回访。


def _cohort_channels(config: dict[str, Any]) -> list[str]:
    """确定队列拆分的渠道取值：优先取分段维度在属性字典里的枚举值。"""
    dim = config.get("segment_dim") or ""
    if dim:
        attr = store.find_by_field("attr_dict", "attr_id", dim)
        if attr and attr.get("enum_values"):
            return list(attr["enum_values"])
        return [dim]
    return ["all"]


def _cohort_size(cohort_idx: int, channel_idx: int) -> int:
    """确定性的入组人数（模拟）：不同入组批次、不同渠道人数不同。"""
    return 24 + ((cohort_idx + 1) * (channel_idx + 3) * 7) % 18


def _member_revisits(user_id: str, day_age: int, cohort_idx: int, channel_idx: int) -> bool:
    """确定性的会员回访模拟。

    特殊单元：第 2 批 × 第 3 渠道 × 天龄 2 全部未回访，用于构造一个
    “已观察但确认为零留存”的单元，与未观察到的单元形成对照。
    """
    if cohort_idx == 2 and channel_idx == 3 and day_age == 2:
        return False
    curve = max(78 - 7 * day_age, 18)  # 回访概率随天龄递减
    return zlib.crc32(f"{user_id}#{day_age}".encode("utf-8")) % 100 < curve


def build_cohort_detail(config_id: str) -> dict[str, Any]:
    """构建留存队列明细产物（全量真值，与观察截止日无关）。

    产物包含每个入组批次 × 渠道 × 天龄的回访真值以及批次成员名单，
    查询时按 as_of_date 决定哪些天龄已被观察到。同一产物支撑不同
    截止日的反复查询，保证“到达第七天之前/之后”两次校准基于同一批数据。
    """
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None:
        raise ValueError(f"Retention config not found: {config_id}")

    base_date = config["base_date"]
    window = int(config.get("retention_window", 30))
    channels = _cohort_channels(config)

    cells: list[dict[str, Any]] = []
    members: dict[str, list[str]] = {}
    for cohort_idx in range(window):
        cohort_date = _add_days(base_date, cohort_idx)
        for channel_idx, channel in enumerate(channels):
            size = _cohort_size(cohort_idx, channel_idx)
            uids = [f"U{cohort_idx:02d}{channel_idx}{k:03d}" for k in range(1, size + 1)]
            members[f"{cohort_date}|{channel}"] = uids
            for day_age in range(0, window + 1):  # 天龄 0..window，含第七天
                if day_age == 0:
                    retained = size  # 入组当天全员在组
                else:
                    retained = sum(
                        1 for uid in uids
                        if _member_revisits(uid, day_age, cohort_idx, channel_idx)
                    )
                cells.append({
                    "cohort_date": cohort_date,
                    "channel": channel,
                    "day_age": day_age,
                    "observe_date": _add_days(cohort_date, day_age),
                    "cohort_size": size,
                    "retained_users": retained,
                })

    artifact = {
        "config_id": config_id,
        "base_date": base_date,
        "retention_window": window,
        "channels": channels,
        "version": COHORT_DETAIL_VERSION,
        "cells": cells,
        "members": members,
    }
    store.set_artifact("retention_cohort", config_id, artifact)

    # 登记入组人数的初始版本，供后续按版本回查
    for cohort_idx in range(window):
        cohort_date = _add_days(base_date, cohort_idx)
        for channel_idx, channel in enumerate(channels):
            if _confirmed_cohort_size(config_id, cohort_date, channel, COHORT_DETAIL_VERSION) is None:
                store.add_snapshot(f"retention_cohort_size:{config_id}", {
                    "cohort_date": cohort_date,
                    "channel": channel,
                    "cohort_size": _cohort_size(cohort_idx, channel_idx),
                    "version": COHORT_DETAIL_VERSION,
                })
    return artifact


def _get_cohort_artifact(config_id: str) -> dict[str, Any]:
    artifact = store.get_artifact("retention_cohort", config_id)
    if artifact is None:
        artifact = build_cohort_detail(config_id)
    return artifact


def _confirmed_cohort_size(config_id: str, cohort_date: str, channel: str, version: str) -> int | None:
    """按对应版本回查确认过的入组人数；该版本无记录时返回 None。"""
    snapshots = store.list_snapshots(f"retention_cohort_size:{config_id}")
    for snap in reversed(snapshots):  # 同一版本取最近一次确认
        if (snap.get("cohort_date") == cohort_date
                and snap.get("channel") == channel
                and snap.get("version") == version):
            return int(snap["cohort_size"])
    return None


def confirm_cohort_size(config_id: str, cohort_date: str, channel: str,
                        cohort_size: int, version: str) -> dict[str, Any]:
    """确认一个入组批次的入组人数，按版本登记，供明细查询回查。"""
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None:
        raise ValueError(f"Retention config not found: {config_id}")
    snapshot = {
        "cohort_date": cohort_date,
        "channel": channel,
        "cohort_size": int(cohort_size),
        "version": version,
    }
    store.add_snapshot(f"retention_cohort_size:{config_id}", snapshot)
    return snapshot


def get_cohort_detail(config_id: str, as_of_date: str | None = None,
                      cohort_date: str | None = None, channel: str | None = None,
                      day_age: int | None = None, version: str | None = None) -> dict[str, Any]:
    """按观察天龄查询留存队列明细数据。

    - as_of_date：观察截止日，默认 base_date + retention_window；截止日之后
      的天龄标记为 unobserved（retained_users / retention_rate 为 None），
      已观察但回访为 0 的单元保留 0，二者清楚区分。
    - version：入组人数按该版本回查确认过的记录，缺省用明细产物版本。
    """
    if store.find_by_field("retention_calc", "config_id", config_id) is None:
        raise ValueError(f"Retention config not found: {config_id}")
    artifact = _get_cohort_artifact(config_id)
    window = int(artifact["retention_window"])
    if as_of_date is None:
        as_of_date = _add_days(artifact["base_date"], window)
    else:
        datetime.datetime.strptime(as_of_date, "%Y-%m-%d")  # 校验日期格式
    use_version = version or artifact["version"]

    rows: list[dict[str, Any]] = []
    for cell in artifact["cells"]:
        if cohort_date is not None and cell["cohort_date"] != cohort_date:
            continue
        if channel is not None and cell["channel"] != channel:
            continue
        if day_age is not None and cell["day_age"] != day_age:
            continue
        observed = cell["observe_date"] <= as_of_date
        confirmed = _confirmed_cohort_size(
            config_id, cell["cohort_date"], cell["channel"], use_version)
        size = confirmed if confirmed is not None else cell["cohort_size"]
        rows.append({
            "config_id": config_id,
            "cohort_date": cell["cohort_date"],
            "channel": cell["channel"],
            "day_age": cell["day_age"],
            "observe_date": cell["observe_date"],
            "cohort_size": size,
            "size_confirmed": confirmed is not None,
            "retained_users": cell["retained_users"] if observed else None,
            "retention_rate": round(cell["retained_users"] / size, 4) if observed and size else None,
            "status": "observed" if observed else "unobserved",
            "version": use_version,
        })
    rows.sort(key=lambda r: (r["cohort_date"], r["channel"], r["day_age"]))
    return {
        "config_id": config_id,
        "as_of_date": as_of_date,
        "basis_day_age": window,
        "version": use_version,
        "items": rows,
        "total": len(rows),
    }


def get_cohort_members(config_id: str, cohort_date: str, channel: str) -> list[str]:
    """返回一个入组批次的成员名单（用于核对估值覆盖了哪些用户）。"""
    artifact = _get_cohort_artifact(config_id)
    return list(artifact["members"].get(f"{cohort_date}|{channel}", []))
