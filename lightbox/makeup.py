"""补播核算。

规则:
1. 只统计黑屏区间与"计划亮灯时段"的重叠（白天熄灭不补）。
2. 半亮按缺光比例折算（deficit=0.5）。
3. 黑屏跨越合同换版时，按当时生效的合同版本分别归属广告主，
   补播时长 = 重叠分钟 × deficit；补偿积分再乘该版本 comp_factor。
4. 巡查员可以提交补播结论，但提交人不能批准自己的结论；
   批准/驳回须由其他巡查员或主管完成（职责分离）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .clock import Clock, from_iso, to_iso
from .schedule import overlap_minutes


class MakeupError(Exception):
    pass


@dataclass
class VersionShare:
    contract_id: int
    advertiser: str
    version: str
    planned_minutes: float          # 该版本期内计划亮灯分钟
    blackout_minutes: float         # 实际黑屏（未折算）
    weighted_minutes: float         # ×deficit 后应补播分钟
    comp_factor: float
    credit: float                   # 补播积分 = weighted × factor


def _contract_versions(conn: sqlite3.Connection, box_id: str,
                       lo: datetime, hi: datetime):
    """返回与 [lo, hi) 相交的合同版本及裁剪后的区间。"""
    rows = conn.execute(
        "SELECT * FROM contracts WHERE box_id=? ORDER BY valid_from",
        (box_id,),
    ).fetchall()
    out = []
    for r in rows:
        vf = from_iso(r["valid_from"])
        vt = from_iso(r["valid_to"]) if r["valid_to"] else None
        c_lo = max(lo, vf)
        c_hi = min(hi, vt) if vt else hi
        if c_lo < c_hi:
            out.append((r, c_lo, c_hi))
    return out


def compute_for_box(conn: sqlite3.Connection, clock: Clock,
                    fault_id: int, box_id: str,
                    at: Optional[datetime] = None) -> list[VersionShare]:
    now = at or clock.now()
    segs = conn.execute(
        "SELECT * FROM blackout_segments WHERE fault_id=? AND box_id=? "
        "ORDER BY start_ts",
        (fault_id, box_id),
    ).fetchall()

    buckets: dict[int, VersionShare] = {}
    for s in segs:
        start = from_iso(s["start_ts"])
        end = from_iso(s["end_ts"])
        if end <= start:                 # 仍在黑屏
            end = now
        if end <= start:
            continue
        deficit = s["deficit"]
        for c, lo, hi in _contract_versions(conn, box_id, start, end):
            planned = overlap_minutes(conn, box_id, lo, hi)
            black = overlap_minutes(conn, box_id, max(lo, start),
                                    min(hi, end))
            weighted = round(black * deficit, 2)
            key = c["id"]
            b = buckets.get(key)
            if b is None:
                b = VersionShare(c["id"], c["advertiser"], c["version"],
                                 0.0, 0.0, 0.0, c["comp_factor"], 0.0)
                buckets[key] = b
            b.planned_minutes += round(planned, 2)
            b.blackout_minutes += round(black, 2)
            b.weighted_minutes += weighted
            b.credit = round(b.weighted_minutes * b.comp_factor, 2)
    return sorted(buckets.values(), key=lambda v: v.contract_id)


def compute_fault(conn: sqlite3.Connection, clock: Clock, fault_id: int,
                  at: Optional[datetime] = None) -> dict:
    fault = conn.execute("SELECT * FROM faults WHERE id=?",
                         (fault_id,)).fetchone()
    if fault is None:
        raise MakeupError("故障不存在")
    boxes = [r["box_id"] for r in conn.execute(
        "SELECT box_id FROM fault_boxes WHERE fault_id=? ORDER BY box_id",
        (fault_id,))]
    per_box = {}
    totals = {"planned": 0.0, "blackout": 0.0, "weighted": 0.0, "credit": 0.0}
    for b in boxes:
        shares = compute_for_box(conn, clock, fault_id, b, at)
        per_box[b] = shares
        for v in shares:
            totals["planned"] += v.planned_minutes
            totals["blackout"] += v.blackout_minutes
            totals["weighted"] += v.weighted_minutes
            totals["credit"] += v.credit
    ongoing = conn.execute(
        "SELECT COUNT(*) n FROM blackout_segments WHERE fault_id=? "
        "AND end_ts=start_ts", (fault_id,)).fetchone()["n"] > 0
    return {"fault": dict(fault), "boxes": per_box, "totals": totals,
            "ongoing": ongoing}


# ---------------- 结论与审批 ----------------

def propose(conn: sqlite3.Connection, clock: Clock, fault_id: int,
            inspector: str) -> int:
    u = conn.execute("SELECT role FROM users WHERE login=?",
                     (inspector,)).fetchone()
    if u is None or u["role"] != "inspector":
        raise MakeupError("只有巡查员能提交补播结论")
    fault = conn.execute("SELECT * FROM faults WHERE id=?",
                         (fault_id,)).fetchone()
    if fault is None:
        raise MakeupError("故障不存在")
    existing = conn.execute(
        "SELECT id FROM makeup_conclusions WHERE fault_id=? AND status='pending'",
        (fault_id,)).fetchone()
    if existing:
        raise MakeupError("该故障已有待审批的补播结论")
    data = compute_fault(conn, clock, fault_id)
    breakdown = []
    for box_id, shares in data["boxes"].items():
        for v in shares:
            breakdown.append({
                "box_id": box_id, "contract_id": v.contract_id,
                "advertiser": v.advertiser, "version": v.version,
                "planned_minutes": v.planned_minutes,
                "blackout_minutes": v.blackout_minutes,
                "weighted_minutes": v.weighted_minutes,
                "comp_factor": v.comp_factor, "credit": v.credit,
            })
    cur = conn.execute(
        "INSERT INTO makeup_conclusions(fault_id, proposed_by, proposed_at,"
        " status, breakdown) VALUES (?,?,?, 'pending', ?)",
        (fault_id, inspector, to_iso(clock.now()), json.dumps(breakdown))
    )
    conn.commit()
    return cur.lastrowid


def decide(conn: sqlite3.Connection, clock: Clock, conclusion_id: int,
           actor: str, approve: bool, note: str = "") -> None:
    u = conn.execute("SELECT role FROM users WHERE login=?",
                     (actor,)).fetchone()
    if u is None or u["role"] not in ("supervisor", "inspector"):
        raise MakeupError("审批需由巡查员或主管完成")
    row = conn.execute(
        "SELECT * FROM makeup_conclusions WHERE id=?",
        (conclusion_id,)).fetchone()
    if row is None:
        raise MakeupError("补播结论不存在")
    if row["status"] != "pending":
        raise MakeupError("该结论已审批")
    if row["proposed_by"] == actor:
        raise MakeupError("巡查员不能批准自己的补播结论；且审批人不得为提交人")
    conn.execute(
        "UPDATE makeup_conclusions SET status=?, decided_by=?, decided_at=?,"
        " decision_note=? WHERE id=?",
        ("approved" if approve else "rejected", actor,
         to_iso(clock.now()), note, conclusion_id))
    conn.commit()
