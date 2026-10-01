"""统一时钟：全系统按 UTC+8 展示与解析，存储一律用 epoch 分钟。"""
from __future__ import annotations

import datetime as _dt

TZ = _dt.timezone(_dt.timedelta(hours=8))


def now_min() -> int:
    return int(_dt.datetime.now(TZ).timestamp() // 60)


def dt_of(ts_min: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts_min * 60, TZ)


def fmt(ts_min: int | None) -> str:
    if ts_min is None:
        return "—"
    return dt_of(ts_min).strftime("%Y-%m-%d %H:%M")


def fmt_hm(ts_min: int) -> str:
    return dt_of(ts_min).strftime("%H:%M")


def parse_local(s: str) -> int:
    """'YYYY-MM-DDTHH:MM'（datetime-local）-> epoch 分钟。"""
    d = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M")
    return int(d.replace(tzinfo=TZ).timestamp() // 60)


def local_input(ts_min: int | None) -> str:
    """epoch 分钟 -> datetime-local 输入框值。"""
    if ts_min is None:
        return ""
    return dt_of(ts_min).strftime("%Y-%m-%dT%H:%M")
