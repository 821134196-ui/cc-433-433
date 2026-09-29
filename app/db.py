"""SQLite 连接与建表。

关键约束：
- appointments.escort_id 在抢单成功时通过条件 UPDATE 保证同一时段只有一人接单；
- handoffs / timeline_events 只追加，不修改；
- 已完成（completed/cancelled/refused）的预约在业务层禁止重新开启。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()


def get_conn(db_path=None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    anon_code TEXT NOT NULL UNIQUE,           -- 对外展示的匿名编号，如 P-1001
    demo_name TEXT NOT NULL,                  -- 演示姓名（明确标注为演示数据）
    demo_id_masked TEXT NOT NULL,             -- 演示证件（脱敏）
    gender TEXT NOT NULL DEFAULT '',
    age INTEGER,
    contact_masked TEXT NOT NULL DEFAULT '',  -- 演示联系方式（脱敏）
    note TEXT NOT NULL DEFAULT ''             -- 患者端不可见的内部备注
);

CREATE TABLE IF NOT EXISTS escorts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone_masked TEXT NOT NULL DEFAULT '',
    rating_avg REAL NOT NULL DEFAULT 5.0,
    rating_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,                -- 预约号 A-20260929-001
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    department TEXT NOT NULL,                -- 检查科室
    exam_item TEXT NOT NULL DEFAULT '',       -- 检查项目
    scheduled_at TEXT NOT NULL,              -- ISO 本地时间
    end_at TEXT NOT NULL,
    mobility TEXT NOT NULL DEFAULT 'none',
    -- 授权范围（JSON 数组），接单前陪诊员只能看到摘要
    authorization_scope TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    -- open(待接单) assigned(已接单/进行中) completed cancelled refused
    escort_id INTEGER REFERENCES escorts(id),
    accepted_at TEXT,
    completed_at TEXT,
    internal_note TEXT NOT NULL DEFAULT '',   -- 内部备注：患者端隐藏
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appt_status_time ON appointments(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_appt_escort ON appointments(escort_id);

-- 陪诊员对预约的“可见授权”：接单成功后逐条落入必要字段，接单前查不到敏感行
CREATE TABLE IF NOT EXISTS escort_grants (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    escort_id INTEGER NOT NULL REFERENCES escorts(id),
    granted_fields TEXT NOT NULL DEFAULT '[]', -- 已授权可见字段
    granted_at TEXT NOT NULL,
    UNIQUE(appointment_id, escort_id)
);

CREATE TABLE IF NOT EXISTS timeline_events (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    stage TEXT NOT NULL,                      -- arrived/waiting/exam_done/departed
    location TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    actor_escort_id INTEGER REFERENCES escorts(id),
    created_at TEXT NOT NULL,
    UNIQUE(appointment_id, stage)             -- 每阶段只能签到一次
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    from_party TEXT NOT NULL,                 -- 交接方（岗位/匿名身份）
    to_party TEXT NOT NULL,                   -- 接收方
    location TEXT NOT NULL DEFAULT '',
    materials TEXT NOT NULL DEFAULT '[]',     -- 交接物料 JSON
    note TEXT NOT NULL DEFAULT '',
    actor_escort_id INTEGER REFERENCES escorts(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoff_appt ON handoffs(appointment_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    filename TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'normal', -- normal / sensitive（检查结果等）
    requires_scope TEXT NOT NULL DEFAULT '',    -- 查看所需授权项
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_exceptions (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    old_time TEXT, new_time TEXT,
    old_route TEXT, new_route TEXT,
    late_minutes INTEGER,
    requires_manual INTEGER NOT NULL DEFAULT 0,  -- 是否超授权，需人工确认
    manual_status TEXT NOT NULL DEFAULT 'none',  -- none/pending/approved/rejected
    manual_decision_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_reviews (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    reason TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    escort_id INTEGER NOT NULL REFERENCES escorts(id),
    stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    tags TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 进程内模拟通知的持久化留痕（同时保留重试次数，便于测试重试逻辑）
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER REFERENCES appointments(id),
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/sent/failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""


def init_db(db_path=None):
    with _lock:
        conn = get_conn(db_path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
