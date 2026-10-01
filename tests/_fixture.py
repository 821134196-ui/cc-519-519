"""测试夹具：内存/临时 SQLite + 确定性时钟。"""
from __future__ import annotations

import sqlite3
import unittest

from lightbox.db import init_db
from lightbox.signals import SimulatedSignalAdapter

DAY = 1440
HOUR = 60
MIN = 1

# 固定的“周一”锚点：2026-09-28 00:00 (UTC+8) 是周一
ANCHOR_WEEK = 2962800  # epoch 分钟，下面按周历推算，不依赖此常量绝对正确


class Fixture:
    def __init__(self):
        # 内存库：避免沙箱文件系统上每次 commit 的 fsync 开销
        self.dir = None
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        self.clock_value = 0
        self.adapter = SimulatedSignalAdapter(self.conn, clock=lambda: self.clock_value)
        self._seed_master()

    def _seed_master(self):
        c = self.conn.cursor()
        # 巡查员
        self.inspectors = {}
        for name in ("甲巡", "乙巡", "丙巡"):
            c.execute("INSERT INTO inspectors(name) VALUES(?)", (name,))
            self.inspectors[name] = c.lastrowid
        # 广告主
        self.advertisers = {}
        for name in ("A商", "B商"):
            c.execute("INSERT INTO advertisers(name) VALUES(?)", (name,))
            self.advertisers[name] = c.lastrowid
        # 回路
        c.execute("INSERT INTO circuits(name) VALUES('回路甲')")
        self.circuit = c.lastrowid
        # 灯箱
        self.boxes = {}
        for code, cid in (("BX-1", self.circuit), ("BX-2", self.circuit), ("BX-3", None)):
            c.execute(
                "INSERT INTO lightboxes(code,name,map_x,map_y,circuit_id,created_at) VALUES(?,?,?,?,?,0)",
                (code, code, 0.5, 0.5, cid),
            )
            self.boxes[code] = c.lastrowid
        self.conn.commit()

    def add_schedule(self, code, weekday, start_hm, end_hm):
        self.conn.execute(
            "INSERT INTO schedules(lightbox_id,weekday,start_hm,end_hm) VALUES(?,?,?,?)",
            (self.boxes[code], weekday, start_hm, end_hm),
        )
        self.conn.commit()

    def add_contract(self, code, adv, start, end, rate=2.0, artwork="画面", color="#abc"):
        self.conn.execute(
            """INSERT INTO contracts(lightbox_id,advertiser_id,artwork_name,preview_color,
                   start_min,end_min,rate_per_min) VALUES(?,?,?,?,?,?,?)""",
            (self.boxes[code], self.advertisers[adv], artwork, color, start, end, rate),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()
