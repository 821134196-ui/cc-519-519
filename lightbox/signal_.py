"""模拟亮灯信号适配器。

真实场景下这里对接回路电流/灯带控制器；本实现按盒子配置生成信号:
  circuit.status='interrupted'  -> 全黑（供电中断，回路下所有灯箱同因）
  sim_mode:
    auto 计划内全亮、计划外全灭（健康）
    on   无论时段强亮（定时器配置错：白天误亮 / 该灭不灭）
    off  无论时段强黑（定时器配置错：计划内不亮）
    half 计划内半数灯带亮（部分灯带不亮）
每次轮询落 signal_readings，界面与巡检以最近读数为"亮灯证据"。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .clock import Clock, to_iso
from .schedule import is_within_schedule


@dataclass
class Signal:
    box_id: str
    ts: str
    fraction: float          # 0~1
    strips_on: int
    within_schedule: bool

    @property
    def state(self) -> str:
        if self.fraction >= 0.999:
            return "全亮"
        if self.fraction <= 0.001:
            return "熄灭" if self.within_schedule else "熄灭（计划外）"
        return f"半亮 {self.fraction:.0%}"


class SimulatedSignalAdapter:
    source = "poll"

    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    def _raw_fraction(self, box: sqlite3.Row, within: bool) -> tuple[float, str]:
        circuit = self.conn.execute(
            "SELECT status FROM circuits WHERE id = ?",
            (box["circuit_id"],),
        ).fetchone()
        if circuit["status"] == "interrupted":
            return 0.0, "供电中断"
        mode = box["sim_mode"]
        if mode == "auto":
            return (1.0 if within else 0.0), "跟随时段"
        if mode == "on":
            return 1.0, "强制通电（定时器异常）"
        if mode == "off":
            return 0.0, "强制断电（定时器异常）"
        if mode == "half":
            return (0.5 if within else 0.0), "半数灯带"
        raise ValueError(f"未知 sim_mode: {mode}")

    def read(self, box_id: str, *, persist: bool = True,
             source: Optional[str] = None) -> Signal:
        now = self.clock.now()
        box = self.conn.execute(
            "SELECT * FROM boxes WHERE id = ?", (box_id,)
        ).fetchone()
        if box is None:
            raise KeyError(box_id)
        within = is_within_schedule(self.conn, box_id, now)
        fraction, _reason = self._raw_fraction(box, within)
        strips_on = round(fraction * box["strips_total"])
        ts = to_iso(now)
        if persist:
            self.conn.execute(
                "INSERT INTO signal_readings(box_id, ts, fraction, source) "
                "VALUES (?,?,?,?)",
                (box_id, ts, fraction, source or self.source),
            )
            self.conn.commit()
        return Signal(box_id, ts, fraction, strips_on, within)

    def poll_all(self) -> list[Signal]:
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM boxes")]
        return [self.read(b) for b in ids]

    def latest(self, box_id: str) -> Optional[Signal]:
        r = self.conn.execute(
            "SELECT sr.ts, sr.fraction, b.strips_total FROM signal_readings sr "
            "JOIN boxes b ON b.id = sr.box_id WHERE sr.box_id = ? "
            "ORDER BY sr.ts DESC LIMIT 1",
            (box_id,),
        ).fetchone()
        if r is None:
            return None
        moment = self.clock.now()
        within = is_within_schedule(self.conn, box_id, moment)
        return Signal(box_id, r["ts"], r["fraction"],
                      round(r["fraction"] * r["strips_total"]), within)
