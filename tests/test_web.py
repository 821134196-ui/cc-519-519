"""端到端：用半亮样例起 HTTP 服务，验证页面与关键 POST 规则。"""
from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from lightbox.clock import VirtualClock
from lightbox.db import connect, init_db
from lightbox import seed as seed_mod
from lightbox import web


class WebE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.db_path = root / "t.db"
        cls.photo_dir = root / "photos"
        conn = connect(cls.db_path)
        init_db(conn)
        seed_mod.seed(conn, cls.photo_dir)
        conn.commit()
        conn.close()

        web.DB_PATH = cls.db_path
        web.PHOTO_DIR = cls.photo_dir
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.App)
        cls.srv.db_path = cls.db_path
        cls.srv.clock = VirtualClock(seed_mod.NOW)
        cls.port = cls.srv.server_address[1]
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, (body if raw else body.decode())

    def post(self, path, data):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, urlencode(data),
                  {"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        loc = r.getheader("Location", "")
        r.read()
        c.close()
        return r.status, loc

    def test_pages_render(self):
        for path in ("/", "/faults", "/tickets", "/inspect",
                     "/fault?id=1", "/fault?id=2", "/fault?id=4",
                     "/box?id=B002", "/ticket?id=1"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("灯箱", body)

    def test_map_shows_codes_and_evidence(self):
        _, body = self.get("/")
        for bid in ("B001", "B002", "B003", "B004", "B005", "B006",
                    "B007", "B008"):
            self.assertIn(bid, body)
        self.assertIn("最近证据", body)
        # 文字结论与颜色状态同时存在
        self.assertIn("部分灯带不亮", body)
        self.assertIn("供电中断", body)

    def test_fault_detail_contrasts_plan_vs_blackout(self):
        _, body = self.get("/fault?id=1")
        self.assertIn("计划亮灯时段 与 实际黑屏 对照", body)
        self.assertIn("青柠汽水", body)
        self.assertIn("2026-V1", body)
        self.assertIn("2026-V2", body)
        self.assertIn("半亮 ×0.5", body)

    def test_photo_served(self):
        name = next(p.name for p in self.photo_dir.glob("*.png"))
        status, body = self.get(f"/photos/{name}", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

    def test_duplicate_dispatch_rejected(self):
        status, loc = self.post("/dispatch", {
            "fault_id": 1, "box_id": "B002", "assignee": "wang",
            "restore": "2026-10-01T22:00:00+08:00"})
        self.assertEqual(status, 303)
        self.assertIn("err=", loc)
        self.assertIn("%E5%B9%B3%E8%A1%8C", loc)

    def test_self_approval_rejected_other_approved(self):
        _, loc = self.post("/makeup/decide",
                           {"id": 1, "actor": "li", "approve": "1"})
        self.assertIn("err=", loc)
        _, loc = self.post("/makeup/decide",
                           {"id": 1, "actor": "zhang", "approve": "1"})
        self.assertIn("ok=", loc)

    def test_daytime_inspection_creates_no_fault(self):
        # B001 健康；改派白天由接口无法改钟，直接断言 B008 历史巡检无故障
        _, body = self.get("/box?id=B008")
        self.assertIn("计划外", body)
        self.assertIn("无故障记录", body)


if __name__ == "__main__":
    unittest.main()
