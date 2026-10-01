"""亮灯计划：按灯箱读取有效计划版本，判断某时刻是否应亮。

每天可有多条窗口；``end <= start`` 表示跨午夜（如 22:00–06:00）。
判断时同时检查"当天起点窗口"和"前一天跨午夜延续窗口"。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _parse_days(s: str) -> frozenset[int]:
    return frozenset(int(x) for x in s.split(",") if x.strip())


def parse_days(s: str) -> list[int]:
    return sorted(_parse_days(s))


def format_days(days) -> str:
    names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    return "周" + "".join(names[d] for d in sorted(days))


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: time
    end: time
    valid_from: date
    valid_to: Optional[date]

    def covers(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return day.isoweekday() in self.days

    def crosses_midnight(self) -> bool:
        return self.end <= self.start


def active_windows(conn: sqlite3.Connection, box_id: str, day: date) -> List[Window]:
    rows = conn.execute(
        "SELECT days, start_hm, end_hm, valid_from, valid_to FROM schedules "
        "WHERE box_id = ? ORDER BY valid_from",
        (box_id,),
    ).fetchall()
    out: List[Window] = []
    for r in rows:
        vt = date.fromisoformat(r["valid_to"]) if r["valid_to"] else None
        out.append(
            Window(
                _parse_days(r["days"]),
                _parse_hm(r["start_hm"]),
                _parse_hm(r["end_hm"]),
                date.fromisoformat(r["valid_from"]),
                vt,
            )
        )
    return out


def is_within_schedule(conn: sqlite3.Connection, box_id: str,
                       moment: datetime) -> bool:
    """该时刻按计划是否应当亮灯。"""
    return schedule_window_at(conn, box_id, moment) is not None


def schedule_window_at(conn: sqlite3.Connection, box_id: str,
                       moment: datetime) -> Optional[Window]:
    # 时段按墙钟配置；时间戳统一带 +08:00，直接取其自身偏移下的日期时间
    d = moment.date()
    t = moment.time()
    wins = active_windows(conn, box_id, d)
    for w in wins:
        # 当天非跨夜窗口
        if w.covers(d) and not w.crosses_midnight() and w.start <= t < w.end:
            return w
        # 当天跨夜窗口的前半段
        if w.covers(d) and w.crosses_midnight() and t >= w.start:
            return w
        # 前一天跨夜窗口延续到今天凌晨
        prev = d - timedelta(days=1)
        if w.covers(prev) and w.crosses_midnight() and t < w.end:
            return w
    return None


def scheduled_intervals_between(conn: sqlite3.Connection, box_id: str,
                                start: datetime, end: datetime
                                ) -> List[Tuple[datetime, datetime]]:
    """展开 [start, end) 区间内该灯箱所有计划亮灯的具体时间段。

    用于把任意一段黑屏裁剪到"本应亮灯"的部分（白天不计黑屏）。
    """
    if end <= start:
        return []
    result: List[Tuple[datetime, datetime]] = []
    day = start.date() - timedelta(days=1)
    last = end.date()
    tz = start.tzinfo
    while day <= last:
        for w in active_windows(conn, box_id, day):
            ws = datetime.combine(day, w.start, tzinfo=tz)
            if w.crosses_midnight():
                we = datetime.combine(day + timedelta(days=1), w.end, tzinfo=tz)
            else:
                we = datetime.combine(day, w.end, tzinfo=tz)
            lo, hi = max(start, ws), min(end, we)
            if lo < hi:
                result.append((lo, hi))
        day += timedelta(days=1)
    # 合并相邻段
    result.sort()
    merged: List[Tuple[datetime, datetime]] = []
    for lo, hi in result:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def overlap_minutes(conn: sqlite3.Connection, box_id: str,
                    seg_start: datetime, seg_end: datetime) -> float:
    """黑屏区间与计划亮灯区间的重叠分钟数（白天不计）。"""
    total = 0.0
    for lo, hi in scheduled_intervals_between(conn, box_id, seg_start, seg_end):
        total += (hi - lo).total_seconds() / 60.0
    return total


def describe_schedule(conn: sqlite3.Connection, box_id: str,
                      day: Optional[date] = None) -> List[str]:
    day = day or datetime.now().astimezone().date()
    out = []
    for w in active_windows(conn, box_id, day):
        tag = format_days(w.days)
        out.append(f"{tag} {w.start.strftime('%H:%M')}–{w.end.strftime('%H:%M')}"
                   + ("（跨午夜）" if w.crosses_midnight() else ""))
    return out
