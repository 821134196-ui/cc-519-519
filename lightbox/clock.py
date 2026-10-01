"""时钟：默认真实时钟，测试可注入虚拟时钟。

时间一律表示为带 tzinfo 的本地 ``datetime``；落库用 ISO-8601 字符串。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class Clock:
    def now(self) -> datetime:  # pragma: no cover - 由子类覆盖
        return datetime.now().astimezone()


class VirtualClock(Clock):
    """固定/可拨动的时钟。``set`` 后 ``now()`` 冻结，便于测试。"""

    def __init__(self, moment: Optional[datetime] = None):
        self._moment: Optional[datetime] = moment

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("moment 必须带时区")
        self._moment = moment

    def advance(self, **delta) -> None:
        assert self._moment is not None, "先 set 一个起点"
        from datetime import timedelta

        self._moment += timedelta(**delta)

    def now(self) -> datetime:
        if self._moment is None:
            return datetime.now().astimezone()
        return self._moment


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime 必须带时区")
    return dt.isoformat(timespec="seconds")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
