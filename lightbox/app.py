"""HTTP 应用：零依赖 stdlib http.server，提供静态界面 + JSON API。"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import clock, faults as svc
from .db import FAULT_KINDS, FAULT_STATUS, connect, init_db
from .photos import PhotoStore
from .signals import SimulatedSignalAdapter
from .times import describe_schedules

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

KIND_TEXT = dict(FAULT_KINDS)
KIND_TEXT["normal"] = "亮灯正常"
KIND_TEXT["daytime_false_positive"] = "白天误判（计划时段外，非故障）"


class AppState:
    def __init__(self, db_path, photo_root):
        self.db_path = str(db_path)
        self.photo_store = PhotoStore(photo_root)
        self.photo_root = Path(photo_root)

    def conn(self):
        conn = connect(self.db_path)
        return conn

    def adapter(self, conn):
        return SimulatedSignalAdapter(conn, clock=clock.now_min)


# ------------------------------------------------------------- 序列化

def _j(rows):
    return [dict(r) for r in rows]


def box_current_state(conn, box):
    """综合最近证据与未关闭故障，给出界面状态结论。"""
    last = conn.execute(
        "SELECT * FROM inspections WHERE lightbox_id=? ORDER BY ts_min DESC, id DESC LIMIT 1",
        (box["id"],),
    ).fetchone()
    # 最近一次“确实亮着”的证据（亮度满），用于地图/列表展示
    last_lit = conn.execute(
        """SELECT * FROM inspections WHERE lightbox_id=? AND brightness >= ?
           ORDER BY ts_min DESC, id DESC LIMIT 1""",
        (box["id"], svc.BRIGHTNESS_FULL),
    ).fetchone()
    open_fault = conn.execute(
        """SELECT * FROM faults WHERE lightbox_id=? AND status IN ('open','repaired','rejected')
           ORDER BY start_min DESC LIMIT 1""",
        (box["id"],),
    ).fetchone()
    # 供电中断主单可能挂在别的灯箱上，但本箱在受影响名单内
    if open_fault is None:
        row = conn.execute(
            """SELECT f.* FROM faults f
               JOIN fault_advertisers fa ON fa.fault_id=f.id
               WHERE fa.lightbox_id=? AND f.kind='power_outage'
                 AND f.status IN ('open','repaired','rejected')
               ORDER BY f.start_min DESC LIMIT 1""",
            (box["id"],),
        ).fetchone()
        if row:
            open_fault = row
    state = {
        "conclusion": "尚无巡检证据",
        "kind": None,
        "fault_id": None,
        "fault_status": None,
        "last_ts": None,
        "last_brightness": None,
        "last_photo": None,
        "last_inspector": None,
        "last_lit_ts": None,
        "last_lit_photo": None,
        "last_lit_inspector": None,
    }
    if last:
        state.update({
            "last_ts": last["ts_min"],
            "last_ts_label": clock.fmt(last["ts_min"]),
            "last_brightness": last["brightness"],
            "last_photo": last["photo_path"],
        })
        insp = conn.execute("SELECT name FROM inspectors WHERE id=?",
                            (last["inspector_id"],)).fetchone()
        state["last_inspector"] = insp["name"] if insp else None
    if last_lit:
        p = conn.execute("SELECT name FROM inspectors WHERE id=?",
                         (last_lit["inspector_id"],)).fetchone()
        state.update({
            "last_lit_ts": last_lit["ts_min"],
            "last_lit_ts_label": clock.fmt(last_lit["ts_min"]),
            "last_lit_photo": last_lit["photo_path"],
            "last_lit_inspector": p["name"] if p else None,
        })
    if open_fault:
        state["fault_id"] = open_fault["id"]
        state["fault_status"] = open_fault["status"]
        if open_fault["status"] == "repaired":
            state["conclusion"] = "维修完成待复验"
        elif open_fault["status"] == "rejected":
            state["conclusion"] = "复验不通过·维修后再次熄灭"
        else:
            state["conclusion"] = KIND_TEXT.get(open_fault["kind"], open_fault["kind"])
        state["kind"] = open_fault["kind"]
    elif last:
        if last["fault_id"]:
            f = conn.execute("SELECT kind,status FROM faults WHERE id=?",
                             (last["fault_id"],)).fetchone()
            if f and f["status"] == "verified_closed":
                state["conclusion"] = "已复验关闭"
                state["kind"] = f["kind"]
            else:
                state["conclusion"] = KIND_TEXT.get(f["kind"], "异常") if f else "异常"
        elif last["brightness"] >= 0.999:
            state["conclusion"] = "亮灯正常"
            state["kind"] = "normal"
        else:
            state["conclusion"] = "白天误判（计划时段外，非故障）"
            state["kind"] = "daytime_false_positive"
    return state


def serialize_box(conn, box):
    contract = conn.execute(
        """SELECT c.*, a.name advertiser_name FROM contracts c
           JOIN advertisers a ON a.id=c.advertiser_id
           WHERE c.lightbox_id=? AND c.start_min<=? AND c.end_min>=?
           ORDER BY c.start_min DESC LIMIT 1""",
        (box["id"], clock.now_min(), clock.now_min()),
    ).fetchone()
    circuit = None
    if box["circuit_id"]:
        circuit = conn.execute("SELECT * FROM circuits WHERE id=?",
                               (box["circuit_id"],)).fetchone()
    schedules = _j(conn.execute(
        "SELECT * FROM schedules WHERE lightbox_id=?", (box["id"],)).fetchall())
    state = box_current_state(conn, box)
    return {
        "id": box["id"], "code": box["code"], "name": box["name"],
        "address": box["address"], "map_x": box["map_x"], "map_y": box["map_y"],
        "circuit": dict(circuit) if circuit else None,
        "schedules": schedules,
        "schedule_text": describe_schedules(schedules),
        "contract": dict(contract) if contract else None,
        "state": state,
    }


def overview(conn):
    boxes = conn.execute("SELECT * FROM lightboxes ORDER BY code").fetchall()
    return [serialize_box(conn, b) for b in boxes]


def fault_detail(conn, fid):
    f = conn.execute("SELECT * FROM faults WHERE id=?", (fid,)).fetchone()
    if f is None:
        return None
    box = conn.execute("SELECT * FROM lightboxes WHERE id=?", (f["lightbox_id"],)).fetchone()
    advertisers = _j(conn.execute(
        """SELECT a.id, a.name, a.contact, fa.lightbox_id, lb.code box_code
           FROM fault_advertisers fa
           JOIN advertisers a ON a.id=fa.advertiser_id
           JOIN lightboxes lb ON lb.id=fa.lightbox_id
           WHERE fa.fault_id=? ORDER BY lb.code""", (fid,)).fetchall())
    wos = _j(conn.execute(
        """SELECT wo.*, (SELECT status FROM faults WHERE id=wo.fault_id) fault_status_now
           FROM work_orders wo WHERE fault_id=? ORDER BY wo.id""", (fid,)).fetchall())
    for w in wos:
        w["created_label"] = clock.fmt(w["created_min"])
        w["eta_label"] = clock.fmt(w["eta_min"])
        w["done_label"] = clock.fmt(w["done_min"])
    inspections = _j(conn.execute(
        """SELECT i.*, p.name inspector_name FROM inspections i
           JOIN inspectors p ON p.id=i.inspector_id
           WHERE i.fault_id=?
              OR i.lightbox_id=?
           ORDER BY i.ts_min DESC LIMIT 20""",
        (fid, f["lightbox_id"])).fetchall())
    for i in inspections:
        i["ts_label"] = clock.fmt(i["ts_min"])
    schedules = _j(conn.execute(
        "SELECT * FROM schedules WHERE lightbox_id=?", (f["lightbox_id"],)).fetchall())
    breakdown = svc.blackout_breakdown(conn, fid)
    for seg in breakdown["planned_segments"]:
        seg["start_label"] = clock.fmt(seg["start"])
        seg["end_label"] = clock.fmt(seg["end"])
    breakdown["window_start_label"] = clock.fmt(breakdown["window_start"])
    breakdown["window_end_label"] = clock.fmt(breakdown["window_end"])
    credits = _j(conn.execute(
        """SELECT mc.*, a.name advertiser_name, lb.code box_code,
                  c.artwork_name, c.preview_color
           FROM makeup_credits mc
           JOIN advertisers a ON a.id=mc.advertiser_id
           JOIN lightboxes lb ON lb.id=mc.lightbox_id
           JOIN contracts c ON c.id=mc.contract_id
           WHERE mc.fault_id=? ORDER BY mc.id""", (fid,)).fetchall())
    reverifs = _j(conn.execute(
        """SELECT r.*, p.name inspector_name FROM reverifications r
           JOIN inspectors p ON p.id=r.inspector_id
           WHERE fault_id=? ORDER BY id""", (fid,)).fetchall())
    for r in reverifs:
        r["ts_label"] = clock.fmt(r["ts_min"])
    reporter = svc.fault_reporter(conn, fid)
    reporter_name = None
    if reporter:
        row = conn.execute("SELECT name FROM inspectors WHERE id=?",
                           (reporter["inspector_id"],)).fetchone()
        reporter_name = row["name"] if row else None
    return {
        "fault": {
            **dict(f),
            "kind_text": FAULT_KINDS.get(f["kind"], f["kind"]),
            "status_text": FAULT_STATUS.get(f["status"], f["status"]),
            "start_label": clock.fmt(f["start_min"]),
            "end_label": clock.fmt(f["end_min"]),
            "detected_label": clock.fmt(f["detected_min"]),
            "closed_label": clock.fmt(f["closed_min"]),
            "reporter_name": reporter_name,
        },
        "box": serialize_box(conn, box),
        "advertisers": advertisers,
        "work_orders": wos,
        "inspections": inspections,
        "schedules": schedules,
        "schedule_text": describe_schedules(schedules),
        "breakdown": breakdown,
        "credits": credits,
        "reverifications": reverifs,
    }


# ------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    state: AppState = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._send({"error": msg}, status)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _serve_file(self, path: Path, content_type=None):
        if not path.is_file():
            self.send_error(404)
            return
        ctype = content_type or (mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        try:
            if p == "/api/overview":
                conn = self.state.conn()
                try:
                    return self._send({"boxes": overview(conn), "now": clock.now_min(),
                                       "now_label": clock.fmt(clock.now_min())})
                finally:
                    conn.close()
            if p == "/api/inspectors":
                conn = self.state.conn()
                try:
                    return self._send({"inspectors": _j(conn.execute(
                        "SELECT * FROM inspectors ORDER BY id").fetchall())})
                finally:
                    conn.close()
            if p.startswith("/api/signal/"):
                lb_id = int(p.rsplit("/", 1)[1])
                conn = self.state.conn()
                try:
                    from urllib.parse import parse_qs
                    q = parse_qs(url.query)
                    t = clock.parse_local(q["at"][0]) if q.get("at") else clock.now_min()
                    sig = self.state.adapter(conn).read(lb_id, at_min=t)
                    return self._send({"brightness": sig.brightness,
                                       "lit_bars": sig.lit_bars,
                                       "total_bars": sig.total_bars,
                                       "source": sig.source})
                finally:
                    conn.close()
            if p == "/api/faults":
                conn = self.state.conn()
                try:
                    rows = _j(conn.execute(
                        """SELECT f.*, lb.code box_code FROM faults f
                           JOIN lightboxes lb ON lb.id=f.lightbox_id
                           ORDER BY CASE f.status WHEN 'open' THEN 0 WHEN 'rejected' THEN 1
                              WHEN 'repaired' THEN 2 ELSE 3 END, f.id DESC""").fetchall())
                    for r in rows:
                        r["kind_text"] = FAULT_KINDS.get(r["kind"], r["kind"])
                        r["status_text"] = FAULT_STATUS.get(r["status"], r["status"])
                        r["start_label"] = clock.fmt(r["start_min"])
                        r["detected_label"] = clock.fmt(r["detected_min"])
                    return self._send({"faults": rows})
                finally:
                    conn.close()
            if p.startswith("/api/faults/"):
                fid = int(p.rsplit("/", 1)[1])
                conn = self.state.conn()
                try:
                    d = fault_detail(conn, fid)
                    if d is None:
                        return self._err("故障单不存在", 404)
                    return self._send(d)
                finally:
                    conn.close()
            if p.startswith("/photos/"):
                rel = p[len("/photos/"):]
                base = self.state.photo_root.resolve()
                target = (base / rel).resolve()
                if not str(target).startswith(str(base)):
                    return self.send_error(403)
                return self._serve_file(target, "image/svg+xml")
            if p == "/" or p == "/index.html":
                return self._serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            if p.startswith("/static/"):
                return self._serve_file(STATIC / p[len("/static/"):])
            self.send_error(404)
        except Exception as e:  # noqa: BLE001
            return self._err(f"服务器错误: {e}", 500)

    def do_POST(self):
        url = urlparse(self.path)
        p = url.path
        data = self._read_json()
        conn = self.state.conn()
        try:
            return self._route_post(conn, p, data)
        except svc.PermissionDenied as e:
            return self._err(f"权限不足：{e}", 403)
        except svc.DomainError as e:
            return self._err(str(e), 400)
        except Exception as e:  # noqa: BLE001
            return self._err(f"服务器错误: {e}", 500)
        finally:
            conn.close()

    def _route_post(self, conn, p, data):
        adapter = self.state.adapter(conn)

        def ts_of(d):
            if d.get("ts"):
                return clock.parse_local(d["ts"])
            return clock.now_min()

        if p == "/api/inspect":
            lb_id = int(data["lightbox_id"])
            t = ts_of(data)
            sig = adapter.read(lb_id, at_min=t)
            ov = conn.execute(
                "SELECT set_min FROM signal_overrides WHERE lightbox_id=?", (lb_id,)
            ).fetchone()
            insp, fault, kind = svc.inspect(
                conn, lightbox_id=lb_id, inspector_id=int(data["inspector_id"]),
                ts_min=t, brightness=sig.brightness, lit_bars=sig.lit_bars,
                total_bars=sig.total_bars, note=data.get("note", ""),
                adapter=adapter, photo_store=self.state.photo_store,
                signal_set_min=ov["set_min"] if ov else None)
            return self._send({"ok": True, "kind": kind,
                               "kind_text": KIND_TEXT.get(kind, kind),
                               "inspection_id": insp["id"],
                               "fault_id": fault["id"] if fault else None,
                               "photo_path": insp["photo_path"]})

        if p.startswith("/api/workorders") and p.endswith("/complete"):
            wo_id = int(p.split("/")[-2])
            wo = svc.complete_repair(conn, work_order_id=wo_id, done_min=ts_of(data),
                                     note=data.get("note", ""), adapter=adapter)
            return self._send({"ok": True, "work_order": dict(wo)})

        if p == "/api/workorders":
            wo = svc.dispatch_work_order(
                conn, fault_id=int(data["fault_id"]), assignee=data["assignee"],
                created_min=ts_of(data),
                eta_min=clock.parse_local(data["eta"]) if data.get("eta") else None,
                note=data.get("note", ""))
            return self._send({"ok": True, "work_order": dict(wo)})

        if p.startswith("/api/faults/") and p.endswith("/reverify"):
            fid = int(p.split("/")[-2])
            res = svc.reverify(conn, fault_id=fid,
                               inspector_id=int(data["inspector_id"]),
                               ts_min=ts_of(data), adapter=adapter,
                               note=data.get("note", ""))
            return self._send({"ok": True, **res})

        if p.startswith("/api/makeup/") and p.endswith("/approve"):
            fid = int(p.split("/")[-2])
            rows = svc.approve_makeup(conn, fault_id=fid,
                                      inspector_id=int(data["inspector_id"]),
                                      ts_min=ts_of(data),
                                      approve=bool(data.get("approve", True)))
            return self._send({"ok": True, "credits": _j(rows)})

        if p == "/api/signals":
            lb_id = int(data["lightbox_id"])
            if data.get("clear"):
                adapter.clear_override(lb_id)
            else:
                adapter.set_override(lb_id, float(data["brightness"]),
                                     int(data["lit_bars"]),
                                     set_min=clock.parse_local(data["set_at"])
                                     if data.get("set_at") else None,
                                     note=data.get("note", ""))
            return self._send({"ok": True})

        return self._err("未知接口", 404)


def make_server(host: str, port: int, state: AppState):
    init_db(connect(state.db_path))
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
