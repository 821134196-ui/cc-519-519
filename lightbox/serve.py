"""启动入口：python3 -m lightbox.serve [--reseed] [--port 8000]。

首次启动（数据库不存在）自动预置含“半亮样例”的演示数据；
--reseed 可强制重置后重新预置。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import seed as seed_mod
from .app import AppState, make_server


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="灯箱运营台：一条命令启动并预置样例")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/lightbox.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reseed", action="store_true", help="清空并重新预置样例数据")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if args.reseed or not db_path.exists():
        print("预置样例数据（含 LB-A01 半亮、回路供电中断、定时器故障、维修后再熄灭等）…")
        seed_mod.seed(args.db, args.photos, reset=args.reseed or not db_path.exists())

    state = AppState(args.db, args.photos)
    httpd = make_server(args.host, args.port, state)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 56)
    print("灯箱运营台已启动")
    print(f"  界面：{url}")
    print("  样例账号（巡查员）：张巡 / 李巡 / 王巡")
    print("  停止：Ctrl+C")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
