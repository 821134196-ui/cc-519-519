"""SQLite 存储层：建表与连接。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 人员与角色: inspector 巡查员 / repair 维修派单 / supervisor 审批
CREATE TABLE IF NOT EXISTS users (
    login    TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    role     TEXT NOT NULL CHECK (role IN ('inspector','repair','supervisor'))
);

-- 供电回路
CREATE TABLE IF NOT EXISTS circuits (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'normal' CHECK (status IN ('normal','interrupted'))
);

-- 灯箱点位
CREATE TABLE IF NOT EXISTS boxes (
    id               TEXT PRIMARY KEY,          -- 灯箱编号 B001
    name             TEXT NOT NULL,
    addr             TEXT NOT NULL,
    lat              REAL NOT NULL,
    lng              REAL NOT NULL,
    circuit_id       TEXT NOT NULL REFERENCES circuits(id),
    strips_total     INTEGER NOT NULL DEFAULT 8,
    creative_hex     TEXT NOT NULL,             -- 当前广告画面预览色
    -- 模拟适配器模式: auto 跟随时段计划 / on 强亮(定时器错) / off 强黑 / half 半亮
    sim_mode         TEXT NOT NULL DEFAULT 'auto'
);

-- 亮灯计划(按星期, 起止 HH:MM, 可跨午夜; 支持版本区间)
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id     TEXT NOT NULL REFERENCES boxes(id),
    days       TEXT NOT NULL,          -- 例如 '1,2,3,4,5,6,7' (周一=1)
    start_hm   TEXT NOT NULL,          -- '18:00'
    end_hm     TEXT NOT NULL,          -- '23:00'; end<=start 视为跨午夜
    valid_from TEXT NOT NULL,          -- ISO 日期
    valid_to   TEXT                     -- NULL 表示当前版本
);

-- 广告合同(按版本计补播)
CREATE TABLE IF NOT EXISTS contracts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id       TEXT NOT NULL REFERENCES boxes(id),
    advertiser   TEXT NOT NULL,
    version      TEXT NOT NULL,
    valid_from   TEXT NOT NULL,        -- ISO 含时区
    valid_to     TEXT,                  -- NULL 为现行
    comp_factor  REAL NOT NULL DEFAULT 1.0   -- 补播权重(版本合同单价系数)
);

-- 模拟适配器读取的亮灯信号(0~1 分数, 1=全亮)
CREATE TABLE IF NOT EXISTS signal_readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id    TEXT NOT NULL REFERENCES boxes(id),
    ts        TEXT NOT NULL,
    fraction  REAL NOT NULL CHECK (fraction BETWEEN 0 AND 1),
    source    TEXT NOT NULL             -- poll / inspect / repair / verify
);
CREATE INDEX IF NOT EXISTS idx_readings_box_ts ON signal_readings(box_id, ts);

-- 巡检记录(含照片)
CREATE TABLE IF NOT EXISTS inspections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id           TEXT NOT NULL REFERENCES boxes(id),
    ts               TEXT NOT NULL,
    inspector        TEXT NOT NULL REFERENCES users(login),
    within_schedule  INTEGER NOT NULL,
    fraction         REAL NOT NULL,
    strips_on        INTEGER NOT NULL,
    creative_match   INTEGER NOT NULL,          -- 现场画面是否与登记合同一致
    classification   TEXT NOT NULL,
    fault_id         INTEGER REFERENCES faults(id),
    photo_path       TEXT,
    note             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_insp_box_ts ON inspections(box_id, ts);

-- 故障单(circuit_id 非空 => 同回路多点位归并)
CREATE TABLE IF NOT EXISTS faults (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id           TEXT NOT NULL,             -- 首报点位
    circuit_id       TEXT REFERENCES circuits(id),
    kind             TEXT NOT NULL CHECK (kind IN
                         ('outage','partial','timer','relapse')),
    status           TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','resolved','closed_nofault')),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    resolved_at      TEXT,
    blackout_start   TEXT,                      -- 本轮黑屏起点(复验点亮则清空)
    expected_restore TEXT,
    close_reason     TEXT NOT NULL DEFAULT ''   -- daytime/creative 等非故障关闭说明
);
CREATE INDEX IF NOT EXISTS idx_faults_status ON faults(status);

-- 回路级故障受影响点位
CREATE TABLE IF NOT EXISTS fault_boxes (
    fault_id INTEGER NOT NULL REFERENCES faults(id),
    box_id   TEXT NOT NULL REFERENCES boxes(id),
    PRIMARY KEY (fault_id, box_id)
);

-- 维修单: 一个灯箱同时只允许一张未关闭维修单
CREATE TABLE IF NOT EXISTS repair_tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fault_id    INTEGER NOT NULL REFERENCES faults(id),
    box_id      TEXT NOT NULL REFERENCES boxes(id),
    status      TEXT NOT NULL DEFAULT 'dispatched'
                  CHECK (status IN ('dispatched','repaired','verified',
                                    'reopened','closed')),
    created_at  TEXT NOT NULL,
    assignee    TEXT REFERENCES users(login),
    expected_restore TEXT,
    repaired_at TEXT,
    repair_by   TEXT REFERENCES users(login),
    verified_at TEXT,
    verify_by   TEXT REFERENCES users(login),
    closed_at   TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
-- 未关闭工单唯一: 杜绝同一灯箱平行两张相同维修单
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_ticket_per_box
    ON repair_tickets(box_id)
    WHERE status IN ('dispatched','repaired','reopened');

CREATE TABLE IF NOT EXISTS ticket_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES repair_tickets(id),
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);

-- 黑屏时段(一次故障可有多轮: 修复后再次熄灭 => 新一段; deficit 1=全黑 0.5=半亮)
CREATE TABLE IF NOT EXISTS blackout_segments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fault_id  INTEGER NOT NULL REFERENCES faults(id),
    box_id    TEXT NOT NULL REFERENCES boxes(id),
    start_ts  TEXT NOT NULL,
    end_ts    TEXT NOT NULL,
    deficit   REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_segments_fault_box ON blackout_segments(fault_id, box_id);

-- 补播结论: 巡查员提出, 他人审批
CREATE TABLE IF NOT EXISTS makeup_conclusions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fault_id     INTEGER NOT NULL REFERENCES faults(id),
    proposed_by  TEXT NOT NULL REFERENCES users(login),
    proposed_at  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','rejected')),
    decided_by   TEXT REFERENCES users(login),
    decided_at   TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    breakdown    TEXT NOT NULL DEFAULT '[]'  -- 按合同版本 JSON
);

-- 供 seed/测试重置
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
