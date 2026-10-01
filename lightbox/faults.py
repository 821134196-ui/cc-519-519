"""核心领域服务：巡检判障、故障归并、维修派单、复验、补播核算。

所有时间以 epoch *分钟* 传入，函数本身不读系统时钟，便于测试回放。
"""
from __future__ import annotations

import sqlite3

from . import clock
from .times import is_scheduled_lit, plan_segments

# 视为“未关闭”的故障状态
OPEN_STATUSES = ("open", "repaired", "rejected")
BRIGHTNESS_FULL = 0.99


class DomainError(Exception):
    """业务规则冲突（如重复派单、不可复验等）。"""


class PermissionDenied(DomainError):
    """复验/审批权限不足：巡查员不能复验或批准自己的结论。"""


# ---------------------------------------------------------------- 基础查询

def box_schedules(conn: sqlite3.Connection, lightbox_id: int):
    return conn.execute(
        "SELECT weekday, start_hm, end_hm FROM schedules WHERE lightbox_id=?",
        (lightbox_id,),
    ).fetchall()


def active_contract(conn: sqlite3.Connection, lightbox_id: int, ts_min: int):
    return conn.execute(
        """SELECT * FROM contracts
           WHERE lightbox_id=? AND start_min <= ? AND end_min >= ?""",
        (lightbox_id, ts_min, ts_min),
    ).fetchone()


def fault_reporter(conn: sqlite3.Connection, fault_id: int):
    """故障的最初上报巡查员（最早一张关联巡检照片的拍摄人）。"""
    return conn.execute(
        """SELECT inspector_id FROM inspections
           WHERE fault_id=? ORDER BY ts_min, id LIMIT 1""",
        (fault_id,),
    ).fetchone()


def _estimated_onset(conn, lightbox_id, ts_min, signal_set_min=None):
    """黑屏起：优先采用信号录制时刻，否则用最近一次亮灯证据时刻，否则取当下。"""
    if signal_set_min is not None:
        return max(0, min(signal_set_min, ts_min))
    row = conn.execute(
        """SELECT ts_min FROM inspections
           WHERE lightbox_id=? AND brightness >= ? AND ts_min < ?
           ORDER BY ts_min DESC LIMIT 1""",
        (lightbox_id, BRIGHTNESS_FULL, ts_min),
    ).fetchone()
    c = active_contract(conn, lightbox_id, ts_min)
    onset = row["ts_min"] if row else ts_min
    if c:
        onset = max(onset, c["start_min"])
    return onset


def _attach_advertisers(conn, fault_id, lightbox_id, start_min, end_min=None):
    """把某灯箱在故障窗口内有在档合同的广告主挂到故障单上。"""
    end = end_min if end_min is not None else 10**12
    rows = conn.execute(
        """SELECT DISTINCT advertiser_id FROM contracts
           WHERE lightbox_id=? AND start_min <= ? AND end_min >= ?""",
        (lightbox_id, end, start_min),
    ).fetchall()
    for r in rows:
        conn.execute(
            """INSERT OR IGNORE INTO fault_advertisers(fault_id, advertiser_id, lightbox_id)
               VALUES(?,?,?)""",
            (fault_id, r["advertiser_id"], lightbox_id),
        )


def _circuit_master(conn, circuit_id, ts_min):
    """该回路上未关闭的供电中断主单。"""
    return conn.execute(
        """SELECT * FROM faults
           WHERE kind='power_outage' AND circuit_id=? AND status IN ('open','repaired','rejected')
             AND start_min <= ?
           ORDER BY start_min LIMIT 1""",
        (circuit_id, ts_min),
    ).fetchone()


def _open_fault_on_box(conn, lightbox_id, kind):
    return conn.execute(
        """SELECT * FROM faults
           WHERE lightbox_id=? AND kind=? AND status IN ('open','repaired','rejected')
           ORDER BY start_min LIMIT 1""",
        (lightbox_id, kind),
    ).fetchone()


# ---------------------------------------------------------------- 巡检判障

def inspect(conn, *, lightbox_id: int, inspector_id: int, ts_min: int,
            brightness: float, lit_bars: int, total_bars: int,
            note: str = "", adapter=None, photo_store=None, signal_set_min=None):
    """登记一次巡检并判定/归并故障。返回 (inspection_row, fault_row_or_None, kind)。

    brightness/lit_bars/total_bars 由信号适配器给出（本函数不直接调适配器，
    以便用固定数据回放）；adapter 仅用于供电中断时探测同回路其他灯箱。
    """
    brightness = max(0.0, min(1.0, float(brightness)))
    lb = conn.execute("SELECT * FROM lightboxes WHERE id=?", (lightbox_id,)).fetchone()
    if lb is None:
        raise DomainError(f"灯箱不存在: {lightbox_id}")

    cur = conn.execute(
        """INSERT INTO inspections(lightbox_id, inspector_id, ts_min, brightness,
               lit_bars, total_bars, note)
           VALUES(?,?,?,?,?,?,?)""",
        (lightbox_id, inspector_id, ts_min, brightness, lit_bars, total_bars, note),
    )
    insp_id = cur.lastrowid
    schedules = box_schedules(conn, lightbox_id)
    scheduled = is_scheduled_lit(ts_min, schedules)

    fault = None
    kind = None

    if not scheduled:
        # 计划时段外的“不亮”——白天误判，不立故障
        kind = "daytime_false_positive"
    elif brightness >= BRIGHTNESS_FULL:
        kind = "normal"
        # 计划时段内重新满亮且在档合同恢复 → 自动关闭此前的“画面已撤换”单
        conn.execute(
            """UPDATE faults SET status='verified_closed', end_min=?, closed_min=?,
                   cause_note=cause_note || '；新画面已上、亮灯恢复，自动关闭'
               WHERE lightbox_id=? AND kind='artwork_removed'
                 AND status IN ('open','repaired','rejected')""",
            (ts_min, ts_min, lightbox_id),
        )
    else:
        contract = active_contract(conn, lightbox_id, ts_min)
        if contract is None:
            kind = "artwork_removed"
        elif brightness <= 0.0:
            kind = _classify_dark(conn, lightbox_id, ts_min, adapter)
        else:
            kind = "strip_partial"

        if kind in ("artwork_removed", "timer_misconfig", "strip_partial"):
            fault = _open_fault_on_box(conn, lightbox_id, kind)
            if fault is None:
                start = _estimated_onset(conn, lightbox_id, ts_min, signal_set_min)
                cur = conn.execute(
                    """INSERT INTO faults(kind, status, lightbox_id, circuit_id,
                           start_min, detected_min, cause_note)
                       VALUES(?, 'open', ?, NULL, ?, ?, ?)""",
                    (kind, lightbox_id, start, ts_min, note),
                )
                fault = conn.execute("SELECT * FROM faults WHERE id=?", (cur.lastrowid,)).fetchone()
        elif kind == "power_outage":
            fault = _open_or_merge_power(conn, lb, ts_min, note, adapter, signal_set_min)

        if fault is not None:
            if kind != "artwork_removed":
                _attach_advertisers(conn, fault["id"], lightbox_id, fault["start_min"], ts_min)
            conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (fault["id"], insp_id))

    # 本地照片（失败不影响判障）
    photo_path = None
    if photo_store is not None:
        ts_label = clock.fmt(ts_min)
        photo_path = photo_store.save(
            code=lb["code"], inspection_id=insp_id, ts_label=ts_label,
            brightness=brightness, lit_bars=lit_bars, total_bars=total_bars,
        )
        conn.execute("UPDATE inspections SET photo_path=? WHERE id=?", (photo_path, insp_id))

    conn.commit()
    insp = conn.execute("SELECT * FROM inspections WHERE id=?", (insp_id,)).fetchone()
    return insp, fault, kind


def _classify_dark(conn, lightbox_id, ts_min, adapter):
    """全黑：同回路其他在计划灯箱也黑 -> 供电中断；否则定时器配置错。"""
    lb = conn.execute("SELECT * FROM lightboxes WHERE id=?", (lightbox_id,)).fetchone()
    if lb["circuit_id"] is None or adapter is None:
        return "timer_misconfig"
    peers = conn.execute(
        "SELECT id FROM lightboxes WHERE circuit_id=? AND id != ?",
        (lb["circuit_id"], lightbox_id),
    ).fetchall()
    for p in peers:
        sig = adapter.read(p["id"], at_min=ts_min)
        if sig.brightness <= 0.0 and is_scheduled_lit(ts_min, box_schedules(conn, p["id"])):
            return "power_outage"
    return "timer_misconfig"


def _open_or_merge_power(conn, lb, ts_min, note, adapter, signal_set_min):
    """供电中断：回路主单存在则归并；否则以本箱建回路主单并收拢同路黑箱。"""
    master = _circuit_master(conn, lb["circuit_id"], ts_min) if lb["circuit_id"] is not None else None
    if master is not None:
        return master

    start = _estimated_onset(conn, lb["id"], ts_min, signal_set_min)
    cur = conn.execute(
        """INSERT INTO faults(kind, status, lightbox_id, circuit_id,
               start_min, detected_min, cause_note)
           VALUES('power_outage','open',?,?,?,?,'同回路多灯箱齐黑，按供电中断归并')""",
        (lb["id"], lb["circuit_id"], start, ts_min),
    )
    master = conn.execute("SELECT * FROM faults WHERE id=?", (cur.lastrowid,)).fetchone()

    # 把同回路其他在计划内齐黑的灯箱一并挂入主单
    if adapter is not None and lb["circuit_id"] is not None:
        for p in conn.execute(
            "SELECT id FROM lightboxes WHERE circuit_id=? AND id != ?",
            (lb["circuit_id"], lb["id"]),
        ).fetchall():
            sig = adapter.read(p["id"], at_min=ts_min)
            if sig.brightness <= 0.0 and is_scheduled_lit(ts_min, box_schedules(conn, p["id"])):
                _attach_advertisers(conn, master["id"], p["id"], start, ts_min)
    return master


# ---------------------------------------------------------------- 维修派单

def dispatch_work_order(conn, *, fault_id: int, assignee: str,
                        created_min: int, eta_min: int | None = None,
                        note: str = ""):
    fault = conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
    if fault is None:
        raise DomainError("故障单不存在")
    if fault["kind"] == "artwork_removed":
        raise DomainError("画面已撤换属于合同空档，不是亮灯故障，无需派修")
    if fault["status"] == "verified_closed":
        raise DomainError("故障已复验关闭，不能再派单")
    dup = conn.execute(
        "SELECT id FROM work_orders WHERE fault_id=? AND status='open'",
        (fault_id,),
    ).fetchone()
    if dup is not None:
        raise DomainError("同一故障存在未关闭维修单，不得平行创建第二张维修单")

    cur = conn.execute(
        """INSERT INTO work_orders(fault_id, assignee, created_min, eta_min, repair_note)
           VALUES(?,?,?,?,?)""",
        (fault_id, assignee, created_min, eta_min, note),
    )
    conn.commit()
    return conn.execute("SELECT * FROM work_orders WHERE id=?", (cur.lastrowid,)).fetchone()


def complete_repair(conn, *, work_order_id: int, done_min: int, note: str = "",
                    adapter=None):
    wo = conn.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()
    if wo is None:
        raise DomainError("维修单不存在")
    if wo["status"] != "open":
        raise DomainError("维修单已完结")
    conn.execute(
        "UPDATE work_orders SET status='done', done_min=?, repair_note=? WHERE id=?",
        (done_min, note, work_order_id),
    )
    conn.execute(
        "UPDATE faults SET status='repaired' WHERE id=? AND status IN ('open','rejected')",
        (wo["fault_id"],),
    )
    # 模拟环境：维修完成即撤掉故障信号，恢复计划亮灯
    if adapter is not None:
        fault = conn.execute("SELECT * FROM faults WHERE id=?", (wo["fault_id"],)).fetchone()
        adapter.clear_override(fault["lightbox_id"])
    conn.commit()
    return conn.execute("SELECT * FROM work_orders WHERE id=?", (work_order_id,)).fetchone()


# ---------------------------------------------------------------- 复验

def reverify(conn, *, fault_id: int, inspector_id: int, ts_min: int,
             adapter, note: str = ""):
    """复验关单。复验人不得是本单最初上报人；不通过则开出 re_outage 衍生单。"""
    fault = conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
    if fault is None:
        raise DomainError("故障单不存在")
    if fault["status"] != "repaired":
        raise DomainError("仅“维修完成待复验”的故障可以复验")
    reporter = fault_reporter(conn, fault_id)
    if reporter is not None and reporter["inspector_id"] == inspector_id:
        raise PermissionDenied("巡查员不能复验自己上报的故障，请交由其他巡查员复验")

    sig = adapter.read(fault["lightbox_id"], at_min=ts_min)
    scheduled = is_scheduled_lit(ts_min, box_schedules(conn, fault["lightbox_id"]))
    if not scheduled:
        raise DomainError("复验须在亮灯计划时段内进行（计划外无法确认亮灯恢复）")
    passed = int(sig.brightness >= BRIGHTNESS_FULL)

    conn.execute(
        """INSERT INTO reverifications(fault_id, inspector_id, ts_min, passed,
               brightness, note)
           VALUES(?,?,?,?,?,?)""",
        (fault_id, inspector_id, ts_min, passed, sig.brightness, note),
    )

    if passed:
        conn.execute(
            """UPDATE faults SET status='verified_closed', end_min=?, closed_min=?
               WHERE id=?""",
            (ts_min, ts_min, fault_id),
        )
        conn.execute(
            "UPDATE work_orders SET status='closed' WHERE fault_id=? AND status='done'",
            (fault_id,),
        )
        rows = _propose_makeup(conn, fault_id, ts_min)
        conn.commit()
        return {"passed": True, "fault_id": fault_id, "credits": rows}

    # 复验不通过 → 记为“维修后再次熄灭”：退回维修，可重新派单（旧单已 done，不违反唯一性）
    conn.execute("UPDATE faults SET status='rejected' WHERE id=?", (fault_id,))
    conn.execute(
        """INSERT INTO inspections(lightbox_id, inspector_id, ts_min, brightness,
               lit_bars, total_bars, note, fault_id)
           VALUES(?,?,?,?,?,?, '复验不通过-维修后再次熄灭', ?)""",
        (fault["lightbox_id"], inspector_id, ts_min, sig.brightness,
         sig.lit_bars, sig.total_bars, fault_id),
    )
    conn.commit()
    return {"passed": False, "fault_id": fault_id}


# ---------------------------------------------------------------- 补播核算

def _partial_deficit(conn, fault_id: int) -> float:
    """部分灯带不亮的缺额比例：1 - 关联巡检的平均（亮带/总带）。"""
    rows = conn.execute(
        "SELECT lit_bars, total_bars FROM inspections WHERE fault_id=? AND total_bars>0",
        (fault_id,),
    ).fetchall()
    if not rows:
        return 1.0
    ratios = [r["lit_bars"] / r["total_bars"] for r in rows]
    return max(0.0, 1.0 - sum(ratios) / len(ratios))


def compute_makeup(conn, fault_id: int, end_min: int | None = None):
    """按合同版本切片计算补播。返回明细字典列表（不写库）。

    - 黑屏只计计划亮灯时段（times.plan_segments 交集）；
    - 窗口按故障期内有效的合同版本切片；
    - strip_partial 按缺额比例折算分钟；artwork_removed 不产生补播。
    """
    fault = conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
    if fault is None:
        raise DomainError("故障单不存在")
    end = end_min if end_min is not None else fault["end_min"]
    if end is None or fault["kind"] == "artwork_removed":
        return []

    deficit = _partial_deficit(conn, fault_id) if fault["kind"] == "strip_partial" else 1.0
    affected = conn.execute(
        "SELECT DISTINCT advertiser_id, lightbox_id FROM fault_advertisers WHERE fault_id=?",
        (fault_id,),
    ).fetchall()
    results = []
    for a in affected:
        contracts = conn.execute(
            """SELECT * FROM contracts
               WHERE lightbox_id=? AND advertiser_id=? AND start_min <= ? AND end_min >= ?
               ORDER BY start_min""",
            (a["lightbox_id"], a["advertiser_id"], end, fault["start_min"]),
        ).fetchall()
        for c in contracts:
            # 合同端点均为含端点：切片转半开分钟区间 [start, end+1)
            seg_start = max(fault["start_min"], c["start_min"])
            seg_end = min(end, c["end_min"] + 1)
            if seg_end <= seg_start:
                continue
            segs = plan_segments(seg_start, seg_end, box_schedules(conn, a["lightbox_id"]))
            planned = sum(e - s for s, e in segs)
            owed = round(planned * deficit)
            if owed <= 0:
                continue
            results.append({
                "fault_id": fault_id,
                "contract_id": c["id"],
                "advertiser_id": a["advertiser_id"],
                "lightbox_id": a["lightbox_id"],
                "segment_start": seg_start,
                "segment_end": seg_end,
                "planned_minutes": planned,
                "deficit": round(deficit, 4),
                "owed_minutes": owed,
                "amount": round(owed * float(c["rate_per_min"]), 2),
                "artwork_name": c["artwork_name"],
            })
    return results


def _propose_makeup(conn, fault_id: int, end_min: int):
    reporter = fault_reporter(conn, fault_id)
    proposed_by = reporter["inspector_id"] if reporter else 1
    rows = compute_makeup(conn, fault_id, end_min)
    for r in rows:
        conn.execute(
            """INSERT INTO makeup_credits(fault_id, contract_id, advertiser_id, lightbox_id,
                   owed_minutes, amount, status, proposed_by)
               VALUES(?,?,?,?,?,?, 'proposed', ?)
               ON CONFLICT(fault_id, contract_id) DO UPDATE SET
                 owed_minutes=excluded.owed_minutes, amount=excluded.amount,
                 status='proposed', approved_by=NULL, approved_min=NULL""",
            (r["fault_id"], r["contract_id"], r["advertiser_id"], r["lightbox_id"],
             r["owed_minutes"], r["amount"], proposed_by),
        )
    return rows


def approve_makeup(conn, *, fault_id: int, inspector_id: int, ts_min: int,
                   approve: bool = True):
    """批准/驳回补播结论。不得批准自己提出的结论。"""
    rows = conn.execute(
        "SELECT * FROM makeup_credits WHERE fault_id=?", (fault_id,),
    ).fetchall()
    if not rows:
        raise DomainError("该故障没有待审批的补播核算")
    proposer = rows[0]["proposed_by"]
    if proposer == inspector_id:
        raise PermissionDenied("巡查员不能批准自己提出的补播结论")
    new_status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE makeup_credits SET status=?, approved_by=?, approved_min=? WHERE fault_id=?",
        (new_status, inspector_id, ts_min, fault_id),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM makeup_credits WHERE fault_id=?", (fault_id,),
    ).fetchall()


# ---------------------------------------------------------------- 故障详情对照

def blackout_breakdown(conn, fault_id: int):
    """故障详情：计划时段 vs 实际黑屏（只统计计划窗口内）。"""
    fault = conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
    if fault is None:
        raise DomainError("故障单不存在")
    end = fault["end_min"] or fault["detected_min"]
    segs = plan_segments(fault["start_min"], end, box_schedules(conn, fault["lightbox_id"]))
    planned = sum(e - s for s, e in segs)
    deficit = _partial_deficit(conn, fault_id) if fault["kind"] == "strip_partial" else 1.0
    return {
        "window_start": fault["start_min"],
        "window_end": end,
        "planned_segments": [{"start": s, "end": e, "minutes": e - s} for s, e in segs],
        "planned_minutes": planned,
        "effective_blackout_minutes": round(planned * deficit) if fault["kind"] != "artwork_removed" else 0,
        "deficit": round(deficit, 4),
    }
