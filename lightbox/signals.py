"""亮灯信号适配器。

生产中适配器对接回路控制器/电表；此处提供可注入、可录制的模拟适配器：
- 默认定时器态：信号 == 计划（全部灯带亮/灭），并可用 set_override 制造故障；
- override 只在「当前模拟时钟」>= set_min 时生效，便于回放故障时间线；
- clock_min 可注入，测试与种子脚本完全确定，不依赖系统时间。
"""
from __future__ import annotations

from dataclasses import dataclass

from .times import is_scheduled_lit


@dataclass
class LightSignal:
    brightness: float          # 0..1
    lit_bars: int
    total_bars: int
    source: str                # simulated_plan / simulated_override

    @property
    def is_dark(self) -> bool:
        return self.brightness <= 0.0

    @property
    def is_partial(self) -> bool:
        return 0.0 < self.brightness < 0.999


class SimulatedSignalAdapter:
    """根据计划与可录制 override 推断当前亮灯信号。"""

    def __init__(self, conn, clock=None):
        self.conn = conn
        self._clock = clock  # 可调用对象 -> epoch 分钟；None 时用系统时间

    def now_min(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        import time
        return int(time.time() // 60)

    def total_bars(self, lightbox_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(total_bars),4) AS n FROM inspections WHERE lightbox_id=?",
            (lightbox_id,),
        ).fetchone()
        # 种子灯箱默认 4 条灯带
        return max(int(row["n"]), 1)

    def read(self, lightbox_id: int, at_min: int | None = None) -> LightSignal:
        """读取信号；at_min 给定则按该时刻回放（测试/种子脚本确定性使用）。"""
        now = self.now_min() if at_min is None else at_min
        schedules = self.conn.execute(
            "SELECT weekday, start_hm, end_hm FROM schedules WHERE lightbox_id=?",
            (lightbox_id,),
        ).fetchall()
        ov = self.conn.execute(
            "SELECT brightness, lit_bars, set_min FROM signal_overrides WHERE lightbox_id=?",
            (lightbox_id,),
        ).fetchone()

        if ov is not None and now >= ov["set_min"]:
            return LightSignal(
                brightness=float(ov["brightness"]),
                lit_bars=int(ov["lit_bars"]),
                total_bars=self.total_bars(lightbox_id),
                source="simulated_override",
            )

        if is_scheduled_lit(now, schedules):
            bars = self.total_bars(lightbox_id)
            return LightSignal(1.0, bars, bars, "simulated_plan")
        return LightSignal(0.0, 0, self.total_bars(lightbox_id), "simulated_plan")

    # -- 测试台/演练用的信号录制 -------------------------------------------
    def set_override(self, lightbox_id: int, brightness: float, lit_bars: int,
                     set_min: int | None = None, note: str = "") -> None:
        set_min = self.now_min() if set_min is None else set_min
        self.conn.execute(
            """INSERT INTO signal_overrides(lightbox_id, brightness, lit_bars, set_min, note)
               VALUES(?,?,?,?,?)
               ON CONFLICT(lightbox_id) DO UPDATE SET
                 brightness=excluded.brightness, lit_bars=excluded.lit_bars,
                 set_min=excluded.set_min, note=excluded.note""",
            (lightbox_id, float(brightness), int(lit_bars), set_min, note),
        )
        self.conn.commit()

    def clear_override(self, lightbox_id: int) -> None:
        self.conn.execute("DELETE FROM signal_overrides WHERE lightbox_id=?", (lightbox_id,))
        self.conn.commit()
