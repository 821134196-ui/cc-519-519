"""一条命令：预置/重置"半亮"样例并启动监管台。

用法:
    python3 -m lightbox.run            # 已有样例则保留，直接启动
    python3 -m lightbox.run --reset    # 重置为半亮样例再启动
    python3 -m lightbox.run --port 8080
"""
from __future__ import annotations

import argparse

from .db import connect, init_db
from . import seed as seed_mod
from . import web


def main():
    ap = argparse.ArgumentParser(description="灯箱亮灯监管台（含半亮样例）")
    ap.add_argument("--reset", action="store_true", help="重置为半亮样例")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    conn = connect(web.DB_PATH)
    init_db(conn)
    seed_mod.seed(conn, web.PHOTO_DIR, reset=args.reset)
    conn.commit()
    conn.close()

    from http.server import ThreadingHTTPServer
    from .clock import VirtualClock

    httpd = ThreadingHTTPServer((args.host, args.port), web.App)
    httpd.db_path = web.DB_PATH
    httpd.clock = VirtualClock(seed_mod.NOW)
    print("半亮样例已就绪：B002 半数灯带不亮、B003/B004 同回路中断归并、"
          "B005 定时器错、B006 修复后再灭、B007 画面撤换、B008 白天误判。")
    print(f"监管台：http://{args.host}:{args.port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
