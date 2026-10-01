"""测试：时段判断、故障归并、补播计算、复验/审批权限。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lightbox.clock import VirtualClock, to_iso
from lightbox.db import connect, init_db
from lightbox.engine import DomainError, Engine
from lightbox import makeup
from lightbox.schedule import is_within_schedule, overlap_minutes
from lightbox.signal_ import SimulatedSignalAdapter

TZ = timezone(timedelta(hours=8))


def dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conn = connect(root / "t.db")
        init_db(self.conn)
        self.photos = root / "photos"
        self.clock = VirtualClock(dt(2026, 10, 1, 20, 0))
        self._fixture()
        self.engine = Engine(self.conn, self.clock, str(self.photos))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _fixture(self):
        c = self.conn
        c.executemany(
            "INSERT INTO users(login,name,role) VALUES(?,?,?)",
            [("li", "李巡", "inspector"), ("zhang", "张巡", "inspector"),
             ("wang", "王维修", "repair"),
             ("zhao", "赵主管", "supervisor")])
        c.executemany(
            "INSERT INTO circuits(id,name,status) VALUES(?,?,?)",
            [("C1", "一回路", "normal"), ("C2", "二回路", "interrupted")])

        def box(bid, cir, mode, strips=8):
            c.execute(
                "INSERT INTO boxes(id,name,addr,lat,lng,circuit_id,strips_total,"
                "creative_hex,sim_mode) VALUES(?,?,?,?,?,?,?,?,?)",
                (bid, bid, "测试路", 31.2, 121.4, cir, strips, "#3563D6", mode))
            c.execute(
                "INSERT INTO schedules(box_id,days,start_hm,end_hm,valid_from)"
                " VALUES(?,'1,2,3,4,5,6,7','18:00','23:00','2026-01-01')",
                (bid,))

        # B1 健康；B2 半亮；B3/B5 挂中断回路；B4 定时错(强黑)；B6 白天误亮(强亮)
        for b, cir, mode in [("B1", "C1", "auto"), ("B2", "C1", "half"),
                             ("B3", "C2", "auto"), ("B4", "C1", "off"),
                             ("B5", "C2", "auto"), ("B6", "C1", "on")]:
            box(b, cir, mode)
        # B7 跨午夜计划 22:00-06:00
        box("B7", "C1", "auto")
        c.execute("UPDATE schedules SET start_hm='22:00', end_hm='06:00'"
                  " WHERE box_id='B7'")
        # 合同：B2 跨版本（10-01 00:00 换版，新版系数 1.5）
        for bid in ("B1", "B3", "B4", "B5", "B6", "B7"):
            c.execute(
                "INSERT INTO contracts(box_id,advertiser,version,valid_from,"
                "comp_factor) VALUES(?,?, 'V1','2026-01-01T00:00:00+08:00',1.0)",
                (bid, bid + "广告主"))
        c.executemany(
            "INSERT INTO contracts(box_id,advertiser,version,valid_from,valid_to,"
            "comp_factor) VALUES(?,?,?,?,?,?)",
            [("B2", "旧广告主", "V1", "2026-01-01T00:00:00+08:00",
              "2026-10-01T00:00:00+08:00", 1.0),
             ("B2", "新广告主", "V2", "2026-10-01T00:00:00+08:00", None, 1.5)])
        c.commit()

    def set_time(self, *a):
        self.clock.set(dt(*a))

    def inspect(self, bid, who="li", match=True):
        return self.engine.inspect(bid, who, creative_match=match,
                                   take_photo=False)


class TestSchedule(CaseBase):
    def test_within_and_outside(self):
        self.set_time(2026, 10, 1, 20, 0)
        self.assertTrue(is_within_schedule(self.conn, "B1", self.clock.now()))
        self.set_time(2026, 10, 1, 12, 0)
        self.assertFalse(is_within_schedule(self.conn, "B1", self.clock.now()))
        self.set_time(2026, 10, 1, 17, 59)
        self.assertFalse(is_within_schedule(self.conn, "B1", self.clock.now()))
        self.set_time(2026, 10, 1, 23, 0)   # 半开区间，23:00 已结束
        self.assertFalse(is_within_schedule(self.conn, "B1", self.clock.now()))

    def test_cross_midnight(self):
        self.set_time(2026, 10, 1, 23, 30)
        self.assertTrue(is_within_schedule(self.conn, "B7", self.clock.now()))
        self.set_time(2026, 10, 2, 5, 30)
        self.assertTrue(is_within_schedule(self.conn, "B7", self.clock.now()))
        self.set_time(2026, 10, 2, 6, 0)
        self.assertFalse(is_within_schedule(self.conn, "B7", self.clock.now()))
        self.set_time(2026, 10, 2, 21, 0)
        self.assertFalse(is_within_schedule(self.conn, "B7", self.clock.now()))

    def test_daytime_excluded_from_overlap(self):
        # 12:00-13:00 与计划无重叠
        m = overlap_minutes(self.conn, "B1", dt(2026, 10, 1, 12),
                            dt(2026, 10, 1, 13))
        self.assertEqual(m, 0.0)
        # 12:00-19:00 只算 18:00-19:00
        m = overlap_minutes(self.conn, "B1", dt(2026, 10, 1, 12),
                            dt(2026, 10, 1, 19))
        self.assertEqual(m, 60.0)


class TestClassification(CaseBase):
    def test_daytime_normal_off_is_not_fault(self):
        self.set_time(2026, 10, 1, 12, 0)
        r = self.inspect("B1")
        self.assertEqual(r.classification, "daytime")
        self.assertIsNone(r.fault_id)

    def test_daytime_wrongly_lit_is_timer_fault(self):
        self.set_time(2026, 10, 1, 12, 0)
        r = self.inspect("B6")          # 强亮
        self.assertEqual(r.classification, "timer")

    def test_healthy(self):
        r = self.inspect("B1")
        self.assertEqual(r.classification, "healthy")

    def test_partial(self):
        r = self.inspect("B2")
        self.assertEqual(r.classification, "partial")

    def test_timer_off_during_schedule(self):
        r = self.inspect("B4")
        self.assertEqual(r.classification, "timer")

    def test_outage_on_interrupted_circuit(self):
        r = self.inspect("B3")
        self.assertEqual(r.classification, "outage")

    def test_creative_swapped_not_a_lighting_fault(self):
        r = self.inspect("B1", match=False)   # 画面撤换但灯亮
        self.assertEqual(r.classification, "creative")
        self.assertIsNone(r.fault_id)
        r2 = self.inspect("B3", match=False)  # 画面撤换优先于黑屏判断
        self.assertEqual(r2.classification, "creative")


class TestFaultMerging(CaseBase):
    def test_same_box_repeat_inspection_merges(self):
        r1 = self.inspect("B2")
        r2 = self.inspect("B2")
        self.assertEqual(r1.fault_id, r2.fault_id)
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM faults WHERE box_id='B2'").fetchone()["n"]
        self.assertEqual(n, 1)
        # 不平行第二张维修单
        self.engine.dispatch(r1.fault_id, "B2", "wang")
        with self.assertRaises(DomainError):
            self.engine.dispatch(r1.fault_id, "B2", "wang")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM repair_tickets WHERE box_id='B2'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_circuit_outage_merges_across_boxes(self):
        r1 = self.inspect("B3")
        r2 = self.inspect("B5")
        self.assertEqual(r1.fault_id, r2.fault_id)
        fid = r1.fault_id
        boxes = {x["box_id"] for x in self.conn.execute(
            "SELECT box_id FROM fault_boxes WHERE fault_id=?", (fid,))}
        self.assertEqual(boxes, {"B3", "B5"})
        # 同回路第三个点位再次首报，仍然归并，且不自动建工单
        self.conn.execute(
            "INSERT INTO boxes(id,name,addr,lat,lng,circuit_id,strips_total,"
            "creative_hex,sim_mode) VALUES('B9','B9','x',31.2,121.4,'C2',8,"
            "'#111','auto')")
        self.conn.execute(
            "INSERT INTO schedules(box_id,days,start_hm,end_hm,valid_from)"
            " VALUES('B9','1,2,3,4,5,6,7','18:00','23:00','2026-01-01')")
        r3 = self.inspect("B9")
        self.assertEqual(r3.fault_id, fid)
        self.assertIsNone(r3.ticket_id)

    def test_repairs_merge_into_one_open_ticket_per_box(self):
        r = self.inspect("B3")
        self.engine.dispatch(r.fault_id, "B3", "wang")
        with self.assertRaises(DomainError):
            self.engine.dispatch(r.fault_id, "B3", "wang")


class TestRelapse(CaseBase):
    def _repaired_ticket(self, bid="B3"):
        r = self.inspect(bid)               # outage（回路 C2 中断）
        tid = self.engine.dispatch(r.fault_id, bid, "wang")
        # 维修：恢复回路供电
        self.conn.execute(
            "UPDATE circuits SET status='normal' WHERE id='C2'")
        self.conn.commit()
        self.engine.complete_repair(tid, "wang")
        return r.fault_id, tid

    def test_verify_pass_closes(self):
        fid, tid = self._repaired_ticket()
        out = self.engine.verify(tid, "zhang")
        self.assertEqual(out.classification, "healthy")
        status = self.conn.execute(
            "SELECT status FROM repair_tickets WHERE id=?", (tid,)).fetchone()["status"]
        self.assertEqual(status, "verified")

    def test_verify_fail_reopens_as_relapse(self):
        fid, tid = self._repaired_ticket()
        # 回路再次中断后复验
        self.conn.execute(
            "UPDATE circuits SET status='interrupted' WHERE id='C2'")
        self.conn.commit()
        out = self.engine.verify(tid, "zhang")
        self.assertEqual(out.classification, "relapse")
        row = self.conn.execute(
            "SELECT t.status, f.kind FROM repair_tickets t JOIN faults f"
            " ON f.id=t.fault_id WHERE t.id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "reopened")
        self.assertEqual(row["kind"], "relapse")
        # 两段黑屏
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM blackout_segments WHERE fault_id=? AND box_id='B3'",
            (fid,)).fetchone()["n"]
        self.assertEqual(n, 2)
        # 不能对同箱再派平行工单（沿用重开单）
        with self.assertRaises(DomainError):
            self.engine.dispatch(fid, "B3", "wang")


class TestVerifyPermissions(CaseBase):
    def test_repair_role_cannot_verify(self):
        r = self.inspect("B3")
        tid = self.engine.dispatch(r.fault_id, "B3", "wang")
        self.conn.execute("UPDATE circuits SET status='normal' WHERE id='C2'")
        self.conn.commit()
        self.engine.complete_repair(tid, "wang")
        with self.assertRaises(DomainError):
            self.engine.verify(tid, "wang")     # 维修人=王维修

    def test_supervisor_cannot_verify(self):
        r = self.inspect("B3")
        tid = self.engine.dispatch(r.fault_id, "B3", "wang")
        self.conn.execute("UPDATE circuits SET status='normal' WHERE id='C2'")
        self.conn.commit()
        self.engine.complete_repair(tid, "wang")
        with self.assertRaises(DomainError):
            self.engine.verify(tid, "zhao")

    def test_verify_blocked_outside_schedule(self):
        r = self.inspect("B3")
        tid = self.engine.dispatch(r.fault_id, "B3", "wang")
        self.conn.execute("UPDATE circuits SET status='normal' WHERE id='C2'")
        self.conn.commit()
        self.engine.complete_repair(tid, "wang")
        self.set_time(2026, 10, 2, 12, 0)      # 白天，不可据此复验
        with self.assertRaises(DomainError):
            self.engine.verify(tid, "zhang")


class TestMakeup(CaseBase):
    def test_split_by_contract_version_and_half_deficit(self):
        # B2 半亮：9-30 22:00 起黑屏，跨 10-01 00:00 换版，至 10-01 20:00 评估
        r = self.inspect("B2")
        fid = r.fault_id
        seg = self.conn.execute(
            "SELECT id FROM blackout_segments WHERE fault_id=?",
            (fid,)).fetchone()
        self.conn.execute(
            "UPDATE blackout_segments SET start_ts=?, end_ts=? WHERE id=?",
            (to_iso(dt(2026, 9, 30, 22)), to_iso(dt(2026, 9, 30, 22)),
             seg["id"]))
        self.conn.commit()
        shares = {v.version: v for v in
                  makeup.compute_for_box(self.conn, self.clock, fid, "B2")}
        # V1：22:00-23:00 = 60 分钟半亮 → 30
        self.assertEqual(shares["V1"].blackout_minutes, 60.0)
        self.assertEqual(shares["V1"].weighted_minutes, 30.0)
        # V2：10-01 18:00 起到 20:00，白天不计 = 120 分钟 → 60 ×1.5 = 90
        self.assertEqual(shares["V2"].blackout_minutes, 120.0)
        self.assertEqual(shares["V2"].weighted_minutes, 60.0)
        self.assertEqual(shares["V2"].credit, 90.0)

    def test_outage_full_deficit_and_circuit_totals(self):
        r1 = self.inspect("B3")
        fid = r1.fault_id
        self.inspect("B5")
        # 段起点拨到 18:00，评估时刻 20:00 → 每箱 120 分钟全黑
        self.conn.execute(
            "UPDATE blackout_segments SET start_ts=?, end_ts=start_ts",
            (to_iso(dt(2026, 10, 1, 18)),))
        self.conn.commit()
        data = makeup.compute_fault(self.conn, self.clock, fid)
        self.assertEqual(data["totals"]["blackout"], 240.0)
        self.assertEqual(data["totals"]["weighted"], 240.0)
        self.assertAlmostEqual(data["totals"]["credit"], 240.0)

    def test_relapse_segments_accumulate(self):
        self.set_time(2026, 10, 1, 18, 0)
        r = self.inspect("B3")
        fid = r.fault_id
        tid = self.engine.dispatch(fid, "B3", "wang")
        self.set_time(2026, 10, 1, 19, 0)
        self.conn.execute("UPDATE circuits SET status='normal' WHERE id='C2'")
        self.conn.commit()
        self.engine.complete_repair(tid, "wang")      # 第一段: 18-19 = 60 分
        self.set_time(2026, 10, 1, 20, 0)
        self.conn.execute("UPDATE circuits SET status='interrupted' WHERE id='C2'")
        self.conn.commit()
        self.engine.verify(tid, "zhang")             # relapse，第二段进行中
        shares = makeup.compute_for_box(self.conn, self.clock, fid, "B3")
        self.assertTrue(shares)
        self.assertEqual(round(shares[0].blackout_minutes), 60)
        data = makeup.compute_fault(self.conn, self.clock, fid)
        self.assertTrue(data["ongoing"])

    def test_propose_and_approval_separation(self):
        r = self.inspect("B2")
        fid = r.fault_id
        # 只有巡查员能提交
        with self.assertRaises(makeup.MakeupError):
            makeup.propose(self.conn, self.clock, fid, "wang")
        cid = makeup.propose(self.conn, self.clock, fid, "li")
        # 提交人不能批准自己的结论
        with self.assertRaises(makeup.MakeupError):
            makeup.decide(self.conn, self.clock, cid, "li", True)
        # 另一名巡查员可以批准
        makeup.decide(self.conn, self.clock, cid, "zhang", True)
        row = self.conn.execute(
            "SELECT status, decided_by FROM makeup_conclusions WHERE id=?",
            (cid,)).fetchone()
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["decided_by"], "zhang")
        # 已审批不可重复审批
        with self.assertRaises(makeup.MakeupError):
            makeup.decide(self.conn, self.clock, cid, "zhao", False)

    def test_supervisor_may_approve_others(self):
        r = self.inspect("B2")
        cid = makeup.propose(self.conn, self.clock, r.fault_id, "li")
        makeup.decide(self.conn, self.clock, cid, "zhao", False)
        row = self.conn.execute(
            "SELECT status FROM makeup_conclusions WHERE id=?",
            (cid,)).fetchone()
        self.assertEqual(row["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
