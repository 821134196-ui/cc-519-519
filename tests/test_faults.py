"""故障分类与归并测试：白天误判/定时器/半亮/供电中断归并/画面撤换/
重复维修单拦截/维修后再次熄灭。"""
from __future__ import annotations

from lightbox import clock
from lightbox import faults as svc
from lightbox.db import FAULT_KINDS

from tests._fixture import BaseCase, DAY, HOUR, MIN


def monday(y=2026, m=9, d=28) -> int:
    t = clock.parse_local(f"{y}-{m:02d}-{d:02d}T00:00")
    return t - clock.dt_of(t).weekday() * DAY


class ClassificationTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.b = monday()
        for code in ("BX-1", "BX-2", "BX-3"):
            self.fx.add_schedule(code, 1, "18:00", "23:00")
        # 周一全档合同
        for code in ("BX-1", "BX-2", "BX-3"):
            self.fx.add_contract(code, "A商", self.b - DAY, self.b + 30 * DAY)

    def inspect(self, code, ts, br, bars=4, who="甲巡", set_min=None):
        return svc.inspect(
            self.fx.conn, lightbox_id=self.fx.boxes[code],
            inspector_id=self.fx.inspectors[who], ts_min=ts,
            brightness=br, lit_bars=bars, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=set_min,
        )

    def test_daytime_false_positive_creates_no_fault(self):
        insp, fault, kind = self.inspect("BX-3", self.b + 10 * HOUR, 0.0, 0)
        self.assertEqual(kind, "daytime_false_positive")
        self.assertIsNone(fault)
        self.assertIsNone(insp["fault_id"])

    def test_timer_misconfig_when_standalone_dark(self):
        # 同回路邻箱 BX-2 正常亮 → 不是供电问题
        _, fault, kind = self.inspect("BX-1", self.b + 20 * HOUR, 0.0, 0,
                                      set_min=self.b + 20 * HOUR)
        self.assertEqual(kind, "timer_misconfig")
        self.assertEqual(fault["status"], "open")
        self.assertEqual(fault["lightbox_id"], self.fx.boxes["BX-1"])

    def test_strip_partial(self):
        _, fault, kind = self.inspect("BX-3", self.b + 20 * HOUR, 0.5, 2,
                                      set_min=self.b + 20 * HOUR)
        self.assertEqual(kind, "strip_partial")
        self.assertEqual(fault["kind"], "strip_partial")

    def test_artwork_removed_when_no_contract(self):
        # BX-3 合同空档
        self.fx.conn.execute("DELETE FROM contracts WHERE lightbox_id=?",
                             (self.fx.boxes["BX-3"],))
        self.fx.conn.commit()
        _, fault, kind = self.inspect("BX-3", self.b + 20 * HOUR, 0.0, 0)
        self.assertEqual(kind, "artwork_removed")
        self.assertIsNotNone(fault)
        # 不挂广告主、不允许派修
        rows = self.fx.conn.execute(
            "SELECT COUNT(*) n FROM fault_advertisers WHERE fault_id=?", (fault["id"],)
        ).fetchone()
        self.assertEqual(rows["n"], 0)
        with self.assertRaises(svc.DomainError):
            svc.dispatch_work_order(self.fx.conn, fault_id=fault["id"],
                                    assignee="钱师傅", created_min=self.b + 21 * HOUR)

    def test_normal_bright_creates_no_fault(self):
        _, fault, kind = self.inspect("BX-3", self.b + 20 * HOUR, 1.0, 4)
        self.assertEqual(kind, "normal")
        self.assertIsNone(fault)

    def test_artwork_fault_auto_closes_when_new_artwork_lit(self):
        # 空档期巡检 → 画面已撤换
        self.fx.conn.execute("DELETE FROM contracts WHERE lightbox_id=?",
                             (self.fx.boxes["BX-3"],))
        self.fx.conn.commit()
        _, f, kind = self.inspect("BX-3", self.b + 20 * HOUR, 0.0, 0)
        self.assertEqual(kind, "artwork_removed")
        # 新合同上线 + 计划时段满亮 → 自动关闭
        self.fx.add_schedule("BX-3", 2, "18:00", "23:00")
        self.fx.add_contract("BX-3", "B商", self.b + DAY, self.b + 30 * DAY)
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 1.0, 4,
                                     self.b + DAY + 18 * HOUR)
        _, f2, kind2 = self.inspect("BX-3", self.b + DAY + 20 * HOUR, 1.0, 4,
                                    who="乙巡")
        self.assertEqual(kind2, "normal")
        self.assertIsNone(f2)
        st = self.fx.conn.execute("SELECT status FROM faults WHERE id=?",
                                  (f["id"],)).fetchone()["status"]
        self.assertEqual(st, "verified_closed")


class PowerOutageMergeTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.b = monday()
        for code in ("BX-1", "BX-2"):
            self.fx.add_schedule(code, 1, "18:00", "23:00")
        self.fx.add_contract("BX-1", "A商", self.b - DAY, self.b + 30 * DAY)
        self.fx.add_contract("BX-2", "B商", self.b - DAY, self.b + 30 * DAY)

    def test_same_circuit_dark_boxes_merge_to_one_fault(self):
        t0 = self.b + 20 * HOUR
        self.fx.adapter.set_override(self.fx.boxes["BX-1"], 0.0, 0, t0)
        self.fx.adapter.set_override(self.fx.boxes["BX-2"], 0.0, 0, t0)

        _, f1, k1 = svc.inspect(
            self.fx.conn, lightbox_id=self.fx.boxes["BX-1"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=t0 + 5 * MIN,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=t0)
        self.assertEqual(k1, "power_outage")
        self.assertEqual(f1["circuit_id"], self.fx.circuit)

        _, f2, k2 = svc.inspect(
            self.fx.conn, lightbox_id=self.fx.boxes["BX-2"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=t0 + 10 * MIN,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=t0)
        self.assertEqual(k2, "power_outage")
        # 第二只箱子归入同一张回路主单，而不是新建
        self.assertEqual(f2["id"], f1["id"])

        n = self.fx.conn.execute("SELECT COUNT(*) n FROM faults WHERE kind='power_outage'").fetchone()
        self.assertEqual(n["n"], 1)
        # 两个受影响广告主（两只箱子）都挂在主单上
        affected = {r["lightbox_id"] for r in self.fx.conn.execute(
            "SELECT lightbox_id FROM fault_advertisers WHERE fault_id=?", (f1["id"],))}
        self.assertEqual(affected, {self.fx.boxes["BX-1"], self.fx.boxes["BX-2"]})

    def test_peer_lit_means_timer_not_outage(self):
        t0 = self.b + 20 * HOUR
        self.fx.adapter.set_override(self.fx.boxes["BX-1"], 0.0, 0, t0)
        # BX-2 不设 override → 计划时段内满亮
        _, f1, k1 = svc.inspect(
            self.fx.conn, lightbox_id=self.fx.boxes["BX-1"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=t0 + 5,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=t0)
        self.assertEqual(k1, "timer_misconfig")


class DuplicateWorkOrderTest(BaseCase):
    def _open_fault(self):
        b = monday()
        self.fx.add_schedule("BX-3", 1, "18:00", "23:00")
        self.fx.add_contract("BX-3", "A商", b - DAY, b + 30 * DAY)
        _, f, _ = svc.inspect(
            self.fx.conn, lightbox_id=self.fx.boxes["BX-3"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=b + 20 * HOUR,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=b + 20 * HOUR)
        return b, f

    def test_cannot_open_two_parallel_work_orders(self):
        b, f = self._open_fault()
        svc.dispatch_work_order(self.fx.conn, fault_id=f["id"], assignee="钱师傅",
                                created_min=b + 20 * HOUR + 10)
        with self.assertRaises(svc.DomainError):
            svc.dispatch_work_order(self.fx.conn, fault_id=f["id"], assignee="孙师傅",
                                    created_min=b + 20 * HOUR + 20)

    def test_redispatch_allowed_after_failed_reverify(self):
        """维修后再次熄灭：复验不通过后允许重新派单，最终复验关闭。"""
        b, f = self._open_fault()
        t0 = b + 20 * HOUR
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0.0, 0, t0)

        wo = svc.dispatch_work_order(self.fx.conn, fault_id=f["id"],
                                     assignee="钱师傅", created_min=t0 + 10)
        svc.complete_repair(self.fx.conn, work_order_id=wo["id"],
                            done_min=t0 + 40, adapter=self.fx.adapter)
        # 修复后 3 分钟再次失电
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0.0, 0, t0 + 43)
        res = svc.reverify(self.fx.conn, fault_id=f["id"],
                           inspector_id=self.fx.inspectors["乙巡"],
                           ts_min=t0 + 50, adapter=self.fx.adapter)
        self.assertFalse(res["passed"])
        f = self.fx.conn.execute("SELECT * FROM faults WHERE id=?", (f["id"],)).fetchone()
        self.assertEqual(f["status"], "rejected")
        note = self.fx.conn.execute(
            "SELECT note FROM inspections WHERE fault_id=? ORDER BY id DESC LIMIT 1",
            (f["id"],)).fetchone()["note"]
        self.assertIn("再次熄灭", note)

        # 旧单已 done，可以再派一张
        wo2 = svc.dispatch_work_order(self.fx.conn, fault_id=f["id"],
                                      assignee="孙师傅", created_min=t0 + 60)
        svc.complete_repair(self.fx.conn, work_order_id=wo2["id"],
                            done_min=t0 + 90, adapter=self.fx.adapter)
        res2 = svc.reverify(self.fx.conn, fault_id=f["id"],
                            inspector_id=self.fx.inspectors["乙巡"],
                            ts_min=t0 + 100, adapter=self.fx.adapter)
        self.assertTrue(res2["passed"])
        f = self.fx.conn.execute("SELECT * FROM faults WHERE id=?", (f["id"],)).fetchone()
        self.assertEqual(f["status"], "verified_closed")


if __name__ == "__main__":
    import unittest
    unittest.main()
