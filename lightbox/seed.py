"""预置"半亮"样例数据。

当前时刻固定为 2026-10-01 20:00（计划亮灯时段内），包含 8 个点位:
  B001 健康全亮            B005 定时器错（计划内不亮）
  B002 半亮·故障进行中     B006 维修后再次熄灭（relapse）
  B003 供电中断（同回路）  B007 画面已撤换（非故障）
  B004 供电中断（归并）    B008 白天误判（非故障，历史巡检）
B002 的黑屏跨越合同换版，已提交一份待审批补播结论。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import VirtualClock, to_iso
from .db import connect, init_db
from .photos import render_photo
from .signal_ import SimulatedSignalAdapter
from . import makeup

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 1, 20, 0, tzinfo=TZ)


def oct(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def sep(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


BOXES = [
    # id, 名称, 地址, lat, lng, 回路, 灯带, 画面色, 模式
    ("B001", "南京西路·茂名北路", "南京西路 800 号", 31.2312, 121.4680,
     "C1", 8, "#E8513A", "auto"),
    ("B002", "静安寺地铁站 2 号口", "南京西路 1600 号", 31.2235, 121.4460,
     "C1", 8, "#2E9E6B", "half"),
    ("B003", "曹家渡公交站", "长寿路 900 号", 31.2396, 121.4355,
     "C2", 8, "#3563D6", "auto"),
    ("B004", "武宁南路路口", "武宁南路 200 号", 31.2370, 121.4301,
     "C2", 8, "#F2A93B", "auto"),
    ("B005", "延平路菜场口", "延平路 135 号", 31.2345, 121.4422,
     "C1", 6, "#7A4FBF", "off"),
    ("B006", "武定路夜市口", "武定路 555 号", 31.2388, 121.4510,
     "C1", 8, "#189AA8", "off"),
    ("B007", "陕西北路老字号街", "陕西北路 600 号", 31.2288, 121.4558,
     "C3", 8, "#D65B93", "auto"),
    ("B008", "常熟路地铁口", "常熟路 100 号", 31.2260, 121.4530,
     "C3", 8, "#8A9099", "auto"),
]


def seed(conn: sqlite3.Connection, photo_dir: Path, *,
         reset: bool = False) -> None:
    flag = conn.execute(
        "SELECT v FROM meta WHERE k='seeded'").fetchone()
    if flag and not reset:
        return
    if reset:
        conn.executescript(
            "DELETE FROM ticket_events; DELETE FROM makeup_conclusions;"
            "DELETE FROM blackout_segments; DELETE FROM inspections;"
            "DELETE FROM repair_tickets; DELETE FROM fault_boxes;"
            "DELETE FROM faults; DELETE FROM signal_readings;"
            "DELETE FROM contracts; DELETE FROM schedules; DELETE FROM boxes;"
            "DELETE FROM circuits; DELETE FROM users; DELETE FROM meta;"
            "DELETE FROM sqlite_sequence;")
        if photo_dir.exists():
            shutil.rmtree(photo_dir)
    photo_dir.mkdir(parents=True, exist_ok=True)

    # ---- 人员 ----
    users = [
        ("li", "李巡", "inspector"),
        ("zhang", "张巡", "inspector"),
        ("wang", "王维修", "repair"),
        ("zhao", "赵主管", "supervisor"),
    ]
    conn.executemany(
        "INSERT INTO users(login,name,role) VALUES(?,?,?)", users)

    # ---- 回路（C2 中断）----
    conn.executemany(
        "INSERT INTO circuits(id,name,status) VALUES(?,?,?)",
        [("C1", "陕西北 I 段回路", "normal"),
         ("C2", "曹家渡 II 段回路", "interrupted"),
         ("C3", "常熟路 III 段回路", "normal")])

    for (bid, name, addr, lat, lng, cir, strips, hexc, mode) in BOXES:
        conn.execute(
            "INSERT INTO boxes(id,name,addr,lat,lng,circuit_id,strips_total,"
            "creative_hex,sim_mode) VALUES(?,?,?,?,?,?,?,?,?)",
            (bid, name, addr, lat, lng, cir, strips, hexc, mode))
        conn.execute(
            "INSERT INTO schedules(box_id,days,start_hm,end_hm,valid_from)"
            " VALUES(?,'1,2,3,4,5,6,7','18:00','23:00','2026-01-01')",
            (bid,))

    # ---- 合同（B002 跨版本换约；其余单版本）----
    contracts = [
        ("B001", "红罐可乐", "2026-A", "2026-01-01T00:00:00+08:00", None, 1.0),
        ("B002", "青柠汽水", "2026-V1", "2026-01-01T00:00:00+08:00",
         "2026-09-30T00:00:00+08:00", 1.0),
        ("B002", "青柠汽水·秋季版", "2026-V2",
         "2026-09-30T00:00:00+08:00", None, 1.2),
        ("B003", "深蓝车险", "2026-A", "2026-01-01T00:00:00+08:00", None, 1.0),
        ("B004", "橙子英语", "2026-B", "2026-01-01T00:00:00+08:00", None, 1.0),
        ("B005", "紫云剧场", "2026-A", "2026-01-01T00:00:00+08:00", None, 1.0),
        ("B006", "青鹭瑜伽", "2026-A", "2026-01-01T00:00:00+08:00", None, 1.1),
        ("B007", "蔷薇美妆（已到期）", "2025-D",
         "2026-01-01T00:00:00+08:00", "2026-09-25T00:00:00+08:00", 1.0),
        ("B008", "灰调银行", "2026-A", "2026-01-01T00:00:00+08:00", None, 1.0),
    ]
    conn.executemany(
        "INSERT INTO contracts(box_id,advertiser,version,valid_from,valid_to,"
        "comp_factor) VALUES(?,?,?,?,?,?)", contracts)

    def add_insp(bid, ts, who, within, frac, on, match, cls, note,
                 label):
        p = photo_dir / f"{bid}_{to_iso(ts).replace(':','').replace('-','')}.png"
        render_photo(p, next(b[7] for b in BOXES if b[0] == bid),
                     frac, on, next(b[6] for b in BOXES if b[0] == bid),
                     within, match, label)
        cur = conn.execute(
            "INSERT INTO inspections(box_id,ts,inspector,within_schedule,"
            "fraction,strips_on,creative_match,classification,photo_path,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bid, to_iso(ts), who, int(within), frac, on, int(match),
             cls, str(p), note))
        return cur.lastrowid

    def add_reading(bid, ts, frac, src):
        conn.execute(
            "INSERT INTO signal_readings(box_id,ts,fraction,source)"
            " VALUES(?,?,?,?)", (bid, to_iso(ts), frac, src))

    # ---------- B001 健康 ----------
    add_insp("B001", oct(1, 19, 30), "zhang", True, 1.0, 8, True,
             "healthy", "计划时段全亮，画面与合同一致", "RED COLA")
    add_reading("B001", oct(1, 19, 30), 1.0, "inspect")

    # ---------- B002 半亮，故障#1，黑屏跨越换版 ----------
    f1 = conn.execute(
        "INSERT INTO faults(box_id,circuit_id,kind,status,first_seen_at,"
        "last_seen_at,blackout_start) VALUES('B002',NULL,'partial','open',"
        "?,?,?)",
        (to_iso(sep(29, 19, 0)), to_iso(oct(1, 19, 0)),
         to_iso(sep(29, 19, 0)))).lastrowid
    conn.execute("INSERT INTO fault_boxes(fault_id,box_id) VALUES(?,'B002')",
                 (f1,))
    # 半亮段持续中（09-29 19:00 起），跨越 09-30 换版
    conn.execute(
        "INSERT INTO blackout_segments(fault_id,box_id,start_ts,end_ts,deficit)"
        " VALUES(?,'B002',?,?,0.5)",
        (f1, to_iso(sep(29, 19, 0)), to_iso(sep(29, 19, 0))))
    i1 = add_insp("B002", sep(29, 19, 0), "li", True, 0.5, 4, True,
                  "partial", "8 条灯带仅 4 条亮，半亮", "LIME SODA")
    conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f1, i1))
    add_reading("B002", sep(29, 19, 0), 0.5, "inspect")
    i2 = add_insp("B002", oct(1, 19, 0), "li", True, 0.5, 4, True,
                  "partial", "换季后仍半亮，故障未修复", "LIME SODA V2")
    conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f1, i2))
    add_reading("B002", oct(1, 19, 0), 0.5, "inspect")
    # 已派维修单
    t1 = conn.execute(
        "INSERT INTO repair_tickets(fault_id,box_id,status,created_at,"
        "assignee,expected_restore) VALUES(?, 'B002','dispatched',?,"
        "'wang',?)",
        (f1, to_iso(sep(29, 20, 0)),
         to_iso(oct(2, 12, 0)))).lastrowid
    conn.execute(
        "INSERT INTO ticket_events(ticket_id,ts,actor,action,detail)"
        " VALUES(?,?, 'wang','dispatch','半亮故障派单，预计 10-02 中午恢复')",
        (t1, to_iso(sep(29, 20, 0)),))

    # ---------- B003/B004 回路中断，归并为故障#2 ----------
    f2 = conn.execute(
        "INSERT INTO faults(box_id,circuit_id,kind,status,first_seen_at,"
        "last_seen_at,expected_restore,blackout_start) VALUES("
        "'B003','C2','outage','open',?,?,?,?)",
        (to_iso(oct(1, 18, 30)), to_iso(oct(1, 19, 10)),
         to_iso(oct(1, 22, 0)), to_iso(oct(1, 18, 30)))).lastrowid
    conn.executemany(
        "INSERT INTO fault_boxes(fault_id,box_id) VALUES(?,?)",
        [(f2, "B003"), (f2, "B004")])
    for bid, ts, who in [("B003", oct(1, 18, 30), "li"),
                         ("B004", oct(1, 18, 35), "zhang")]:
        conn.execute(
            "INSERT INTO blackout_segments(fault_id,box_id,start_ts,end_ts,"
            "deficit) VALUES(?, ?, ?, ?,1.0)",
            (f2, bid, to_iso(ts), to_iso(ts)))
        ii = add_insp(bid, ts, who, True, 0.0, 0, True, "outage",
                      "曹家渡回路 C2 供电中断，整箱全黑",
                      "BLUE" if bid == "B003" else "ORANGE")
        conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f2, ii))
        add_reading(bid, ts, 0.0, "inspect")
    # 只为 B003 派单；B004 在界面上可继续派单（回路故障已归并）
    t2 = conn.execute(
        "INSERT INTO repair_tickets(fault_id,box_id,status,created_at,"
        "assignee,expected_restore) VALUES(?, 'B003','dispatched',?,'wang',?)",
        (f2, to_iso(oct(1, 18, 40)), to_iso(oct(1, 22, 0)))).lastrowid
    conn.execute(
        "INSERT INTO ticket_events(ticket_id,ts,actor,action,detail)"
        " VALUES(?,?, 'wang','dispatch','回路中断抢修，预计 22:00 恢复')",
        (t2, to_iso(oct(1, 18, 40))))

    # ---------- B005 定时器错（计划内不亮），故障#3 ----------
    f3 = conn.execute(
        "INSERT INTO faults(box_id,circuit_id,kind,status,first_seen_at,"
        "last_seen_at,blackout_start) VALUES('B005',NULL,'timer','open',"
        "?,?,?)",
        (to_iso(oct(1, 18, 5)), to_iso(oct(1, 19, 5)),
         to_iso(oct(1, 18, 5)))).lastrowid
    conn.execute("INSERT INTO fault_boxes(fault_id,box_id) VALUES(?,'B005')",
                 (f3,))
    conn.execute(
        "INSERT INTO blackout_segments(fault_id,box_id,start_ts,end_ts,"
        "deficit) VALUES(?,'B005',?,?,1.0)",
        (f3, to_iso(oct(1, 18, 5)), to_iso(oct(1, 18, 5))))
    i5 = add_insp("B005", oct(1, 18, 5), "zhang", True, 0.0, 0, True,
                  "timer", "计划时段定时器未合闸，疑似定时配置错", "PURPLE")
    conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f3, i5))
    add_reading("B005", oct(1, 18, 5), 0.0, "inspect")

    # ---------- B006 维修后再次熄灭（relapse），故障#4 ----------
    f4 = conn.execute(
        "INSERT INTO faults(box_id,circuit_id,kind,status,first_seen_at,"
        "last_seen_at,resolved_at,blackout_start) VALUES("
        "'B006',NULL,'relapse','open',?,?,?,?)",
        (to_iso(sep(28, 18, 30)), to_iso(sep(30, 19, 30)),
         to_iso(sep(29, 21, 0)), to_iso(sep(30, 19, 30)))).lastrowid
    conn.execute("INSERT INTO fault_boxes(fault_id,box_id) VALUES(?,'B006')",
                 (f4,))
    # 第一轮黑屏（已修复）
    conn.execute(
        "INSERT INTO blackout_segments(fault_id,box_id,start_ts,end_ts,deficit)"
        " VALUES(?,'B006',?,?,1.0)",
        (f4, to_iso(sep(28, 18, 30)), to_iso(sep(29, 21, 0))))
    # 修复后再次熄灭，第二轮进行中
    conn.execute(
        "INSERT INTO blackout_segments(fault_id,box_id,start_ts,end_ts,deficit)"
        " VALUES(?,'B006',?,?,1.0)",
        (f4, to_iso(sep(30, 19, 30)), to_iso(sep(30, 19, 30))))
    i6a = add_insp("B006", sep(28, 18, 30), "li", True, 0.0, 0, True,
                   "outage", "首报全黑", "YOGA")
    conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f4, i6a))
    i6b = add_insp("B006", sep(30, 19, 30), "zhang", True, 0.0, 0, True,
                   "relapse", "9-29 修复后再次熄灭，重开原维修单", "YOGA")
    conn.execute("UPDATE inspections SET fault_id=? WHERE id=?", (f4, i6b))
    add_reading("B006", sep(30, 19, 30), 0.0, "inspect")
    t4 = conn.execute(
        "INSERT INTO repair_tickets(fault_id,box_id,status,created_at,"
        "assignee,repaired_at,repair_by) VALUES("
        "?,'B006','reopened',?,'wang',?,'wang')",
        (f4, to_iso(sep(28, 19, 0)), to_iso(sep(29, 21, 0)))).lastrowid
    conn.executemany(
        "INSERT INTO ticket_events(ticket_id,ts,actor,action,detail)"
        " VALUES(?,?,?,?,?)",
        [(t4, to_iso(sep(28, 19, 0)), "wang", "dispatch", "首报派单"),
         (t4, to_iso(sep(29, 21, 0)), "wang", "repair", "更换电源，填报修复"),
         (t4, to_iso(sep(30, 19, 30)), "zhang", "verify_fail",
          "复验仍全黑，维修单重开（relapse）")])

    # ---------- B007 画面已撤换（非故障）----------
    add_insp("B007", oct(1, 19, 10), "li", True, 1.0, 8, False,
             "creative", "灯亮正常，但现场画面已撤换，登记合同 9-25 到期",
             "REMOVED")
    add_reading("B007", oct(1, 19, 10), 1.0, "inspect")

    # ---------- B008 白天误判（历史，12:00 巡检）----------
    add_insp("B008", oct(1, 12, 0), "zhang", False, 0.0, 0, True,
             "daytime", "白天非亮灯时段，熄灭符合计划，非故障", "BANK")
    add_reading("B008", oct(1, 12, 0), 0.0, "inspect")

    conn.commit()

    # 当前时刻轮询，产生"最近亮灯证据"
    clock = VirtualClock(NOW)
    SimulatedSignalAdapter(conn, clock).poll_all()
    conn.commit()

    # B002 补播结论：李巡提交，待审批（不能自批）
    cid = makeup.propose(conn, clock, f1, "li")
    conn.execute("INSERT INTO meta(k,v) VALUES('seeded','1')")
    conn.commit()
    return cid


def build(db_path: Path, photo_dir: Path, reset: bool = False):
    conn = connect(db_path)
    init_db(conn)
    seed(conn, photo_dir, reset=reset)
    return conn


if __name__ == "__main__":  # pragma: no cover
    import sys

    root = Path(__file__).resolve().parent.parent
    reset = "--reset" in sys.argv
    c = build(root / "data" / "lightbox.db", root / "data" / "photos", reset)
    print("seeded:", c.execute("SELECT COUNT(*) n FROM boxes").fetchone()["n"],
          "boxes; reset =", reset)
