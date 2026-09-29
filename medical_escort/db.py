"""SQLite 连接管理与建表脚本。

所有写操作走短事务；接单使用 BEGIN IMMEDIATE 行级更新，保证同一时段并发
接单只有一人成功（SQLite 单写锁 + UPDATE...WHERE status='scheduled'）。
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 患者匿名档案：对外只展示脱敏姓名；证件号属于敏感字段，最小权限读取
CREATE TABLE IF NOT EXISTS patients (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- 匿名编号 P-xxxx
    display_name    TEXT NOT NULL,                 -- 脱敏展示名，如 王*
    full_name       TEXT NOT NULL,                 -- 真实姓名（敏感，仅调度台可见）
    id_document     TEXT NOT NULL,                 -- 证件号（敏感，最小权限）
    gender          TEXT NOT NULL,
    age             INTEGER NOT NULL,
    contact_phone   TEXT NOT NULL,
    mobility_note   TEXT NOT NULL DEFAULT ''       -- 行动能力，用于路线规划
);

CREATE TABLE IF NOT EXISTS escorts (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- E-xxxx
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' -- active / paused
);

-- 检查预约
CREATE TABLE IF NOT EXISTS appointments (
    id                INTEGER PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,        -- A-xxxx
    patient_id        INTEGER NOT NULL REFERENCES patients(id),
    department        TEXT NOT NULL,               -- 检查科室
    exam_item         TEXT NOT NULL,               -- 检查项目
    scheduled_start   TEXT NOT NULL,               -- ISO 本地时间
    scheduled_end     TEXT NOT NULL,
    mobility_needs    TEXT NOT NULL DEFAULT '',    -- 行动辅助要求（轮椅/平车/陪同步行…）
    special_requirements TEXT NOT NULL DEFAULT '', -- 检查前注意（空腹/憋尿/停药…）
    internal_note     TEXT NOT NULL DEFAULT '',    -- 内部备注：患者端永不展示
    status            TEXT NOT NULL DEFAULT 'scheduled',
        -- scheduled 已发布待接单
        -- claimed   已接单
        -- enroute   陪诊员已出发（可选）
        -- arrived   已到达签到
        -- waiting   候诊
        -- in_exam   检查中
        -- completed 检查完成
        -- discharged 已离院（服务终态）
        -- cancelled 已取消（终态，含患者拒绝陪同）
        -- rejected  患者拒绝陪同（终态，保留独立语义）
        -- rescheduling 临时改期（中间态，改期确认后回到 scheduled）
    escort_id         INTEGER REFERENCES escorts(id),
    claimed_at        TEXT,
    cancel_reason     TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    version           INTEGER NOT NULL DEFAULT 0   -- 乐观版本/改期次数留痕
);

CREATE INDEX IF NOT EXISTS idx_appt_status_time ON appointments(status, scheduled_start);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id);
CREATE INDEX IF NOT EXISTS idx_appt_escort ON appointments(escort_id);

-- 服务时段（陪诊员可接单窗口）
CREATE TABLE IF NOT EXISTS service_slots (
    id           INTEGER PRIMARY KEY,
    escort_id    INTEGER NOT NULL REFERENCES escorts(id),
    slot_start   TEXT NOT NULL,
    slot_end     TEXT NOT NULL,
    UNIQUE(escort_id, slot_start, slot_end)
);

-- 授权事项（按预约逐条授权）
CREATE TABLE IF NOT EXISTS consents (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    scope_code      TEXT NOT NULL,
        -- pickup 接送到院 / queue 代取号排队 / accompany_check 陪同检查
        -- med_report 领取检查结果 / payment 代缴费用 / medication 代取药
    label           TEXT NOT NULL,
    granted         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- 检查时间线（到达/候诊/检查完成/离院逐步签到）
CREATE TABLE IF NOT EXISTS checkins (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    stage           TEXT NOT NULL, -- arrived / waiting / in_exam / completed / discharged
    note            TEXT NOT NULL DEFAULT '',
    actor_id        INTEGER REFERENCES escorts(id),
    location        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- 交接记录：对象、时间、地点必须留痕
CREATE TABLE IF NOT EXISTS handoffs (
    id                INTEGER PRIMARY KEY,
    appointment_id    INTEGER NOT NULL REFERENCES appointments(id),
    seq               INTEGER NOT NULL,
    from_party        TEXT NOT NULL,   -- 调度台 / 陪诊员-李 / 检查科护士
    to_party          TEXT NOT NULL,
    handoff_time      TEXT NOT NULL,
    location          TEXT NOT NULL,
    items             TEXT NOT NULL DEFAULT '', -- 交接物品/资料清单
    patient_confirmed INTEGER NOT NULL DEFAULT 0,
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

-- 异常事件：临时改期 / 检查取消 / 拒绝陪同 / 路线变更 / 陪诊员迟到 / 资料缺失
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER REFERENCES appointments(id),
    kind            TEXT NOT NULL,
        -- reschedule / exam_cancelled / patient_refused / route_change
        -- escort_late / material_missing
    detail          TEXT NOT NULL DEFAULT '',
    old_value       TEXT NOT NULL DEFAULT '',
    new_value       TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- 院内路线（版本化：路线变更保留历史版本）
CREATE TABLE IF NOT EXISTS routes (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    version         INTEGER NOT NULL,
    steps_json      TEXT NOT NULL,  -- [{"seq":1,"place":"门诊大厅","action":"…","est_min":5}]
    reason          TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- 敏感附件（检查结果、证件影像等），最小权限展示：仅记录元数据+文件路径+可见角色
CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointments(id),
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    category        TEXT NOT NULL, -- id_doc / exam_result / consent_form / other
    sensitive       INTEGER NOT NULL DEFAULT 1,
    visibility      TEXT NOT NULL DEFAULT 'dispatch', -- dispatch / escort / patient / all
    uploaded_at     TEXT NOT NULL
);

-- 服务评价
CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    escort_id       INTEGER REFERENCES escorts(id),
    rating          INTEGER NOT NULL,          -- 1..5
    tags            TEXT NOT NULL DEFAULT '',  -- 准时/耐心/专业…
    comment         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- 越权操作 -> 人工确认队列
CREATE TABLE IF NOT EXISTS escalations (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER REFERENCES appointments(id),
    requested_scope TEXT NOT NULL,
    reason          TEXT NOT NULL,
    requested_by    TEXT NOT NULL,            -- 角色或陪诊员
    status          TEXT NOT NULL DEFAULT 'pending', -- pending / approved / denied
    resolved_by     TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

-- 进程内模拟通知的落库记录（含重试次数，便于测试与页面展示）
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER REFERENCES appointments(id),
    channel         TEXT NOT NULL, -- sms / push / dispatch
    target          TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL, -- pending / sent / failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(
        str(db_path or config.DB_PATH),
        timeout=15,
        isolation_level=None,  # 手工事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def rows_to_dicts(rows: Iterable[sqlite3.Row]):
    return [dict(r) for r in rows]
