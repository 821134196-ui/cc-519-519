"""时段判断测试：周历、跨午夜、计划外交集（白天误判的判定依据）。"""
from __future__ import annotations

import unittest

from lightbox import clock
from lightbox.times import (is_scheduled_lit, plan_segments, planned_minutes,
                            hm_to_min, local_day, domain_weekday)

DAY, HOUR = 1440, 60


def week_anchor(y=2026, m=10, d=1) -> int:
    """该日期所在周的周一 00:00（本地）。"""
    t = clock.parse_local(f"{y}-{m:02d}-{d:02d}T00:00")
    return t - clock.dt_of(t).weekday() * DAY


class WeekdayAnchorTest(unittest.TestCase):
    def test_anchor_is_monday(self):
        b = week_anchor()
        self.assertEqual(domain_weekday(local_day(b)), 1)

    def test_weekday_progression(self):
        b = week_anchor()
        for i in range(7):
            self.assertEqual(domain_weekday(local_day(b + i * DAY)), i + 1)


class SameDayScheduleTest(unittest.TestCase):
    def setUp(self):
        self.b = week_anchor()
        self.sch = [{"weekday": 1, "start_hm": "18:00", "end_hm": "23:00"}]

    def test_within_window_lit(self):
        self.assertTrue(is_scheduled_lit(self.b + 18 * HOUR, self.sch))
        self.assertTrue(is_scheduled_lit(self.b + 22 * HOUR + 59, self.sch))

    def test_boundaries(self):
        self.assertTrue(is_scheduled_lit(self.b + 18 * HOUR, self.sch))   # 含起点
        self.assertFalse(is_scheduled_lit(self.b + 23 * HOUR, self.sch))  # 不含终点

    def test_daytime_is_off(self):
        # 白天巡查 → 计划外，这是“白天误判”不立故障的依据
        for hm in (0, 9 * HOUR, 12 * HOUR, 17 * HOUR + 59, 23 * HOUR + 30):
            self.assertFalse(is_scheduled_lit(self.b + hm, self.sch), hm)

    def test_other_weekday_off(self):
        self.assertFalse(is_scheduled_lit(self.b + DAY + 20 * HOUR, self.sch))

    def test_planned_minutes(self):
        self.assertEqual(planned_minutes(self.b + 18 * HOUR, self.b + 23 * HOUR, self.sch), 300)
        # 查询窗口超出计划，只计交集
        self.assertEqual(planned_minutes(self.b + 17 * HOUR, self.b + DAY + 12 * HOUR, self.sch), 300)


class CrossMidnightScheduleTest(unittest.TestCase):
    def setUp(self):
        self.b = week_anchor()
        # 每天 18:00–次日 02:00
        self.sch = [{"weekday": w, "start_hm": "18:00", "end_hm": "02:00"}
                    for w in range(1, 8)]

    def test_before_midnight_and_after(self):
        self.assertTrue(is_scheduled_lit(self.b + 22 * HOUR, self.sch))
        self.assertTrue(is_scheduled_lit(self.b + DAY + 1 * HOUR, self.sch))
        self.assertFalse(is_scheduled_lit(self.b + DAY + 2 * HOUR, self.sch))
        self.assertFalse(is_scheduled_lit(self.b + DAY + 17 * HOUR, self.sch))

    def test_segments_across_midnight(self):
        segs = plan_segments(self.b + 20 * HOUR, self.b + DAY + 4 * HOUR, self.sch)
        # 周一晚段 + 跨到周二凌晨的段，合并后应为 20:00→02:00 一段
        self.assertEqual(segs, [(self.b + 20 * HOUR, self.b + DAY + 2 * HOUR)])
        self.assertEqual(sum(e - s for s, e in segs), 6 * HOUR)

    def test_multi_night_window(self):
        # 周一 18:00 到周三 02:00：两晚各 8 小时
        mins = planned_minutes(self.b + 18 * HOUR, self.b + 2 * DAY + 2 * HOUR, self.sch)
        self.assertEqual(mins, 16 * HOUR)


class OverlapMergeTest(unittest.TestCase):
    def test_overlapping_schedules_counted_once(self):
        b = week_anchor()
        sch = [
            {"weekday": 1, "start_hm": "18:00", "end_hm": "22:00"},
            {"weekday": 1, "start_hm": "20:00", "end_hm": "23:00"},
        ]
        self.assertEqual(planned_minutes(b + 17 * HOUR, b + 24 * HOUR, sch), 5 * HOUR)


class HmParseTest(unittest.TestCase):
    def test_hm(self):
        self.assertEqual(hm_to_min("00:00"), 0)
        self.assertEqual(hm_to_min("18:30"), 1110)
        self.assertEqual(hm_to_min("23:59"), 1439)


if __name__ == "__main__":
    unittest.main()
