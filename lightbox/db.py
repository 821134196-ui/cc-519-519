"""SQLite 存储层：schema 建库、连接、领域常量。

故障类别 fault_kind：
  daytime_false_positive 白天误判（计划时段外，不立故障单）
  timer_misconfig         定时器配置错（该亮不亮，计划覆盖内）
  strip_partial           部分灯带不亮（0<亮度<阈值）
  power_outage            供电中断（同回路多箱齐黑，归并为一张回路故障）
  artwork_removed         画面已撤换（合同空档，不计补播）
  re_outage               维修后再次熄灭（同一故障单复验失败后复发）

工作单状态：open（未关闭）/ repaired（待复验）/ verified_closed（复验关闭）/
            rejected（复验不通过，退回维修）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 广告主
CREATE TABLE IF NOT EXISTS advertisers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    contact     TEXT
);

-- 灯箱点位
CREATE TABLE IF NOT EXISTS lightboxes (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,          -- 灯箱编号，如 LB-A01
    name         TEXT NOT NULL,
    address      TEXT,
    map_x        REAL NOT NULL,                 -- 地图坐标 0..1
    map_y        REAL NOT NULL,
    circuit_id   INTEGER REFERENCES circuits(id),
    created_at   INTEGER NOT NULL               -- epoch 分钟
);

-- 供电回路
CREATE TABLE IF NOT EXISTS circuits (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    note   TEXT
);

-- 亮灯计划（按周历，可多条）
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY,
    lightbox_id INTEGER NOT NULL REFERENCES lightboxes(id) ON DELETE CASCADE,
    weekday     INTEGER NOT NULL,               -- 1=周一 … 7=周日
    start_hm    TEXT NOT NULL,                   -- HH:MM
    end_hm      TEXT NOT NULL,                   -- HH:MM，<=start 表示跨午夜
    UNIQUE(lightbox_id, weekday, start_hm, end_hm)
);

-- 合同版本：同一灯箱上的画面合同按时间切片，补播严格按黑屏发生时的版本计算
CREATE TABLE IF NOT EXISTS contracts (
    id             INTEGER PRIMARY KEY,
    lightbox_id    INTEGER NOT NULL REFERENCES lightboxes(id) ON DELETE CASCADE,
    advertiser_id  INTEGER NOT NULL REFERENCES advertisers(id),
    artwork_name   TEXT NOT NULL,               -- 画面名称
    preview_color  TEXT NOT NULL DEFAULT '#c98a3d',  -- 广告画面预览色
    start_min      INTEGER NOT NULL,            -- 合同生效（epoch 分钟，含）
    end_min        INTEGER NOT NULL,            -- 合同结束（含）；空档期视为画面撤换
    rate_per_min   REAL NOT NULL DEFAULT 0      -- 补播单价（元/分钟，仅用于展示核算金额）
);

-- 巡检员
CREATE TABLE IF NOT EXISTS inspectors (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

-- 巡检记录（含本地照片）
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY,
    lightbox_id   INTEGER NOT NULL REFERENCES lightboxes(id),
    inspector_id  INTEGER NOT NULL REFERENCES inspectors(id),
    ts_min        INTEGER NOT NULL,             -- 拍照时刻（epoch 分钟）
    brightness    REAL NOT NULL,                -- 0..1 实测亮度（模拟适配器给出）
    lit_bars      INTEGER NOT NULL,             -- 点亮灯带条数
    total_bars    INTEGER NOT NULL,
    note          TEXT DEFAULT '',
    photo_path    TEXT,                         -- 本地照片相对路径
    fault_id      INTEGER REFERENCES faults(id) -- 判定后关联的故障（白天误判为空）
);

-- 故障单
CREATE TABLE IF NOT EXISTS faults (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open', -- open/repaired/rejected/verified_closed
    lightbox_id   INTEGER NOT NULL REFERENCES lightboxes(id),
    circuit_id    INTEGER REFERENCES circuits(id),  -- power_outage 时为受影响回路
    start_min     INTEGER NOT NULL,             -- 黑屏起（钳制在计划/合同内）
    end_min       INTEGER,                      -- 实际恢复时刻（复验确认前为空）
    detected_min  INTEGER NOT NULL,             -- 巡查发现时刻
    closed_min    INTEGER,                      -- 复验关闭时刻
    cause_note    TEXT DEFAULT '',
    merged_into   INTEGER REFERENCES faults(id)  -- 归并目标（回路归并时的从单）
);
CREATE INDEX IF NOT EXISTS idx_faults_lb ON faults(lightbox_id, status);
CREATE INDEX IF NOT EXISTS idx_faults_circuit ON faults(circuit_id, status);

-- 故障影响的广告主（故障单关联受影响广告主；回路单含回路内全部在档合同）
CREATE TABLE IF NOT EXISTS fault_advertisers (
    fault_id       INTEGER NOT NULL REFERENCES faults(id) ON DELETE CASCADE,
    advertiser_id  INTEGER NOT NULL REFERENCES advertisers(id),
    lightbox_id    INTEGER NOT NULL REFERENCES lightboxes(id),
    PRIMARY KEY(fault_id, advertiser_id, lightbox_id)
);

-- 维修派工单（一个故障单同一时刻只允许一张未关闭维修单；复验不通过后可再派）
CREATE TABLE IF NOT EXISTS work_orders (
    id               INTEGER PRIMARY KEY,
    fault_id         INTEGER NOT NULL REFERENCES faults(id),
    assignee        TEXT NOT NULL,              -- 维修人员
    created_min      INTEGER NOT NULL,
    eta_min          INTEGER,                   -- 预计恢复时间
    status           TEXT NOT NULL DEFAULT 'open',  -- open/done/closed
    done_min         INTEGER,
    repair_note      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wo_fault ON work_orders(fault_id, status);

-- 复验记录（复验人不得是最初上报该故障的巡查员）
CREATE TABLE IF NOT EXISTS reverifications (
    id            INTEGER PRIMARY KEY,
    fault_id      INTEGER NOT NULL REFERENCES faults(id),
    inspector_id  INTEGER NOT NULL REFERENCES inspectors(id),
    ts_min        INTEGER NOT NULL,
    passed        INTEGER NOT NULL,             -- 1 通过并关闭 / 0 不通过（再次熄灭）
    brightness    REAL NOT NULL,
    note          TEXT DEFAULT ''
);

-- 补播核算明细：按 (故障, 广告主, 灯箱, 合同版本) 切片
CREATE TABLE IF NOT EXISTS makeup_credits (
    id             INTEGER PRIMARY KEY,
    fault_id       INTEGER NOT NULL REFERENCES faults(id),
    contract_id    INTEGER NOT NULL REFERENCES contracts(id),
    advertiser_id  INTEGER NOT NULL REFERENCES advertisers(id),
    lightbox_id    INTEGER NOT NULL REFERENCES lightboxes(id),
    owed_minutes   INTEGER NOT NULL DEFAULT 0,  -- 应补分钟（已按缺额比例折算）
    amount         REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'proposed', -- proposed/approved/rejected
    proposed_by    INTEGER NOT NULL REFERENCES inspectors(id),
    approved_by    INTEGER REFERENCES inspectors(id),
    approved_min   INTEGER,
    note           TEXT DEFAULT '',
    UNIQUE(fault_id, contract_id)
);

-- 信号适配器的模拟覆盖（灯箱 -> {brightness, bars, ts起}），见 signals.py
CREATE TABLE IF NOT EXISTS signal_overrides (
    lightbox_id  INTEGER PRIMARY KEY REFERENCES lightboxes(id) ON DELETE CASCADE,
    brightness   REAL NOT NULL,
    lit_bars     INTEGER NOT NULL,
    set_min      INTEGER NOT NULL,
    note         TEXT DEFAULT ''
);
"""

FAULT_KINDS = {
    "daytime_false_positive": "白天误判",
    "timer_misconfig": "定时器配置错",
    "strip_partial": "部分灯带不亮",
    "power_outage": "供电中断",
    "artwork_removed": "画面已撤换",
    "re_outage": "维修后再次熄灭",
}

FAULT_STATUS = {
    "open": "未关闭",
    "repaired": "维修完成待复验",
    "rejected": "复验不通过-退回维修",
    "verified_closed": "已复验关闭",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
