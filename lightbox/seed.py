"""预置样例数据：以本周一 00:00（UTC+8）为锚点回放一条完整时间线。

包含要求的“半亮样例”（LB-A01 两条灯带熄灭）以及其余各类处置：
- LB-A02/A03 同回路齐黑 → 供电中断，归并为一张回路故障；
- LB-B01 单箱全黑（邻箱亮）→ 定时器配置错，已维修/复验/补播审批闭环；
- LB-B03 维修完成后复验时再次熄灭 → 复验不通过、退回维修；
- LB-C01 合同空档期巡检 → 画面已撤换，不派修、不计补播；
- 白天对 LB-A01 巡检 → 白天误判，不立故障。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import clock
from . import faults as svc
from .db import connect, init_db
from .photos import PhotoStore
from .signals import SimulatedSignalAdapter

DAY, HOUR, MIN = 1440, 60, 1


def monday_anchor() -> int:
    today = clock.dt_of(clock.now_min()).date()
    monday = today - dt.timedelta(days=today.weekday())
    return clock.parse_local(f"{monday.isoformat()}T00:00")


def seed(db_path: str | Path = "data/lightbox.db",
         photo_root: str | Path = "data/photos", *, reset: bool = True) -> None:
    db_path, photo_root = Path(db_path), Path(photo_root)
    if reset and db_path.exists():
        db_path.unlink()
    if reset and photo_root.exists():
        for p in sorted(photo_root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()

    conn = connect(db_path)
    init_db(conn)
    photos = PhotoStore(photo_root)
    adapter = SimulatedSignalAdapter(conn, clock=lambda: 0)  # 回放中显式传时刻

    b = monday_anchor()
    cur = conn.cursor()

    # --- 巡查员 / 广告主 ---------------------------------------------------
    insp = {}
    for name in ("张巡", "李巡", "王巡"):
        cur.execute("INSERT INTO inspectors(name) VALUES(?)", (name,))
        insp[name] = cur.lastrowid
    adv = {}
    for name, contact in (("星河咖啡", "周经理 138-0000-0101"),
                          ("云岚手机", "林经理 138-0000-0202"),
                          ("青禾教育", "陈老师 138-0000-0303")):
        cur.execute("INSERT INTO advertisers(name, contact) VALUES(?,?)", (name, contact))
        adv[name] = cur.lastrowid

    # --- 回路 / 灯箱 -------------------------------------------------------
    circuits = {}
    for name, note in (("C-1 中山路干线", "市政环网柜 03"),
                       ("C-2 滨河路干线", "景观照明箱 B2")):
        cur.execute("INSERT INTO circuits(name, note) VALUES(?,?)", (name, note))
        circuits[name.split()[0]] = cur.lastrowid

    boxes = [
        # code, 名称, 地址, x, y, circuit
        ("LB-A01", "中山路·便利店外墙", "中山路 12 号", 0.18, 0.30, "C-1"),
        ("LB-A02", "中山路·公交站亭",   "中山路 18 号", 0.27, 0.43, "C-1"),
        ("LB-A03", "中山路·书店门头",   "中山路 24 号", 0.36, 0.35, "C-1"),
        ("LB-B01", "滨河路·咖啡亭",     "滨河路 7 号",  0.63, 0.57, "C-2"),
        ("LB-B02", "滨河路·亲水平台",   "滨河路 9 号",  0.71, 0.67, "C-2"),
        ("LB-B03", "滨河路·廊桥下",     "滨河路 11 号", 0.60, 0.74, "C-2"),
        ("LB-C01", "老城广场·戏台前",   "老城广场 1 号", 0.47, 0.80, None),
    ]
    box_id = {}
    for code, name, addr, x, y, ckey in boxes:
        cur.execute(
            """INSERT INTO lightboxes(code, name, address, map_x, map_y, circuit_id, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (code, name, addr, x, y, circuits.get(ckey) if ckey else None, b - 30 * DAY),
        )
        box_id[code] = cur.lastrowid

    # --- 亮灯计划 ----------------------------------------------------------
    daily = [1, 2, 3, 4, 5, 6, 7]
    for code in ("LB-A01", "LB-A02", "LB-A03", "LB-B01", "LB-B03"):
        for wd in daily:
            cur.execute(
                "INSERT INTO schedules(lightbox_id, weekday, start_hm, end_hm) VALUES(?,?,?,?)",
                (box_id[code], wd, "18:00", "23:00"),
            )
    for wd in daily:  # 邻箱，时段略宽
        cur.execute(
            "INSERT INTO schedules(lightbox_id, weekday, start_hm, end_hm) VALUES(?,?,?,?)",
            (box_id["LB-B02"], wd, "17:30", "23:30"),
        )
    for wd in daily:  # 跨午夜计划
        cur.execute(
            "INSERT INTO schedules(lightbox_id, weekday, start_hm, end_hm) VALUES(?,?,?,?)",
            (box_id["LB-C01"], wd, "18:00", "02:00"),
        )

    def contract(code, advertiser, artwork, color, start, end, rate):
        cur.execute(
            """INSERT INTO contracts(lightbox_id, advertiser_id, artwork_name,
                   preview_color, start_min, end_min, rate_per_min)
               VALUES(?,?,?,?,?,?,?)""",
            (box_id[code], adv[advertiser], artwork, color, start, end, rate),
        )

    # --- 合同（含青禾教育的版本切片）--------------------------------------
    contract("LB-A01", "星河咖啡", "星河·秋日拿铁", "#b06a3a", b - 30 * DAY, b + 60 * DAY, 2.5)
    contract("LB-A02", "云岚手机", "云岚 X9 夜景模式", "#335e8c", b - 30 * DAY, b + 60 * DAY, 3.2)
    contract("LB-A03", "青禾教育", "青禾·秋季班（旧版）", "#3f7d5e", b - 30 * DAY, b + 2 * DAY + 23 * HOUR + 59 * MIN, 1.8)
    contract("LB-A03", "青禾教育", "青禾·冲刺班（新版）", "#c0483e", b + 3 * DAY, b + 60 * DAY, 2.2)
    contract("LB-B01", "云岚手机", "云岚 X9 夜景模式", "#335e8c", b - 30 * DAY, b + 60 * DAY, 3.2)
    contract("LB-B02", "星河咖啡", "星河·秋日拿铁", "#b06a3a", b - 30 * DAY, b + 60 * DAY, 2.5)
    contract("LB-B03", "星河咖啡", "星河·燕麦拿铁", "#8a6f4d", b - 30 * DAY, b + 60 * DAY, 2.5)
    # C01：旧画面周日 23:00 到期，新画面下周一才上——当前为撤换空档
    contract("LB-C01", "星河咖啡", "星河·夏日气泡（已撤）", "#7a9fc4", b - 30 * DAY, b - DAY + 23 * HOUR, 2.5)
    contract("LB-C01", "星河咖啡", "星河·冬日热橙（待上）", "#d1792f", b + 7 * DAY, b + 60 * DAY, 2.5)
    conn.commit()

    def do_inspect(code, who, ts, br, bars, total=4, note="", set_min=None):
        return svc.inspect(
            conn, lightbox_id=box_id[code], inspector_id=insp[who], ts_min=ts,
            brightness=br, lit_bars=bars, total_bars=total, note=note,
            adapter=adapter, photo_store=photos, signal_set_min=set_min,
        )

    # === 周一时间线 ===
    # LB-A01：19:00 正常 → 20:00 起半亮（预置“半亮样例”）
    do_inspect("LB-A01", "张巡", b + 19 * HOUR, 1.0, 4, note="亮灯正常")
    adapter.set_override(box_id["LB-A01"], 0.5, 2, b + 20 * HOUR, "两条灯带熄灭")
    _, f_partial, kind = do_inspect(
        "LB-A01", "张巡", b + 20 * HOUR + 5 * MIN, 0.5, 2,
        note="上数两条灯带不亮，画面半亮", set_min=b + 20 * HOUR)
    assert kind == "strip_partial", kind

    # LB-A02/A03：20:00 起同回路齐黑
    adapter.set_override(box_id["LB-A02"], 0.0, 0, b + 20 * HOUR, "回路无电")
    adapter.set_override(box_id["LB-A03"], 0.0, 0, b + 20 * HOUR, "回路无电")
    _, f_power, kind = do_inspect(
        "LB-A02", "张巡", b + 20 * HOUR + 10 * MIN, 0.0, 0,
        note="整箱黑屏，邻箱 LB-A03 同时熄灭", set_min=b + 20 * HOUR)
    assert kind == "power_outage", kind
    do_inspect("LB-A03", "张巡", b + 20 * HOUR + 15 * MIN, 0.0, 0,
               note="同回路黑屏，归入供电中断单")

    # LB-B01：定时器配置错 → 派修 → 复验通过 → 补播审批闭环
    adapter.set_override(box_id["LB-B01"], 0.0, 0, b + 19 * HOUR + 30 * MIN, "定时器无输出")
    _, f_timer, kind = do_inspect(
        "LB-B01", "李巡", b + 19 * HOUR + 40 * MIN, 0.0, 0,
        note="计划时段内不亮，邻箱 LB-B02 正常", set_min=b + 19 * HOUR + 30 * MIN)
    assert kind == "timer_misconfig", kind
    wo = svc.dispatch_work_order(conn, fault_id=f_timer["id"], assignee="赵师傅",
                                 created_min=b + 20 * HOUR, eta_min=b + 21 * HOUR,
                                 note="更换定时器控制模块")
    svc.complete_repair(conn, work_order_id=wo["id"], done_min=b + 21 * HOUR,
                        note="定时器已更换，恢复亮灯", adapter=adapter)
    res = svc.reverify(conn, fault_id=f_timer["id"], inspector_id=insp["王巡"],
                       ts_min=b + 21 * HOUR + 10 * MIN, adapter=adapter,
                       note="亮灯恢复正常")
    assert res["passed"] and res["credits"], res
    svc.approve_makeup(conn, fault_id=f_timer["id"], inspector_id=insp["张巡"],
                       ts_min=b + 21 * HOUR + 15 * MIN, approve=True)

    # LB-B03：维修后复验时再次熄灭
    adapter.set_override(box_id["LB-B03"], 0.0, 0, b + 21 * HOUR + 30 * MIN, "时控开关跳闸")
    _, f_re, kind = do_inspect(
        "LB-B03", "李巡", b + 21 * HOUR + 40 * MIN, 0.0, 0,
        note="计划时段不亮", set_min=b + 21 * HOUR + 30 * MIN)
    assert kind == "timer_misconfig", kind
    wo2 = svc.dispatch_work_order(conn, fault_id=f_re["id"], assignee="孙师傅",
                                  created_min=b + 21 * HOUR + 45 * MIN,
                                  eta_min=b + 22 * HOUR, note="复位时控开关")
    svc.complete_repair(conn, work_order_id=wo2["id"], done_min=b + 22 * HOUR,
                        note="已复位", adapter=adapter)
    adapter.set_override(box_id["LB-B03"], 0.0, 0, b + 22 * HOUR + 3 * MIN, "修复后再次失电")
    res2 = svc.reverify(conn, fault_id=f_re["id"], inspector_id=insp["王巡"],
                        ts_min=b + 22 * HOUR + 10 * MIN, adapter=adapter,
                        note="仍黑屏，维修后再次熄灭")
    assert not res2["passed"], res2

    # LB-C01：画面撤换空档巡检
    _, f_art, kind = do_inspect("LB-C01", "王巡", b + 20 * HOUR + 20 * MIN, 0.0, 0,
                                note="画面已撤，灯箱空置")
    assert kind == "artwork_removed", kind

    # === 周二：白天误判 + 健康灯箱亮灯证据 ===
    do_inspect("LB-A01", "李巡", b + DAY + 10 * HOUR, 0.0, 0,
               note="白天巡查不亮，属计划外，非故障")
    do_inspect("LB-B02", "张巡", b + DAY + 19 * HOUR + 40 * MIN, 1.0, 4,
               note="亮灯正常")

    # 供电中断主单预置一张进行中的维修单（预计当晚 22:00 恢复）
    svc.dispatch_work_order(conn, fault_id=f_power["id"], assignee="市政抢修·钱师傅",
                            created_min=b + DAY + 9 * HOUR,
                            eta_min=b + DAY + 22 * HOUR,
                            note="环网柜 03 缺相，等待备件送电")

    conn.commit()
    conn.close()
    return db_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="预置灯箱运营样例数据")
    ap.add_argument("--db", default="data/lightbox.db")
    ap.add_argument("--photos", default="data/photos")
    args = ap.parse_args()
    seed(args.db, args.photos)
    print(f"已预置样例：数据库 {args.db}，照片目录 {args.photos}")
