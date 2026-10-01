"""HTTP 界面：点位地图、列表、故障详情、维修派单、补播核算。

只依赖标准库。演示时钟固定在 seed.NOW（20:00，亮灯时段内）。
配色：白 #FFFFFF / 墨灰 #2A2D34，状态用灯箱画面预览色点缀，
所有状态同时给出文字结论。
"""
from __future__ import annotations

import html
import json
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .clock import Clock, VirtualClock, from_iso, to_iso
from .db import connect, init_db
from .engine import DomainError, Engine, KIND_TEXT
from . import makeup
from .schedule import (describe_schedule, overlap_minutes,
                      scheduled_intervals_between)
from . import seed as seed_mod

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "lightbox.db"
PHOTO_DIR = DATA / "photos"

STATE_COLOR = {
    "healthy": "#2E9E6B",
    "partial": "#E8A33B",
    "outage": "#C0392B",
    "timer": "#7A4FBF",
    "relapse": "#B23A48",
    "daytime": "#8A9099",
    "creative": "#D65B93",
}
STATE_TEXT = {
    "healthy": "正常点亮",
    "partial": "部分灯带不亮（半亮）",
    "outage": "供电中断·全黑",
    "timer": "定时器配置错",
    "relapse": "维修后再次熄灭",
    "daytime": "计划外熄灭（非故障）",
    "creative": "画面已撤换（非亮灯故障）",
}


def h(x) -> str:
    return html.escape("" if x is None else str(x))


# ---------------- 视图辅助 ----------------

def latest_overview(conn: sqlite3.Connection, clock: Clock) -> list[dict]:
    now = clock.now()
    boxes = conn.execute(
        "SELECT b.*, c.name AS circuit_name, c.status AS circuit_status,"
        " a.advertiser, a.version FROM boxes b"
        " JOIN circuits c ON c.id=b.circuit_id"
        " LEFT JOIN contracts a ON a.box_id=b.id AND a.valid_from<=?"
        "   AND (a.valid_to IS NULL OR a.valid_to>?) "
        "ORDER BY b.id",
        (to_iso(now), to_iso(now)),
    ).fetchall()
    out = []
    for b in boxes:
        r = conn.execute(
            "SELECT ts, fraction FROM signal_readings WHERE box_id=? "
            "ORDER BY ts DESC LIMIT 1", (b["id"],)).fetchone()
        insp = conn.execute(
            "SELECT ts, classification, within_schedule FROM inspections"
            " WHERE box_id=? ORDER BY ts DESC LIMIT 1", (b["id"],)).fetchone()
        open_fault = conn.execute(
            "SELECT id, kind FROM faults WHERE box_id=? AND status='open'"
            " ORDER BY id LIMIT 1", (b["id"],)).fetchone()
        out.append(dict(b=b, reading=r, insp=insp, open_fault=open_fault))
    return out


def evidence_text(item: dict) -> tuple[str, str]:
    """返回 (状态分类, 文字结论)。"""
    b, r, insp = item["b"], item["reading"], item["insp"]
    if r is None:
        return "daytime", "暂无亮灯信号证据"
    within = bool(insp and insp["within_schedule"])
    frac = r["fraction"]
    if item["open_fault"]:
        cls = item["open_fault"]["kind"]
    elif insp:
        cls = insp["classification"]
    else:
        cls = "healthy" if frac >= 0.999 else (
            "partial" if frac > 0 else "daytime")
    if not within:
        cls = "timer" if frac > 0.001 else "daytime"
    pct = f"{frac:.0%} 亮"
    ts = r["ts"][5:16].replace("T", " ")
    base = STATE_TEXT.get(cls, cls)
    return cls, f"{base}｜最近证据 {ts} 信号 {pct}"


def fmt_minutes(x: float) -> str:
    x = round(x, 1)
    if abs(x - round(x)) < 0.05:
        return f"{int(round(x))} 分钟"
    return f"{x} 分钟"


# ---------------- HTML 外壳 ----------------

CSS = """
:root{--ink:#2A2D34;--sub:#6B7076;--line:#E3E6EA;--bg:#FFFFFF;--accent:#2A2D34;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 color:var(--ink);background:var(--bg);font-size:14px;line-height:1.55}
header{border-bottom:1px solid var(--line);padding:12px 24px;display:flex;
 gap:20px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:17px;margin:0;letter-spacing:1px}
nav a{color:var(--sub);text-decoration:none;margin-right:16px;font-size:13px}
nav a:hover{color:var(--ink)}
.wrap{padding:20px 24px;max-width:1180px;margin:0 auto}
.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:20px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}
th{color:var(--sub);font-weight:600}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;color:#fff;
 font-size:12px;white-space:nowrap}
.muted{color:var(--sub)}
.rowline{display:flex;justify-content:space-between;gap:10px;
 border-bottom:1px solid var(--line);padding:9px 2px}
a.boxlink{color:var(--ink);text-decoration:none;font-weight:600}
a.boxlink:hover{text-decoration:underline}
button,.btn{background:var(--ink);color:#fff;border:0;border-radius:6px;
 padding:6px 14px;font-size:13px;cursor:pointer}
button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
input,select{border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13px}
label{font-size:12px;color:var(--sub);margin-right:8px}
.flash{background:#FDECEA;border:1px solid #E6B8B2;border-radius:8px;
 padding:9px 12px;margin-bottom:14px;color:#8A2A20}
.ok{background:#EAF6F0;border-color:#BFE0CE;color:#1E5E42}
.swatch{display:inline-block;width:12px;height:12px;border-radius:3px;
 border:1px solid rgba(0,0,0,.15);vertical-align:-1px;margin-right:5px}
.legend span{margin-right:14px;font-size:12px;color:var(--sub)}
.timeline{position:relative;height:46px;background:#F4F5F7;border-radius:6px;margin:6px 0}
.tl-plan{position:absolute;top:6px;height:14px;background:#DDE6F7;border-radius:3px}
.tl-black{position:absolute;top:26px;height:14px;border-radius:3px}
.thumb{width:150px;border:1px solid var(--line);border-radius:6px}
.kvs td:first-child{color:var(--sub);width:120px;white-space:nowrap}
"""


def page(title: str, body: str, flash: str = "") -> bytes:
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}｜灯箱亮灯监管</title><style>{CSS}</style></head><body>
<header><h1>灯箱亮灯监管台</h1>
<nav><a href="/">点位地图</a><a href="/tickets">维修派单</a>
<a href="/faults">故障与补播</a><a href="/inspect">巡查登记</a></nav>
<span class="muted" style="margin-left:auto;font-size:12px">演示时钟 2026-10-01 20:00 (+08:00)</span>
</header><div class="wrap">{flash}{body}</div></body></html>"""
    return doc.encode()


def flash_html(err: str, ok: str = "") -> str:
    if err:
        return f'<div class="flash">⚠ {h(err)}</div>'
    if ok:
        return f'<div class="flash ok">✓ {h(ok)}</div>'
    return ""


def state_pill(cls: str) -> str:
    color = STATE_COLOR.get(cls, "#8A9099")
    return (f'<span class="pill" style="background:{color}">'
            f'{h(STATE_TEXT.get(cls, cls))}</span>')


# ---------------- 页面 ----------------

def view_map(conn, clock) -> str:
    items = latest_overview(conn, clock)
    lats = [i["b"]["lat"] for i in items]
    lngs = [i["b"]["lng"] for i in items]
    W, H, PAD = 720, 460, 60

    def xy(lat, lng):
        x = PAD + (lng - min(lngs)) / (max(lngs) - min(lngs)) * (W - 2 * PAD)
        y = H - PAD - (lat - min(lats)) / (max(lats) - min(lats)) * (H - 2 * PAD)
        return x, y

    markers = []
    for i in items:
        b = i["b"]
        x, y = xy(b["lat"], b["lng"])
        cls, text = evidence_text(i)
        color = STATE_COLOR.get(cls, "#8A9099")
        markers.append(f"""<g>
<a href="/box?id={h(b['id'])}">
<circle cx="{x:.0f}" cy="{y:.0f}" r="15" fill="{color}"
 stroke="{h(b['creative_hex'])}" stroke-width="3" opacity="0.92"/>
<text x="{x:.0f}" y="{y+4:.0f}" text-anchor="middle" font-size="10"
 fill="#fff" font-weight="700">{h(b['id'][1:])}</text>
<text x="{x:.0f}" y="{y+30:.0f}" text-anchor="middle" font-size="11"
 fill="#2A2D34">{h(b['id'])}</text></a></g>""")
    svg = (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'aria-label="点位地图" style="background:#FAFAFB;border:1px solid '
           f'var(--line);border-radius:10px">'
           + "".join(markers) + "</svg>")
    legend = '<div class="legend" style="margin-top:8px">' + "".join(
        f'<span><i class="dot" style="background:{c}"></i>{h(t)}</span>'
        for c, t in [(STATE_COLOR["healthy"], "正常点亮"),
                     (STATE_COLOR["partial"], "半亮"),
                     (STATE_COLOR["outage"], "全黑/中断"),
                     (STATE_COLOR["timer"], "定时器错"),
                     (STATE_COLOR["relapse"], "修复后再灭"),
                     (STATE_COLOR["creative"], "画面撤换"),
                     (STATE_COLOR["daytime"], "计划外/非故障")]) + "</div>"

    rows = []
    for i in items:
        b = i["b"]
        cls, text = evidence_text(i)
        rows.append(f"""<div class="rowline">
 <div><a class="boxlink" href="/box?id={h(b['id'])}">{h(b['id'])}</a>
 <span class="muted">{h(b['name'])}</span><br>
 <span class="muted" style="font-size:12px">{h(text)}</span></div>
 <div style="text-align:right">{state_pill(cls)}
 <div class="muted" style="font-size:12px;margin-top:4px">
 <span class="swatch" style="background:{h(b['creative_hex'])}"></span>
 现行画面：{h(b['advertiser'] or '无有效合同')}</div></div></div>""")
    return f"""<div class="grid"><div>
 <div class="card"><h2>点位地图（8 处）</h2>{svg}{legend}
 <p class="muted" style="font-size:12px;margin-bottom:0">圆点外环为灯箱当前
 广告画面预览色；点击点位查看详情。颜色与右侧文字结论一一对应。</p></div></div>
 <div class="card"><h2>灯箱列表 · 编号与最近亮灯证据</h2>
 {''.join(rows)}</div></div>"""


def view_box(conn, clock, box_id) -> Optional[str]:
    b = conn.execute(
        "SELECT b.*, c.name circuit_name, c.status circuit_status FROM boxes b"
        " JOIN circuits c ON c.id=b.circuit_id WHERE b.id=?",
        (box_id,)).fetchone()
    if not b:
        return None
    sched = "；".join(describe_schedule(conn, box_id))
    contracts = conn.execute(
        "SELECT * FROM contracts WHERE box_id=? ORDER BY valid_from DESC",
        (box_id,)).fetchall()
    cinsp = conn.execute(
        "SELECT * FROM inspections WHERE box_id=? ORDER BY ts DESC LIMIT 5",
        (box_id,)).fetchall()
    faults = conn.execute(
        "SELECT * FROM faults WHERE id IN (SELECT fault_id FROM fault_boxes"
        " WHERE box_id=?) ORDER BY id", (box_id,)).fetchall()
    photos = []
    for r in cinsp:
        if r["photo_path"]:
            p = Path(r["photo_path"])
            photos.append((r, f"/photos/{p.name}" if p.exists() else None))
    photo_html = "".join(
        f'<figure style="display:inline-block;margin:0 12px 10px 0">'
        f'<img class="thumb" src="{src}" alt="巡检照片">'
        f'<figcaption class="muted" style="font-size:11px">'
        f'{h(r["ts"][5:16].replace("T"," "))} · '
        f'{h(STATE_TEXT.get(r["classification"], r["classification"]))}'
        f'</figcaption></figure>'
        for r, src in photos if src)
    crows = "".join(
        f"<tr><td>{h(x['advertiser'])}</td><td>{h(x['version'])}</td>"
        f"<td>{h(x['valid_from'][:10])} 起</td>"
        f"<td>{h((x['valid_to'] or '现行')[:10])}</td>"
        f"<td>×{x['comp_factor']}</td></tr>" for x in contracts)
    frows = "".join(
        f'<tr><td><a href="/fault?id={x["id"]}">#{x["id"]}</a></td>'
        f'<td>{h(KIND_TEXT[x["kind"]])}</td><td>{h(x["status"])}</td>'
        f'<td>{h(x["first_seen_at"][5:16].replace("T"," "))}</td></tr>'
        for x in faults) or '<tr><td colspan="4" class="muted">无故障记录</td></tr>'
    irows = "".join(
        f'<tr><td>{h(r["ts"][5:16].replace("T"," "))}</td>'
        f'<td>{h(r["inspector"])}</td>'
        f'<td>{"计划内" if r["within_schedule"] else "计划外"}</td>'
        f'<td>{r["fraction"]:.0%}</td>'
        f'<td>{h(STATE_TEXT.get(r["classification"], r["classification"]))}</td>'
        f'<td class="muted">{h(r["note"])}</td></tr>' for r in cinsp)
    return f"""
<div class="card"><h2>{h(b['id'])} · {h(b['name'])}</h2>
<table class="kvs"><tr><td>点位地址</td><td>{h(b['addr'])}</td></tr>
<tr><td>坐标</td><td>{b['lat']:.4f}, {b['lng']:.4f}</td></tr>
<tr><td>供电回路</td><td>{h(b['circuit_id'])} {h(b['circuit_name'])}
 <span class="pill" style="background:{'#C0392B' if b['circuit_status']=='interrupted' else '#2E9E6B'}">
 {'供电中断' if b['circuit_status']=='interrupted' else '供电正常'}</span></td></tr>
<tr><td>亮灯计划</td><td>{h(sched)}</td></tr>
<tr><td>灯带</td><td>{b['strips_total']} 条</td></tr>
<tr><td>画面预览色</td><td><span class="swatch" style="background:{h(b['creative_hex'])}"></span>{h(b['creative_hex'])}</td></tr>
</table></div>
<div class="card"><h2>合同版本（补播按版本计）</h2>
<table><tr><th>广告主</th><th>版本</th><th>生效</th><th>截止</th><th>补播系数</th></tr>{crows}</table></div>
<div class="card"><h2>巡检照片</h2>{photo_html or '<span class="muted">暂无</span>'}</div>
<div class="card"><h2>故障单</h2>
<table><tr><th>单号</th><th>类型</th><th>状态</th><th>首报</th></tr>{frows}</table></div>
<div class="card"><h2>最近巡检记录</h2>
<table><tr><th>时间</th><th>巡查员</th><th>时段</th><th>亮灯</th><th>结论</th><th>备注</th></tr>{irows}</table></div>"""


def _segment_timeline(conn, box_id, segs, now):
    if not segs:
        return ""
    lo = from_iso(min(s["start_ts"] for s in segs))
    hi = max(now, *(from_iso(s["end_ts"]) for s in segs
                    if s["end_ts"] != s["start_ts"]),
             *(from_iso(s["start_ts"]) for s in segs))
    span = max((hi - lo).total_seconds(), 1)

    def pct(t):
        return (t - lo).total_seconds() / span * 100

    bars = []
    for a, z in scheduled_intervals_between(conn, box_id, lo, hi):
        bars.append(
            f'<div class="tl-plan" title="计划 {a:%m-%d %H:%M}–{z:%H:%M}"'
            f' style="left:{pct(a):.2f}%;width:{max((z-a).total_seconds()/span*100,0.6):.2f}%"></div>')
    for s in segs:
        a = from_iso(s["start_ts"])
        z = from_iso(s["end_ts"]) if s["end_ts"] != s["start_ts"] else now
        color = "#E8A33B" if s["deficit"] < 1 else "#C0392B"
        bars.append(
            f'<div class="tl-black" title="黑屏 {a:%m-%d %H:%M} 起"'
            f' style="left:{pct(a):.2f}%;width:{max((z-a).total_seconds()/span*100,0.6):.2f}%;'
            f'background:{color}"></div>')
    return (f'<div class="timeline">{"".join(bars)}</div>'
            '<div class="muted" style="font-size:12px">蓝条＝计划应亮时段；'
            '红条＝实际全黑；黄条＝半亮（按 50% 折算）</div>')


def view_fault(conn, clock, fid) -> Optional[str]:
    f = conn.execute("SELECT * FROM faults WHERE id=?", (fid,)).fetchone()
    if not f:
        return None
    now = clock.now()
    affected = conn.execute(
        "SELECT fb.box_id, b.name, c.advertiser FROM fault_boxes fb"
        " JOIN boxes b ON b.id=fb.box_id"
        " LEFT JOIN contracts c ON c.box_id=b.id AND c.valid_from<=?"
        "   AND (c.valid_to IS NULL OR c.valid_to>?)"
        " WHERE fb.fault_id=? ORDER BY fb.box_id",
        (to_iso(now), to_iso(now), fid)).fetchall()
    aff = "、".join(
        f'{r["box_id"]}（{h(r["advertiser"] or "无现行合同")}）' for r in affected)

    blocks = []
    segs_all = conn.execute(
        "SELECT * FROM blackout_segments WHERE fault_id=? ORDER BY box_id,start_ts",
        (fid,)).fetchall()
    for box_id in sorted({s["box_id"] for s in segs_all}):
        segs = [s for s in segs_all if s["box_id"] == box_id]
        rows = []
        for n, s in enumerate(segs, 1):
            a = from_iso(s["start_ts"])
            ongoing = s["end_ts"] == s["start_ts"]
            z = now if ongoing else from_iso(s["end_ts"])
            plan = overlap_minutes(conn, box_id, a, z)
            black = overlap_minutes(conn, box_id, a, z)
            rows.append(
                f"<tr><td>第 {n} 轮{'（进行中）' if ongoing else ''}</td>"
                f"<td>{a:%m-%d %H:%M}</td>"
                f"<td>{'至今' if ongoing else f'{z:%m-%d %H:%M}'}</td>"
                f"<td>{fmt_minutes(plan)}</td><td>{fmt_minutes(black)}</td>"
                f"<td>{'半亮 ×0.5' if s['deficit'] < 1 else '全黑 ×1.0'}</td>"
                f"<td>{fmt_minutes(black * s['deficit'])}</td></tr>")
        tl = _segment_timeline(conn, box_id, segs, now)
        blocks.append(
            f'<h3 style="font-size:13px;margin:14px 0 6px">{h(box_id)}'
            f' 计划时段 vs 实际黑屏</h3>{tl}'
            '<table><tr><th>轮次</th><th>黑屏起</th><th>黑屏止</th>'
            '<th>计划应亮</th><th>实际黑屏(仅计时段内)</th>'
            '<th>缺光</th><th>折算应补</th></tr>'
            + "".join(rows) + "</table>")

    # 维修单
    tickets = conn.execute(
        "SELECT t.*, u.name assignee_name FROM repair_tickets t"
        " LEFT JOIN users u ON u.login=t.assignee WHERE t.fault_id=? ORDER BY t.id",
        (fid,)).fetchall()
    tk = []
    status_text = {"dispatched": "已派单·维修中", "repaired": "已修复·待复验",
                   "reopened": "复验失败·重开", "verified": "复验通过·已关闭",
                   "closed": "已关闭"}
    for t in tickets:
        tk.append(
            f'<tr><td><a href="/ticket?id={t["id"]}">#{t["id"]}</a></td>'
            f'<td>{h(t["box_id"])}</td><td>{h(t["assignee_name"] or "")}</td>'
            f'<td><span class="pill" style="background:#2A2D34">'
            f'{h(status_text[t["status"]])}</span></td>'
            f'<td>{h((t["expected_restore"] or "")[5:16].replace("T"," "))}</td></tr>')
    ticket_table = (
        '<table><tr><th>工单</th><th>灯箱</th><th>维修人</th><th>状态</th>'
        f'<th>预计恢复</th></tr>{"".join(tk)}</table>')

    # 补播
    data = makeup.compute_fault(conn, clock, fid, at=now)
    mrows = []
    for box_id, shares in data["boxes"].items():
        for v in shares:
            mrows.append(
                f"<tr><td>{h(box_id)}</td><td>{h(v.advertiser)}</td>"
                f"<td>{h(v.version)}</td><td>{fmt_minutes(v.blackout_minutes)}</td>"
                f"<td>×{v.comp_factor}</td><td><b>{fmt_minutes(v.credit)}</b></td></tr>")
    if not mrows:
        mrows.append('<tr><td colspan="6" class="muted">暂无可计黑屏</td></tr>')
    pending = conn.execute(
        "SELECT * FROM makeup_conclusions WHERE fault_id=? AND status='pending'",
        (fid,)).fetchone()
    decided = conn.execute(
        "SELECT * FROM makeup_conclusions WHERE fault_id=? AND status!='pending'"
        " ORDER BY id DESC LIMIT 1", (fid,)).fetchone()

    approve_ui = ""
    if pending:
        approve_ui = f"""<p class="muted">待审批结论 #{pending['id']}：
         由巡查员 {h(pending['proposed_by'])} 于
         {h(pending['proposed_at'][5:16].replace('T',' '))} 提交。
         <b>提交人不得审批自己的结论</b>。</p>
        <form method="post" action="/makeup/decide" style="display:flex;gap:8px">
         <input type="hidden" name="id" value="{pending['id']}">
         <label>审批人 <select name="actor">
          <option value="zhang">张巡（巡查员）</option>
          <option value="li">李巡（提交人，禁止）</option>
          <option value="zhao">赵主管</option></select></label>
         <button name="approve" value="1">批准补播</button>
         <button class="ghost" name="approve" value="0">驳回</button></form>"""
    elif f["status"] == "open":
        approve_ui = """<form method="post" action="/makeup/propose">
         <input type="hidden" name="fault_id" value="%d">
         <label>由巡查员提交补播结论
         <select name="inspector"><option value="li">李巡</option>
         <option value="zhang">张巡</option></select></label>
         <button>提交补播结论待审批</button></form>""" % fid
    if decided:
        approve_ui += (f'<p style="margin-top:8px">最近审批：'
                       f'{h(decided["proposed_by"])} 提交 → '
                       f'<b>{"已批准" if decided["status"]=="approved" else "已驳回"}</b>'
                       f'（{h(decided["decided_by"])}）</p>')

    return f"""
<div class="card"><h2>故障 #{f['id']} · {h(KIND_TEXT[f['kind']])}
 {state_pill(f['kind'] if f['status']=='open' else 'healthy')}</h2>
<table class="kvs"><tr><td>状态</td><td>{h(status_text.get(f['status'], f['status']))}</td></tr>
<tr><td>首报 / 最近</td><td>{h(f['first_seen_at'][5:16].replace('T',' '))} ／ {h(f['last_seen_at'][5:16].replace('T',' '))}</td></tr>
<tr><td>关联回路</td><td>{h(f['circuit_id'] or '—（单箱故障）')}</td></tr>
<tr><td>受影响广告主</td><td>{aff}</td></tr>
<tr><td>预计恢复</td><td>{h((f['expected_restore'] or '待派单')[5:16].replace('T',' ') if f['expected_restore'] else '待派单')}</td></tr>
</table></div>
<div class="card"><h2>计划亮灯时段 与 实际黑屏 对照</h2>{''.join(blocks)}</div>
<div class="card"><h2>维修派单</h2>{ticket_table}</div>
<div class="card"><h2>补播核算（按合同版本）</h2>
<table><tr><th>灯箱</th><th>广告主</th><th>合同版本</th>
<th>时段内黑屏</th><th>版本系数</th><th>应补播时长</th></tr>{''.join(mrows)}</table>
<p style="margin-top:8px">合计应补播：<b>{fmt_minutes(data['totals']['weighted'])}</b>
（半亮折算后）；补播积分合计 <b>{round(data['totals']['credit'],1)}</b>
{'；故障仍在进行，数字会继续累计' if data['ongoing'] else '；故障已结束'}</p>
{approve_ui}</div>"""


def view_tickets(conn) -> str:
    rows = conn.execute(
        "SELECT t.*, f.kind, b.name box_name FROM repair_tickets t"
        " JOIN faults f ON f.id=t.fault_id JOIN boxes b ON b.id=t.box_id"
        " ORDER BY CASE t.status WHEN 'dispatched' THEN 0 WHEN 'reopened' THEN 1"
        " WHEN 'repaired' THEN 2 ELSE 3 END, t.id").fetchall()
    out = ['<div class="card"><h2>维修派单</h2>',
           '<form method="post" action="/dispatch" style="margin-bottom:14px">',
           '<label>故障 <select name="fault_id">']
    for r in conn.execute("SELECT id,box_id,kind FROM faults WHERE status='open'"
                          " ORDER BY id"):
        out.append(f'<option value="{r["id"]}">#{r["id"]} {r["box_id"]} '
                   f'{h(KIND_TEXT[r["kind"]])}</option>')
    out.append("</select></label><label>灯箱 <select name='box_id'>")
    for r in conn.execute("SELECT id,name FROM boxes ORDER BY id"):
        out.append(f'<option value="{r["id"]}">{r["id"]} {h(r["name"])}</option>')
    out.append("</select></label><label>维修人 <select name='assignee'>"
               "<option value='wang'>王维修（维修调度）</option></select></label>"
               "<label>预计恢复 <input type='text' name='restore' "
               "value='2026-10-01T22:00:00+08:00' size='24'></label>"
               "<button>派单</button></form>"
               '<p class="muted" style="font-size:12px;margin-top:-6px">'
               '同一灯箱存在未关闭工单时，派单会被系统拒绝（禁止平行重复单）。</p>')
    out.append('<table><tr><th>工单</th><th>灯箱</th><th>故障</th><th>状态</th>'
               '<th>维修人</th><th>操作</th></tr>')
    status_text = {"dispatched": "已派单", "repaired": "待复验",
                   "reopened": "已重开", "verified": "已关闭", "closed": "已关闭"}
    for t in rows:
        ops = f'<a href="/ticket?id={t["id"]}">处理 →</a>'
        out.append(f'<tr><td>#{t["id"]}</td><td>{t["box_id"]}</td>'
                   f'<td>{h(KIND_TEXT[t["kind"]])}</td>'
                   f'<td>{h(status_text[t["status"]])}</td>'
                   f'<td>{h(t["assignee"] or "")}</td><td>{ops}</td></tr>')
    out.append("</table></div>")
    return "".join(out)


def view_ticket(conn, clock, tid) -> Optional[str]:
    t = conn.execute(
        "SELECT t.*, b.name box_name, f.kind FROM repair_tickets t"
        " JOIN boxes b ON b.id=t.box_id JOIN faults f ON f.id=t.fault_id"
        " WHERE t.id=?", (tid,)).fetchone()
    if not t:
        return None
    events = conn.execute(
        "SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id", (tid,))
    ev = "".join(
        f'<li>{h(r["ts"][5:16].replace("T"," "))} · {h(r["actor"])} · '
        f'{h(r["action"])} — {h(r["detail"])}</li>' for r in events)
    actions = []
    if t["status"] in ("dispatched", "reopened"):
        actions.append(f"""<form method="post" action="/repair">
         <input type="hidden" name="id" value="{tid}">
         <label>填报人 <select name="actor"><option value="wang">王维修</option></select></label>
         <label><input type="checkbox" name="fixed" value="1" checked>
          现场已修复（恢复信号/供电）</label>
         <button>填报修复，转待复验</button></form>""")
    elif t["status"] == "repaired":
        actions.append(f"""<form method="post" action="/verify">
         <input type="hidden" name="id" value="{tid}">
         <label>复验巡查员
         <select name="inspector">
          <option value="zhang">张巡</option>
          <option value="li">李巡</option></select></label>
         <button>现场复验</button></form>
         <p class="muted" style="font-size:12px">维修人（{h(t['repair_by'])}）
         不能复验自己的工单；复验须在亮灯时段内（当前 20:00 ✓）。</p>""")
    else:
        actions.append('<p class="muted">工单已关闭。</p>')
    return f"""<div class="card"><h2>维修单 #{t['id']} · {h(t['box_id'])}
     {h(t['box_name'])}</h2>
 <table class="kvs"><tr><td>所属故障</td><td><a href="/fault?id={t['fault_id']}">
 #{t['fault_id']} {h(KIND_TEXT[t['kind']])}</a></td></tr>
 <tr><td>状态</td><td>{h(t['status'])}</td></tr>
 <tr><td>派单/修复/复验</td><td>{h((t['created_at'] or '')[5:16])} ／
  {h((t['repaired_at'] or '—')[5:16])} ／ {h((t['verified_at'] or '—')[5:16])}</td></tr>
 <tr><td>预计恢复</td><td>{h((t['expected_restore'] or '—'))}</td></tr></table></div>
 <div class="card"><h2>处理</h2>{''.join(actions)}</div>
 <div class="card"><h2>处理记录</h2><ul>{ev}</ul></div>"""


def view_inspect():
    opts_box = "".join(
        f'<option value="{r["id"]}">{r["id"]} {h(r["name"])}</option>'
        for r in connect(DB_PATH).execute(
            "SELECT id,name FROM boxes ORDER BY id"))
    return f"""<div class="card"><h2>巡查登记（规定时间拍照 + 亮灯情况）</h2>
 <form method="post" action="/inspect">
 <p><label>灯箱 <select name="box_id">{opts_box}</select></label>
 <label>巡查员 <select name="inspector">
  <option value="li">李巡</option><option value="zhang">张巡</option></select></label></p>
 <p><label><input type="checkbox" name="creative_match" value="1" checked>
  现场画面与登记合同一致（取消勾选＝画面已撤换/不符）</label></p>
 <p><label>备注 <input type="text" name="note" size="50"></label></p>
 <p><button>拍照并记录亮灯</button></p>
 <p class="muted" style="font-size:12px">系统读取模拟亮灯信号并自动判定：
 计划外熄灭＝非故障；画面撤换＝非亮灯故障；计划内全黑/半亮/定时异常
 分别归并到对应故障单；修复后再灭判为 relapse 并重开原单。</p></form></div>"""


def view_faults(conn):
    rows = conn.execute(
        "SELECT f.*, (SELECT GROUP_CONCAT(box_id) FROM fault_boxes WHERE fault_id=f.id) bs"
        " FROM faults f ORDER BY f.status='closed_nofault', f.id").fetchall()
    out = ['<div class="card"><h2>故障与补播</h2><table>',
           '<tr><th>单号</th><th>类型</th><th>状态</th><th>点位</th>'
           '<th>首报</th><th></th></tr>']
    for r in rows:
        out.append(f'<tr><td>#{r["id"]}</td><td>{h(KIND_TEXT[r["kind"]])}</td>'
                   f'<td>{h(r["status"])}</td><td>{h(r["bs"])}</td>'
                   f'<td>{h(r["first_seen_at"][5:16].replace("T"," "))}</td>'
                   f'<td><a href="/fault?id={r["id"]}">详情与补播 →</a></td></tr>')
    out.append("</table></div>")
    return "".join(out)


# ---------------- HTTP 服务 ----------------

class App(BaseHTTPRequestHandler):
    server_version = "LightboxMonitor/1.0"

    @property
    def clock(self) -> Clock:
        return self.server.clock

    _local = threading.local()

    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.server.db_path)
            self._local.conn = conn
        return conn

    def log_message(self, *a):  # 安静
        pass

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html"):
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, err: str = "", ok: str = ""):
        q = ""
        if err:
            q = "?err=" + urllib.parse.quote(err)
        elif ok:
            q = "?ok=" + urllib.parse.quote(ok)
        self.send_response(303)
        self.send_header("Location", path + q)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        conn, clock = self._db(), self.clock
        err = q.get("err", [""])[0]
        ok = q.get("ok", [""])[0]
        flash = flash_html(err, ok)
        try:
            if u.path == "/":
                self._send(page("点位地图", view_map(conn, clock), flash))
            elif u.path == "/box":
                body = view_box(conn, clock, q["id"][0])
                self._send(page("灯箱详情", body or "无此灯箱", flash),
                           200 if body else 404)
            elif u.path == "/fault":
                body = view_fault(conn, clock, int(q["id"][0]))
                self._send(page("故障详情", body or "无此故障", flash),
                           200 if body else 404)
            elif u.path == "/faults":
                self._send(page("故障与补播", view_faults(conn), flash))
            elif u.path == "/tickets":
                self._send(page("维修派单", view_tickets(conn), flash))
            elif u.path == "/ticket":
                body = view_ticket(conn, clock, int(q["id"][0]))
                self._send(page("维修单", body or "无此工单", flash),
                           200 if body else 404)
            elif u.path == "/inspect":
                self._send(page("巡查登记", view_inspect(), flash))
            elif u.path.startswith("/photos/"):
                name = Path(u.path).name
                p = (PHOTO_DIR / name).resolve()
                if PHOTO_DIR.resolve() not in p.parents or not p.exists():
                    self._send(b"not found", 404, "text/plain")
                else:
                    self._send(p.read_bytes(), 200, "image/png")
            else:
                self._send(page("未找到", "404", ""), 404)
        except DomainError as e:
            self._send(page("业务错误", flash_html(str(e)), ), 409)

    def _form(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        f = self._form()
        conn, clock = self._db(), self.clock
        engine = Engine(conn, clock, str(PHOTO_DIR))
        try:
            if u.path == "/inspect":
                r = engine.inspect(
                    f["box_id"], f["inspector"],
                    creative_match=bool(f.get("creative_match")),
                    note=f.get("note", ""))
                target = (f"/fault?id={r.fault_id}" if r.fault_id
                          else f"/box?id={f['box_id']}")
                self._redirect(target, ok=f"{r.classification}：{r.message}")
            elif u.path == "/dispatch":
                restore = f.get("restore") or None
                rt = from_iso(restore) if restore else None
                tid = engine.dispatch(int(f["fault_id"]), f["box_id"],
                                      f["assignee"], rt)
                self._redirect(f"/ticket?id={tid}", ok=f"已派单 #{tid}")
            elif u.path == "/repair":
                tid = int(f["id"])
                t = conn.execute("SELECT box_id, fault_id FROM repair_tickets"
                                 " WHERE id=?", (tid,)).fetchone()
                if f.get("fixed"):
                    conn.execute("UPDATE boxes SET sim_mode='auto' WHERE id=?",
                                 (t["box_id"],))
                    conn.execute("UPDATE circuits SET status='normal' WHERE id="
                                 "(SELECT circuit_id FROM boxes WHERE id=?)",
                                 (t["box_id"],))
                    conn.commit()
                engine.complete_repair(tid, f["actor"],
                                       "现场修复" if f.get("fixed")
                                       else "未彻底修复")
                self._redirect(f"/ticket?id={tid}", ok="已填报修复，等待复验")
            elif u.path == "/verify":
                r = engine.verify(int(f["id"]), f["inspector"])
                self._redirect(f"/ticket?id={f['id']}", ok=r.message)
            elif u.path == "/makeup/propose":
                cid = makeup.propose(conn, clock, int(f["fault_id"]),
                                     f["inspector"])
                self._redirect(f"/fault?id={f['fault_id']}",
                               ok=f"补播结论 #{cid} 已提交，待他人审批")
            elif u.path == "/makeup/decide":
                makeup.decide(conn, clock, int(f["id"]), f["actor"],
                              f.get("approve") == "1")
                mc = conn.execute("SELECT fault_id FROM makeup_conclusions"
                                  " WHERE id=?", (int(f["id"]),)).fetchone()
                self._redirect(f"/fault?id={mc['fault_id']}",
                               ok="已批准" if f.get("approve") == "1"
                               else "已驳回")
            else:
                self._send(b"404", 404, "text/plain")
        except (DomainError, makeup.MakeupError, ValueError, KeyError) as e:
            back = self.headers.get("Referer", "/")
            self._redirect(urllib.parse.urlparse(back).path or "/",
                           err=str(e))


def main(host="127.0.0.1", port=8000):  # pragma: no cover
    if not DB_PATH.exists():
        seed_mod.build(DB_PATH, PHOTO_DIR)
    init_db(connect(DB_PATH))
    httpd = ThreadingHTTPServer((host, port), App)
    httpd.db_path = DB_PATH
    httpd.clock = VirtualClock(seed_mod.NOW)
    print(f"灯箱亮灯监管台已启动：http://{host}:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    import sys

    kw = {}
    if len(sys.argv) > 1:
        kw["port"] = int(sys.argv[1])
    main(**kw)
