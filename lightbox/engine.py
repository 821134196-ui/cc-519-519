"""业务引擎：巡检分类、故障归并、维修派单、复验权限。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .clock import Clock, from_iso, to_iso
from .schedule import is_within_schedule
from .signal_ import SimulatedSignalAdapter
from .photos import render_photo

# 非故障结论（不落 faults）
DAYTIME = "daytime"      # 白天/计划外，本就该灭
CREATIVE = "creative"    # 画面已撤换，亮灯本身正常

FAULT_KINDS = ("outage", "partial", "timer", "relapse")
KIND_TEXT = {
    "outage": "供电中断/全黑",
    "partial": "部分灯带不亮",
    "timer": "定时器配置错",
    "relapse": "维修后再次熄灭",
    DAYTIME: "计划外时段（非故障）",
    CREATIVE: "画面已撤换（非亮灯故障）",
}


class DomainError(Exception):
    """业务规则冲突。"""


@dataclass
class InspectionResult:
    inspection_id: int
    classification: str
    fault_id: Optional[int]
    ticket_id: Optional[int]
    message: str


class Engine:
    def __init__(self, conn: sqlite3.Connection, clock: Clock,
                 photo_dir: str = "data/photos"):
        self.conn = conn
        self.clock = clock
        self.photo_dir = photo_dir
        self.adapter = SimulatedSignalAdapter(conn, clock)

    # ---------------- 巡检与分类 ----------------

    def classify(self, box_id: str, *, fraction: float,
                 within_schedule: bool, creative_match: bool) -> str:
        box = self.conn.execute(
            "SELECT b.*, c.status AS circuit_status FROM boxes b "
            "JOIN circuits c ON c.id = b.circuit_id WHERE b.id = ?",
            (box_id,),
        ).fetchone()
        if box is None:
            raise DomainError(f"未知灯箱 {box_id}")

        if not within_schedule:
            # 计划外：灭是正常的；亮着说明定时器错（白天误亮）
            return "timer" if fraction > 0.001 else DAYTIME

        if not creative_match:
            # 计划内但画面已撤换：不按亮灯故障处理
            return CREATIVE

        if fraction <= 0.001:
            return "outage" if box["circuit_status"] == "interrupted" else "timer"
        if fraction < 0.999:
            return "partial"
        return "healthy"  # 计划内正常点亮

    def inspect(self, box_id: str, inspector: str, *,
                creative_match: Optional[bool] = None,
                note: str = "", take_photo: bool = True) -> InspectionResult:
        self._require_role(inspector, "inspector")
        now = self.clock.now()
        box = self.conn.execute(
            "SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
        contract = self._current_contract(box_id, now)
        if creative_match is None:
            creative_match = True
        sig = self.adapter.read(box_id, source="inspect")
        within = sig.within_schedule
        cls = self.classify(
            box_id, fraction=sig.fraction, within_schedule=within,
            creative_match=creative_match,
        )

        photo_path = None
        if take_photo:
            label = (contract["advertiser"] if contract else "NO CONTRACT")
            fname = f"{box_id}_{to_iso(now).replace(':','').replace('-','')}.png"
            photo_path = render_photo(
                f"{self.photo_dir}/{fname}",
                box["creative_hex"], sig.fraction, sig.strips_on,
                box["strips_total"], within, creative_match, label,
            )

        cur = self.conn.execute(
            "INSERT INTO inspections(box_id, ts, inspector, within_schedule,"
            " fraction, strips_on, creative_match, classification, photo_path,"
            " note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (box_id, to_iso(now), inspector, int(within), sig.fraction,
             sig.strips_on, int(creative_match), cls, photo_path, note),
        )
        insp_id = cur.lastrowid
        fault_id = ticket_id = None
        message = ""

        if cls in (DAYTIME, CREATIVE):
            message = ("计划外时段灯箱按计划熄灭，判为非故障"
                       if cls == DAYTIME else
                       "现场画面与登记合同不符（已撤换），转合同/画面处理，不开维修单")
        elif cls == "healthy":
            # 若该灯箱有已修复待复验单，复验点亮
            message = "巡检正常：灯箱全亮"
        else:
            fault_id, ticket_id, message = self._register_fault(
                box_id, cls, now, note)
            self.conn.execute(
                "UPDATE inspections SET fault_id=? WHERE id=?",
                (fault_id, insp_id))

        self.conn.commit()
        return InspectionResult(insp_id, cls, fault_id, ticket_id, message)

    def _register_fault(self, box_id: str, kind: str,
                        now: datetime, note: str):
        """归并到开放故障或建单；必要时创建维修单。

        - 同回路中断(outage) => 归并到同一回路故障
        - 同灯箱同故障开放中 => 追加证据，不平行建单
        - 已报修复但复验再灭 => relapse，重开原单
        """
        box = self.conn.execute(
            "SELECT * FROM boxes WHERE id=?", (box_id,)).fetchone()
        circuit_id = box["circuit_id"]
        ts = to_iso(now)

        # 1) 维修后再次熄灭：该箱存在 repaired 工单
        repaired = self.conn.execute(
            "SELECT t.id, t.fault_id FROM repair_tickets t "
            "WHERE t.box_id=? AND t.status='repaired' ORDER BY id DESC LIMIT 1",
            (box_id,),
        ).fetchone()
        if repaired is not None:
            self.conn.execute(
                "UPDATE repair_tickets SET status='reopened' WHERE id=?",
                (repaired["id"],))
            self._event(repaired["id"], "system", "reopen",
                        "复验发现仍熄灭/不达标，维修单重开")
            self._open_segment(repaired["fault_id"], box_id, now,
                               deficit=self._deficit(kind))
            self.conn.execute(
                "UPDATE faults SET kind='relapse', status='open', "
                "resolved_at=NULL, last_seen_at=?, blackout_start=? WHERE id=?",
                (ts, ts, repaired["fault_id"]))
            return repaired["fault_id"], repaired["id"], \
                "维修后再次熄灭：原维修单重开（relapse），不计新单"

        # 2) 回路中断归并
        existing = None
        if kind == "outage":
            existing = self.conn.execute(
                "SELECT * FROM faults WHERE circuit_id=? AND status='open' "
                "AND kind='outage' ORDER BY id LIMIT 1",
                (circuit_id,),
            ).fetchone()

        # 3) 同箱同故障归并（partial 加重为 outage 也算同一起）
        if existing is None:
            existing = self.conn.execute(
                "SELECT * FROM faults WHERE box_id=? AND status='open' "
                "AND kind IN ('outage','partial','timer') ORDER BY id LIMIT 1",
                (box_id,),
            ).fetchone()

        if existing is not None:
            self.conn.execute(
                "UPDATE faults SET last_seen_at=?, kind=? WHERE id=?",
                (ts, "outage" if (existing["kind"] == "partial"
                                  and kind == "outage") else existing["kind"],
                 existing["id"]))
            self.conn.execute(
                "INSERT OR IGNORE INTO fault_boxes(fault_id, box_id) VALUES(?,?)",
                (existing["id"], box_id))
            # 新并入的点位（如同回路另一箱）从本次首报起计自己的黑屏段
            open_seg = self.conn.execute(
                "SELECT 1 FROM blackout_segments WHERE fault_id=? AND box_id=?"
                " AND end_ts=start_ts",
                (existing["id"], box_id)).fetchone()
            if open_seg is None:
                self._open_segment(existing["id"], box_id, now,
                                   deficit=self._deficit(kind))
            ticket = self.conn.execute(
                "SELECT id FROM repair_tickets WHERE fault_id=? AND box_id=? "
                "AND status IN ('dispatched','repaired','reopened')",
                (existing["id"], box_id),
            ).fetchone()
            tid = ticket["id"] if ticket else None
            return existing["id"], tid, "故障仍在处理中：归并到既有故障单，未重复建单"

        # 4) 新建故障
        cur = self.conn.execute(
            "INSERT INTO faults(box_id, circuit_id, kind, status,"
            " first_seen_at, last_seen_at, blackout_start) "
            "VALUES (?,?,?, 'open', ?, ?, ?)",
            (box_id, circuit_id if kind == "outage" else None, kind,
             ts, ts, ts),
        )
        fid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO fault_boxes(fault_id, box_id) VALUES(?,?)",
            (fid, box_id))
        self._open_segment(fid, box_id, now, deficit=self._deficit(kind))
        return fid, None, f"新故障单 #{fid}（{KIND_TEXT[kind]}）"

    @staticmethod
    def _deficit(kind: str) -> float:
        return 0.5 if kind == "partial" else 1.0

    def _open_segment(self, fault_id: int, box_id: str,
                      start: datetime, deficit: float) -> None:
        self.conn.execute(
            "INSERT INTO blackout_segments(fault_id, box_id, start_ts, end_ts,"
            " deficit) VALUES (?,?,?, ?,?)",
            (fault_id, box_id, to_iso(start), to_iso(start), deficit),
        )

    # ---------------- 派单 / 维修 / 复验 ----------------

    def open_ticket_for_box(self, box_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM repair_tickets WHERE box_id=? "
            "AND status IN ('dispatched','repaired','reopened') "
            "ORDER BY id DESC LIMIT 1",
            (box_id,),
        ).fetchone()

    def dispatch(self, fault_id: int, box_id: str, assignee: str,
                 expected_restore: Optional[datetime] = None) -> int:
        self._require_role(assignee, "repair")
        fault = self.conn.execute(
            "SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
        if fault is None:
            raise DomainError("故障不存在")
        if fault["status"] != "open":
            raise DomainError("故障已关闭，不能派单")
        if self.open_ticket_for_box(box_id) is not None:
            raise DomainError(f"{box_id} 已有未关闭维修单，禁止平行创建相同维修单")
        ts = to_iso(self.clock.now())
        cur = self.conn.execute(
            "INSERT INTO repair_tickets(fault_id, box_id, status, created_at,"
            " assignee, expected_restore) VALUES (?,?, 'dispatched', ?,?,?)",
            (fault_id, box_id, ts, assignee,
             to_iso(expected_restore) if expected_restore else None),
        )
        tid = cur.lastrowid
        self._event(tid, assignee, "dispatch",
                    f"派单至 {assignee}，预计恢复 "
                    f"{expected_restore:%m-%d %H:%M}" if expected_restore
                    else f"派单至 {assignee}")
        self.conn.execute(
            "INSERT OR IGNORE INTO fault_boxes(fault_id, box_id) VALUES(?,?)",
            (fault_id, box_id))
        if not fault["expected_restore"] and expected_restore:
            self.conn.execute(
                "UPDATE faults SET expected_restore=? WHERE id=?",
                (to_iso(expected_restore), fault_id))
        self.conn.commit()
        return tid

    def complete_repair(self, ticket_id: int, actor: str,
                        note: str = "") -> None:
        self._require_role(actor, "repair")
        t = self._get_ticket(ticket_id)
        if t["status"] not in ("dispatched", "reopened"):
            raise DomainError(f"工单状态 {t['status']}，不能填报修复")
        now = self.clock.now()
        self.conn.execute(
            "UPDATE repair_tickets SET status='repaired', repaired_at=?,"
            " repair_by=?, note=? WHERE id=?",
            (to_iso(now), actor, note, ticket_id))
        self._event(ticket_id, actor, "repair", note or "已修复，待复验")
        # 关闭当前黑屏段（按修复时刻）
        self._close_segments(t["fault_id"], t["box_id"], now)
        self._maybe_mark_resolved(t["fault_id"], now)
        self.conn.commit()

    def verify(self, ticket_id: int, inspector: str) -> InspectionResult:
        """复验：必须巡查员且不能是维修者本人；亮则通过，灭则重开。"""
        self._require_role(inspector, "inspector")
        t = self._get_ticket(ticket_id)
        if t["repair_by"] == inspector:
            raise DomainError("维修人不能复验自己的维修结果，请由其他巡查员复验")
        if t["status"] != "repaired":
            raise DomainError(f"工单状态 {t['status']}，不可复验")

        # 复验必须发生在计划亮灯时段，否则白天看不出问题
        now = self.clock.now()
        if not is_within_schedule(self.conn, t["box_id"], now):
            raise DomainError("当前不在计划亮灯时段，无法据此复验亮灯")

        sig = self.adapter.read(t["box_id"], source="verify")
        if sig.fraction >= 0.999:
            self.conn.execute(
                "UPDATE repair_tickets SET status='verified', verified_at=?,"
                " verify_by=?, closed_at=? WHERE id=?",
                (to_iso(now), inspector, to_iso(now), ticket_id))
            self._event(ticket_id, inspector, "verify_pass",
                        "复验全亮，工单关闭")
            self._close_segments(t["fault_id"], t["box_id"], now)
            self._maybe_mark_resolved(t["fault_id"], now)
            self.conn.commit()
            return InspectionResult(-1, "healthy", t["fault_id"], ticket_id,
                                    "复验通过：灯箱全亮")
        # 复验仍不亮 => relapse
        self.conn.execute(
            "UPDATE repair_tickets SET status='reopened' WHERE id=?",
            (ticket_id,))
        self._event(ticket_id, inspector, "verify_fail",
                    f"复验不达标（{sig.fraction:.0%} 亮），重开")
        kind = "partial" if 0 < sig.fraction < 1 else "relapse"
        self._open_segment(t["fault_id"], t["box_id"], now,
                           deficit=self._deficit(kind))
        self.conn.execute(
            "UPDATE faults SET kind='relapse', status='open', resolved_at=NULL,"
            " last_seen_at=?, blackout_start=? WHERE id=?",
            (to_iso(now), to_iso(now), t["fault_id"]))
        self.conn.commit()
        return InspectionResult(-1, "relapse", t["fault_id"], ticket_id,
                                "复验未通过：灯箱仍异常，工单重开")

    def _close_segments(self, fault_id: int, box_id: str,
                        end: datetime) -> None:
        rows = self.conn.execute(
            "SELECT id, start_ts, end_ts FROM blackout_segments "
            "WHERE fault_id=? AND box_id=? AND end_ts=start_ts",
            (fault_id, box_id),
        ).fetchall()
        for r in rows:
            if from_iso(r["start_ts"]) <= end:
                self.conn.execute(
                    "UPDATE blackout_segments SET end_ts=? WHERE id=?",
                    (to_iso(end), r["id"]))

    def _maybe_mark_resolved(self, fault_id: int, now: datetime) -> None:
        """该故障下所有点位工单都已修复/验证，才把故障标为已恢复(待复验)。"""
        pend = self.conn.execute(
            "SELECT COUNT(*) n FROM repair_tickets WHERE fault_id=? "
            "AND status IN ('dispatched','reopened')",
            (fault_id,),
        ).fetchone()["n"]
        if pend == 0:
            self.conn.execute(
                "UPDATE faults SET resolved_at=?, status='resolved' WHERE id=?"
                " AND status='open'",
                (to_iso(now), fault_id))

    # ---------------- 查询辅助 ----------------

    def _get_ticket(self, ticket_id: int) -> sqlite3.Row:
        t = self.conn.execute(
            "SELECT * FROM repair_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        if t is None:
            raise DomainError("维修单不存在")
        return t

    def _require_role(self, login: str, role: str) -> None:
        u = self.conn.execute(
            "SELECT role FROM users WHERE login=?", (login,)).fetchone()
        if u is None:
            raise DomainError(f"用户 {login} 不存在")
        if u["role"] != role:
            need = {"inspector": "巡查员", "repair": "维修调度",
                    "supervisor": "主管"}[role]
            raise DomainError(f"该操作需要{need}身份（{login} 是 {u['role']}）")

    def _current_contract(self, box_id: str, at: datetime) -> Optional[sqlite3.Row]:
        ts = to_iso(at)
        return self.conn.execute(
            "SELECT * FROM contracts WHERE box_id=? AND valid_from<=? "
            "AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC "
            "LIMIT 1",
            (box_id, ts, ts),
        ).fetchone()

    def _event(self, ticket_id: int, actor: str, action: str,
               detail: str) -> None:
        self.conn.execute(
            "INSERT INTO ticket_events(ticket_id, ts, actor, action, detail)"
            " VALUES (?,?,?,?,?)",
            (ticket_id, to_iso(self.clock.now()), actor, action, detail),
        )
