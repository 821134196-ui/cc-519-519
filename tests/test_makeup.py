"""补播核算测试：按合同版本切片、仅计计划时段、半亮缺额折算；
复验/审批权限：巡查员不得批准自己的结论。"""
from __future__ import annotations

from lightbox import clock
from lightbox import faults as svc

from tests._fixture import BaseCase, DAY, HOUR


def monday(y=2026, m=9, d=28) -> int:
    t = clock.parse_local(f"{y}-{m:02d}-{d:02d}T00:00")
    return t - clock.dt_of(t).weekday() * DAY


def run_fault_to_close(fx, *, code="BX-3", who="甲巡", verifier="乙巡",
                       onset_hm=19 * HOUR + 30, detect_hm=19 * HOUR + 40,
                       done_hm=21 * HOUR, verify_hm=21 * HOUR + 10,
                       brightness=0.0, bars=0):
    """完整闭环：巡检 → 派单 → 维修 → 复验，返回 (b, fault, close_min)。"""
    conn = fx.conn
    b = monday()
    fx.add_schedule(code, 1, "18:00", "23:00")
    fx.add_contract(code, "A商", b - DAY, b + 30 * DAY, rate=2.0)
    if brightness <= 0:
        fx.adapter.set_override(fx.boxes[code], 0.0, 0, b + onset_hm)
    else:
        fx.adapter.set_override(fx.boxes[code], brightness, bars, b + onset_hm)
    _, f, kind = svc.inspect(
        conn, lightbox_id=fx.boxes[code], inspector_id=fx.inspectors[who],
        ts_min=b + detect_hm, brightness=brightness, lit_bars=bars, total_bars=4,
        adapter=fx.adapter, signal_set_min=b + onset_hm)
    wo = svc.dispatch_work_order(conn, fault_id=f["id"], assignee="钱师傅",
                                 created_min=b + detect_hm + 5)
    svc.complete_repair(conn, work_order_id=wo["id"], done_min=b + done_hm,
                        adapter=fx.adapter)
    res = svc.reverify(conn, fault_id=f["id"], inspector_id=fx.inspectors[verifier],
                       ts_min=b + verify_hm, adapter=fx.adapter)
    assert res["passed"], res
    return b, f, b + verify_hm


class MakeupCalcTest(BaseCase):
    def test_full_blackout_counts_only_scheduled_minutes(self):
        b, f, close = run_fault_to_close(
            self.fx, onset_hm=19 * HOUR + 30, detect_hm=19 * HOUR + 40,
            done_hm=21 * HOUR, verify_hm=21 * HOUR + 10)
        rows = svc.compute_makeup(self.fx.conn, f["id"])
        self.assertEqual(len(rows), 1)
        # 19:30 → 21:10 = 100 分钟，全在计划时段
        self.assertEqual(rows[0]["owed_minutes"], 100)
        self.assertEqual(rows[0]["planned_minutes"], 100)
        self.assertEqual(rows[0]["amount"], 200.0)

    def test_blackout_clipped_to_schedule_window(self):
        # 黑屏起在计划前、复验关在计划结束前：起点被钳制到计划开始 18:00
        conn = self.fx.conn
        b = monday()
        self.fx.add_schedule("BX-3", 1, "18:00", "23:00")
        self.fx.add_contract("BX-3", "A商", b - DAY, b + 30 * DAY)
        # override 16:00 起；白天/计划前不计
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0, 0, b + 16 * HOUR)
        _, f, kind = svc.inspect(
            conn, lightbox_id=self.fx.boxes["BX-3"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=b + 20 * HOUR,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=b + 16 * HOUR)
        self.assertEqual(kind, "timer_misconfig")
        self.assertEqual(f["start_min"], b + 16 * HOUR)
        wo = svc.dispatch_work_order(conn, fault_id=f["id"], assignee="钱师傅",
                                     created_min=b + 20 * HOUR + 5)
        svc.complete_repair(conn, work_order_id=wo["id"], done_min=b + 22 * HOUR,
                            adapter=self.fx.adapter)
        res = svc.reverify(conn, fault_id=f["id"],
                           inspector_id=self.fx.inspectors["乙巡"],
                           ts_min=b + 22 * HOUR + 50, adapter=self.fx.adapter)
        self.assertTrue(res["passed"])
        rows = svc.compute_makeup(self.fx.conn, f["id"])
        # 计划交集：18:00–22:50 = 290 分钟（16:00–18:00 不计）
        self.assertEqual(rows[0]["planned_minutes"], 4 * HOUR + 50)

    def test_partial_strip_deficit_ratio(self):
        # 2/4 灯带亮 → 缺额 50%
        b, f, close = run_fault_to_close(
            self.fx, brightness=0.5, bars=2,
            onset_hm=20 * HOUR, detect_hm=20 * HOUR + 5,
            done_hm=22 * HOUR, verify_hm=22 * HOUR + 10)
        rows = svc.compute_makeup(self.fx.conn, f["id"])
        # 20:00 → 22:10 = 130 分钟计划黑屏窗口 × 50% = 65
        self.assertEqual(rows[0]["deficit"], 0.5)
        self.assertEqual(rows[0]["owed_minutes"], 65)

    def test_sliced_by_contract_version(self):
        """故障横跨两个合同版本：补播必须分别归到对应版本；合同空档不计。"""
        conn = self.fx.conn
        b = monday()
        self.fx.add_schedule("BX-3", 1, "18:00", "23:00")
        self.fx.add_schedule("BX-3", 2, "18:00", "23:00")
        # 旧版：到周一 21:00；空档 21:00–24:00；新版周二 00:00 起
        self.fx.add_contract("BX-3", "A商", b - DAY, b + 21 * HOUR - 1, rate=2.0,
                             artwork="旧版画面")
        self.fx.add_contract("BX-3", "A商", b + DAY, b + 30 * DAY, rate=3.0,
                             artwork="新版画面")
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0, 0, b + 20 * HOUR)
        _, f, _ = svc.inspect(
            conn, lightbox_id=self.fx.boxes["BX-3"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=b + 20 * HOUR + 5,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=b + 20 * HOUR)
        # 手工指定关闭时刻为周二 21:00，直接算（不必走复验）
        conn.execute("UPDATE faults SET end_min=? WHERE id=?", (b + DAY + 21 * HOUR, f["id"]))
        conn.commit()
        rows = svc.compute_makeup(conn, f["id"])
        by_artwork = {r["artwork_name"]: r for r in rows}
        self.assertEqual(set(by_artwork), {"旧版画面", "新版画面"})
        # 旧版：周一 20:00–21:00 = 60 分钟
        self.assertEqual(by_artwork["旧版画面"]["planned_minutes"], 60)
        self.assertEqual(by_artwork["旧版画面"]["amount"], 120.0)
        # 新版：周二 18:00–21:00 = 180 分钟；周一 21:00–23:00 的空档不计
        self.assertEqual(by_artwork["新版画面"]["planned_minutes"], 180)
        self.assertEqual(by_artwork["新版画面"]["amount"], 540.0)


class ApprovalPermissionTest(BaseCase):
    def _closed_fault(self):
        return run_fault_to_close(self.fx, who="甲巡", verifier="乙巡",
                                  onset_hm=19 * HOUR + 30, detect_hm=19 * HOUR + 40,
                                  done_hm=21 * HOUR, verify_hm=21 * HOUR + 10)

    def test_proposer_cannot_approve_own_makeup(self):
        b, f, _ = self._closed_fault()
        # 补播由上报人 甲巡 提出；甲巡本人不能批准
        with self.assertRaises(svc.PermissionDenied):
            svc.approve_makeup(self.fx.conn, fault_id=f["id"],
                               inspector_id=self.fx.inspectors["甲巡"],
                               ts_min=b + 22 * HOUR)
        # 仍是待批
        st = self.fx.conn.execute(
            "SELECT DISTINCT status FROM makeup_credits WHERE fault_id=?", (f["id"],)
        ).fetchone()["status"]
        self.assertEqual(st, "proposed")

    def test_other_inspector_approves(self):
        b, f, _ = self._closed_fault()
        rows = svc.approve_makeup(self.fx.conn, fault_id=f["id"],
                                  inspector_id=self.fx.inspectors["丙巡"],
                                  ts_min=b + 22 * HOUR, approve=True)
        self.assertTrue(all(r["status"] == "approved" for r in rows))
        self.assertTrue(all(r["approved_by"] == self.fx.inspectors["丙巡"] for r in rows))

    def test_reporter_cannot_reverify_own_report(self):
        conn = self.fx.conn
        b = monday()
        self.fx.add_schedule("BX-3", 1, "18:00", "23:00")
        self.fx.add_contract("BX-3", "A商", b - DAY, b + 30 * DAY)
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0, 0, b + 20 * HOUR)
        _, f, _ = svc.inspect(
            conn, lightbox_id=self.fx.boxes["BX-3"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=b + 20 * HOUR + 5,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=b + 20 * HOUR)
        wo = svc.dispatch_work_order(conn, fault_id=f["id"], assignee="钱师傅",
                                     created_min=b + 20 * HOUR + 10)
        svc.complete_repair(conn, work_order_id=wo["id"], done_min=b + 21 * HOUR,
                            adapter=self.fx.adapter)
        # 甲巡复验自己的上报 → 拒绝
        with self.assertRaises(svc.PermissionDenied):
            svc.reverify(conn, fault_id=f["id"],
                         inspector_id=self.fx.inspectors["甲巡"],
                         ts_min=b + 21 * HOUR + 5, adapter=self.fx.adapter)
        # 换乙巡复验通过
        res = svc.reverify(conn, fault_id=f["id"],
                           inspector_id=self.fx.inspectors["乙巡"],
                           ts_min=b + 21 * HOUR + 10, adapter=self.fx.adapter)
        self.assertTrue(res["passed"])

    def test_reverify_outside_schedule_rejected(self):
        conn = self.fx.conn
        b = monday()
        self.fx.add_schedule("BX-3", 1, "18:00", "23:00")
        self.fx.add_contract("BX-3", "A商", b - DAY, b + 30 * DAY)
        self.fx.adapter.set_override(self.fx.boxes["BX-3"], 0, 0, b + 20 * HOUR)
        _, f, _ = svc.inspect(
            conn, lightbox_id=self.fx.boxes["BX-3"],
            inspector_id=self.fx.inspectors["甲巡"], ts_min=b + 20 * HOUR + 5,
            brightness=0, lit_bars=0, total_bars=4,
            adapter=self.fx.adapter, signal_set_min=b + 20 * HOUR)
        wo = svc.dispatch_work_order(conn, fault_id=f["id"], assignee="钱师傅",
                                     created_min=b + 20 * HOUR + 10)
        svc.complete_repair(conn, work_order_id=wo["id"], done_min=b + 21 * HOUR,
                            adapter=self.fx.adapter)
        # 次日白天申请复验：计划时段外，不允许判定
        with self.assertRaises(svc.DomainError):
            svc.reverify(conn, fault_id=f["id"],
                         inspector_id=self.fx.inspectors["乙巡"],
                         ts_min=b + DAY + 10 * HOUR, adapter=self.fx.adapter)


if __name__ == "__main__":
    import unittest
    unittest.main()
