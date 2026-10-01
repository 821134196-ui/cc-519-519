#!/usr/bin/env bash
# 一条本地命令：初始化 SQLite、预置半亮样例并启动界面。
# 用法：./run.sh [--reseed] [--port 8000]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m lightbox.serve "$@"
