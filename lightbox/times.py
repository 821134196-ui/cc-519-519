"""时间与亮灯计划纯逻辑：周历计划、跨午夜时段、任意区间与计划时段交集。

计划以 *周* 为周期（weekdays: 1=周一 … 7=周日），start_hm/end_hm 为
"HH:MM"。end<=start 表示跨午夜，例如 18:00-06:00 在周一表示
周一 18:00 至周二 06:00。白天巡查落在计划时段外时，判定为“计划外/白天
误判”而非故障。

所有分钟数均为 naive-local epoch minute，由调用方用 int(dt.timestamp()/60)
生成，测试可直接传整数，完全不依赖当前时钟。
"""
from __future__ import annotations

from dataclasses import dataclass

MINUTES_PER_DAY = 1440
# 与 clock.TZ 保持一致；周历/时段按本地日历切日（本地 18:00 在 UTC 仍是当天）
LOCAL_OFFSET_MIN = 8 * 60


def hm_to_min(hm: str) -> int:
    """'HH:MM' -> 当日分钟数 (0..1439)。"""
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def local_day(ts: int) -> int:
    """epoch 分钟 -> 本地日历日序号。"""
    return (ts + LOCAL_OFFSET_MIN) // MINUTES_PER_DAY


def day_origin(day: int) -> int:
    """本地日历日序号 -> 该日 00:00 的 epoch 分钟。"""
    return day * MINUTES_PER_DAY - LOCAL_OFFSET_MIN


def domain_weekday(day: int) -> int:
    """本地日序号 -> 领域星期 (1=周一 … 7=周日)。1970-01-01 为周四。"""
    return ((day + 3) % 7) + 1


def plan_segments(start_ts: int, end_ts: int, schedules) -> list[tuple[int, int]]:
    """返回 [start_ts, end_ts) 内所有被亮灯计划覆盖的分钟区间。

    schedules: 可迭代对象，每项含 weekday / start_hm / end_hm（或同名键）。
    多个计划的覆盖会合并去重，结果按起点升序、互不重叠。
    """
    if end_ts <= start_ts:
        return []
    intervals: list[list[int]] = []
    day0, day1 = local_day(start_ts), local_day(end_ts)
    for sch in schedules:
        weekday = sch.weekday if hasattr(sch, "weekday") else sch["weekday"]
        start_hm = sch.start_hm if hasattr(sch, "start_hm") else sch["start_hm"]
        end_hm = sch.end_hm if hasattr(sch, "end_hm") else sch["end_hm"]
        sm, em = hm_to_min(start_hm), hm_to_min(end_hm)
        span_days = 0 if sm < em else 1
        for day in range(day0 - 1, day1 + 1):
            if domain_weekday(day) != weekday:
                continue
            origin = day_origin(day)
            if sm < em:
                intervals.append([origin + sm, origin + em])
            else:
                intervals.append([origin + sm, origin + span_days * MINUTES_PER_DAY + em])
    return _intersect_with_window(_merge(intervals), start_ts, end_ts)


def _merge(intervals: list[list[int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals.sort()
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _intersect_with_window(segments, start_ts, end_ts):
    out = []
    for s, e in segments:
        s2, e2 = max(s, start_ts), min(e, end_ts)
        if e2 > s2:
            out.append((s2, e2))
    return out


def planned_minutes(start_ts: int, end_ts: int, schedules) -> int:
    """区间内计划亮灯总分钟数（多计划重叠不重复计）。"""
    return sum(e - s for s, e in plan_segments(start_ts, end_ts, schedules))


def is_scheduled_lit(ts: int, schedules) -> bool:
    """某一分钟是否处于任一亮灯计划内。"""
    return bool(plan_segments(ts, ts + 1, schedules))


@dataclass
class ScheduleView:
    """给界面/API 展示用的计划时段视图。"""
    weekday: int
    start_hm: str
    end_hm: str
    cross_midnight: bool

    @classmethod
    def from_row(cls, row) -> "ScheduleView":
        return cls(
            weekday=row["weekday"],
            start_hm=row["start_hm"],
            end_hm=row["end_hm"],
            cross_midnight=hm_to_min(row["start_hm"]) >= hm_to_min(row["end_hm"]),
        )


WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四",
              5: "周五", 6: "周六", 7: "周日"}


def describe_schedules(schedules) -> str:
    parts = []
    for sch in schedules:
        v = sch if isinstance(sch, ScheduleView) else ScheduleView.from_row(sch)
        suffix = "（次日）" if v.cross_midnight else ""
        parts.append(f"{WEEKDAY_CN[v.weekday]} {v.start_hm}–{v.end_hm}{suffix}")
    return "；".join(parts) if parts else "无计划"
