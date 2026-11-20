"""留存队列明细与LTV校准的集成测试。

场景：同一个预测周期使用两组注册会员的回访数据。
- 较早的一组（2026-11-01 入组）已有第七天的回访；
- 较新的一组（2026-11-05 入组）在校准时点只观测了四天。
在较新队列达到第七天之前（as_of=2026-11-08）和之后（as_of=2026-11-12）
各校准一次，比较返回的用户估值、组别及其分母，确认未知天数没有被计成零回访。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.retention_calc import (
    get_cohort_detail,
    confirm_cohort_size,
    COHORT_DETAIL_VERSION,
)
from app.services.lifetime_value import calibrate_ltv_with_cohorts

CONFIG_ID = "RC-001"
MODEL_ID = "LTV-001"
COHORT_A = "2026-11-01"  # 较早的一组：已有第七天回访
COHORT_B = "2026-11-05"  # 较新的一组：校准时点只观测了四天
AS_OF_BEFORE = "2026-11-08"  # A 到达天龄7，B 只到天龄3（四天）
AS_OF_AFTER = "2026-11-12"   # B 到达天龄7
CHANNELS = ["app", "web", "miniapp", "h5"]


def _group(groups: list[dict], cohort_date: str, channel: str) -> dict | None:
    for g in groups:
        if g["cohort_date"] == cohort_date and g["channel"] == channel:
            return g
    return None


class TestCohortDetailQuery(unittest.TestCase):
    """按观察天龄查询留存队列明细：跨入组批次时间对齐。"""

    def setUp(self) -> None:
        store._artifacts.clear()
        store._snapshots.clear()
        store._compute_cache.clear()

    def test_day_age_alignment_across_cohorts_and_channels(self) -> None:
        """不同入组日、不同渠道按同一观察天龄网格对齐，可互相比较。"""
        detail = get_cohort_detail(CONFIG_ID, as_of_date=AS_OF_BEFORE)
        self.assertEqual(detail["as_of_date"], AS_OF_BEFORE)
        rows = detail["items"]
        # 天龄网格：每个入组批次 × 渠道都覆盖 0..7（含第七天）
        by_key: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            by_key.setdefault((row["cohort_date"], row["channel"]), []).append(row["day_age"])
        self.assertIn((COHORT_A, "app"), by_key)
        self.assertIn((COHORT_B, "app"), by_key)
        for key, ages in by_key.items():
            self.assertEqual(ages, list(range(0, 8)), key)
        # 同一天龄下不同入组批次可比较：天龄3在A、B两组都有记录
        age3 = [r for r in rows if r["day_age"] == 3]
        self.assertIn(COHORT_A, {r["cohort_date"] for r in age3})
        self.assertIn(COHORT_B, {r["cohort_date"] for r in age3})
        # 同一天龄下不同渠道可比较
        age3_a = [r for r in age3 if r["cohort_date"] == COHORT_A]
        self.assertEqual({r["channel"] for r in age3_a}, set(CHANNELS))

    def test_unobserved_distinct_from_confirmed_zero(self) -> None:
        """未观察到与确认的零留存清楚区分。"""
        detail = get_cohort_detail(CONFIG_ID, as_of_date=AS_OF_BEFORE)
        rows = detail["items"]
        # 较新队列 B 的天龄4..7 尚未观察到：不是零，是未知
        for age in (4, 5, 6, 7):
            row = next(r for r in rows
                       if r["cohort_date"] == COHORT_B and r["channel"] == "app"
                       and r["day_age"] == age)
            self.assertEqual(row["status"], "unobserved")
            self.assertIsNone(row["retained_users"])
            self.assertIsNone(row["retention_rate"])
        # B 已观察的四天（天龄0..3）有真实回访数
        for age in (0, 1, 2, 3):
            row = next(r for r in rows
                       if r["cohort_date"] == COHORT_B and r["channel"] == "app"
                       and r["day_age"] == age)
            self.assertEqual(row["status"], "observed")
            self.assertIsNotNone(row["retained_users"])
        # 确认的零留存：已观察但回访为 0（11-03 批次 h5 渠道 天龄2）
        zero = next(r for r in rows
                    if r["cohort_date"] == "2026-11-03" and r["channel"] == "h5"
                    and r["day_age"] == 2)
        self.assertEqual(zero["status"], "observed")
        self.assertEqual(zero["retained_users"], 0)
        self.assertEqual(zero["retention_rate"], 0.0)

    def test_confirmed_cohort_size_looked_up_by_version(self) -> None:
        """确认过的入组人数按对应版本回查，旧版本查询不受新版本影响。"""
        before = get_cohort_detail(CONFIG_ID, as_of_date=AS_OF_BEFORE)
        row_v1 = next(r for r in before["items"]
                      if r["cohort_date"] == COHORT_A and r["channel"] == "app"
                      and r["day_age"] == 0)
        original_size = row_v1["cohort_size"]
        self.assertTrue(row_v1["size_confirmed"])

        # 以新版本 v1.1 确认一个修正后的入组人数
        confirm_cohort_size(CONFIG_ID, COHORT_A, "app", original_size + 100, "v1.1")

        # 默认版本（v1.0）回查：仍是原确认数
        still_v1 = get_cohort_detail(CONFIG_ID, as_of_date=AS_OF_BEFORE)
        row = next(r for r in still_v1["items"]
                   if r["cohort_date"] == COHORT_A and r["channel"] == "app"
                   and r["day_age"] == 0)
        self.assertEqual(row["version"], COHORT_DETAIL_VERSION)
        self.assertEqual(row["cohort_size"], original_size)

        # 按 v1.1 回查：返回该版本确认的人数
        v11 = get_cohort_detail(CONFIG_ID, as_of_date=AS_OF_BEFORE, version="v1.1")
        row11 = next(r for r in v11["items"]
                     if r["cohort_date"] == COHORT_A and r["channel"] == "app"
                     and r["day_age"] == 0)
        self.assertEqual(row11["cohort_size"], original_size + 100)
        # 未确认过 v1.1 的批次仍回退到计算值
        other = next(r for r in v11["items"]
                     if r["cohort_date"] == COHORT_B and r["channel"] == "app"
                     and r["day_age"] == 0)
        self.assertFalse(other["size_confirmed"])
        self.assertGreater(other["cohort_size"], 0)


class TestCalibrationBeforeAfterDay7(unittest.TestCase):
    """较新队列达到第七天之前和之后各校准一次并对比。"""

    def setUp(self) -> None:
        store._artifacts.clear()
        store._snapshots.clear()
        store._compute_cache.clear()
        self.before = calibrate_ltv_with_cohorts(
            MODEL_ID, CONFIG_ID, as_of_date=AS_OF_BEFORE)
        self.after = calibrate_ltv_with_cohorts(
            MODEL_ID, CONFIG_ID, as_of_date=AS_OF_AFTER)

    def test_same_predict_window(self) -> None:
        """两次校准属于同一个预测周期。"""
        self.assertEqual(self.before["predict_window"], self.after["predict_window"])
        self.assertEqual(self.before["model_id"], self.after["model_id"])

    def test_groups_and_denominators_returned(self) -> None:
        """校准返回实际参与估值的组别、观察区间和对应分母。"""
        self.assertGreater(len(self.before["groups"]), 0)
        for g in self.before["groups"] + self.before["pending_groups"]:
            self.assertGreater(g["denominator"], 0)
            self.assertIn("observation_window", g)
            self.assertIn("observed_day_ages", g["observation_window"])
        # 之前：只有较早队列 A 的四个渠道组参与估值
        self.assertEqual({g["cohort_date"] for g in self.before["groups"]}, {COHORT_A})
        self.assertEqual({g["channel"] for g in self.before["groups"]}, set(CHANNELS))
        # 之后：较新队列 B 也参与估值
        self.assertIn(COHORT_B, {g["cohort_date"] for g in self.after["groups"]})

    def test_seven_day_basis_coverage_is_verifiable(self) -> None:
        """七日依据实际覆盖了哪些用户、哪些估值仍要等后续回访，均可核对。"""
        # 之前：七日依据只覆盖 A 组用户；B 组估值等待后续回访
        covered_cohorts = {v["cohort_date"] for v in self.before["user_valuations"]}
        self.assertEqual(covered_cohorts, {COHORT_A})
        b_pending = _group(self.before["pending_groups"], COHORT_B, "app")
        self.assertIsNotNone(b_pending)
        self.assertEqual(b_pending["status"], "awaiting_revisit")
        self.assertEqual(b_pending["observation_window"]["observed_day_ages"], [0, 1, 2, 3])
        self.assertEqual(b_pending["missing_day_ages"], [4, 5, 6, 7])
        self.assertIsNone(b_pending["adjusted_avg_value"])
        self.assertEqual(self.before["pending_user_count"],
                         sum(g["denominator"] for g in self.before["pending_groups"]))
        # 之后：B 组进入估值，等待列表缩短
        self.assertIn(COHORT_B, {v["cohort_date"] for v in self.after["user_valuations"]})
        self.assertLess(len(self.after["pending_groups"]), len(self.before["pending_groups"]))
        self.assertGreater(self.after["covered_user_count"], self.before["covered_user_count"])

    def test_unknown_days_not_counted_as_zero(self) -> None:
        """未知天数没有被计成零回访：B 组前四天的留存口径前后一致。"""
        b_before = _group(self.before["pending_groups"], COHORT_B, "app")
        b_after = _group(self.after["groups"], COHORT_B, "app")
        self.assertIsNotNone(b_before)
        self.assertIsNotNone(b_after)
        # 同一批数据：B 已观察天龄的留存率前后完全一致
        rates_before = {r["day_age"]: r["retention_rate"] for r in b_before["observed_rates"]}
        rates_after = {r["day_age"]: r["retention_rate"] for r in b_after["observed_rates"]}
        for age in (0, 1, 2, 3):
            self.assertEqual(rates_before[age], rates_after[age])
        # 之前的均值只按已观察的四天计算；若未知天数被计成零，均值会被稀释
        self.assertAlmostEqual(
            b_before["avg_retention_rate"],
            round(sum(rates_after[a] for a in (0, 1, 2, 3)) / 4, 4),
        )
        zero_filled = sum(rates_after[a] for a in (0, 1, 2, 3)) / 8
        self.assertNotAlmostEqual(b_before["avg_retention_rate"], zero_filled, places=3)
        # 之后 B 组覆盖完整 0..7 天龄
        self.assertEqual(b_after["observation_window"]["observed_day_ages"],
                         list(range(0, 8)))
        self.assertTrue(b_after["basis_covered"])

    def test_user_valuations_compared_across_calibrations(self) -> None:
        """比较两次校准返回的用户估值、组别及其分母。"""
        valuations_before = {v["user_id"]: v["adjusted_value"]
                             for v in self.before["user_valuations"]}
        valuations_after = {v["user_id"]: v["adjusted_value"]
                            for v in self.after["user_valuations"]}
        # A 组用户估值前后不变（同一批数据、同一预测周期）
        self.assertEqual(valuations_before,
                         {k: v for k, v in valuations_after.items() if k in valuations_before})
        # 之后新增了 B 组用户的估值
        new_users = set(valuations_after) - set(valuations_before)
        self.assertGreater(len(new_users), 0)
        # 分母前后一致：同版本回查的入组人数不随观察截止日变化
        denom_before = {(g["cohort_date"], g["channel"]): g["denominator"]
                        for g in self.before["groups"] + self.before["pending_groups"]}
        denom_after = {(g["cohort_date"], g["channel"]): g["denominator"]
                       for g in self.after["groups"] + self.after["pending_groups"]}
        for key in denom_before:
            self.assertEqual(denom_before[key], denom_after[key], key)
        # 用户估值数与覆盖组的分母合计一致
        self.assertEqual(self.before["covered_user_count"],
                         sum(g["denominator"] for g in self.before["groups"]))
        self.assertEqual(self.after["covered_user_count"],
                         sum(g["denominator"] for g in self.after["groups"]))

    def test_channel_comparison_within_same_cohort(self) -> None:
        """渠道比较：同一入组日各渠道的回访与估值口径可互相核对。"""
        a_groups = [g for g in self.after["groups"] if g["cohort_date"] == COHORT_A]
        self.assertEqual({g["channel"] for g in a_groups}, set(CHANNELS))
        for g in a_groups:
            # 每个渠道组都有完整七日依据和各自的分母
            self.assertTrue(g["basis_covered"])
            self.assertGreater(g["denominator"], 0)
            self.assertEqual(g["valued_user_count"], g["denominator"])
            self.assertIsNotNone(g["adjusted_avg_value"])
        # 不同渠道的分母不同，估值按各自分母加权汇总
        self.assertGreater(len({g["denominator"] for g in a_groups}), 1)
        total = sum(g["denominator"] for g in self.after["groups"])
        expected = round(
            sum(g["adjusted_avg_value"] * g["denominator"] for g in self.after["groups"]) / total, 2)
        self.assertEqual(self.after["retention_adjusted_ltv"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
