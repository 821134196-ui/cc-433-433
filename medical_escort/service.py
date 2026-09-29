"""核心领域服务：预约、接单、签到、交接、异常处理、授权边界、附件与评价。

角色：
- dispatch 调度台：全部字段（含证件、内部备注、全部附件）
- escort   陪诊员：接单后才可查看必要字段；敏感信息最小化
- patient  患者：本人预约，永不展示内部备注；附件按 visibility 过滤

并发接单：claim_appointment 使用 BEGIN IMMEDIATE +
UPDATE ... WHERE id=? AND status='scheduled' AND escort_id IS NULL，
SQLite 单写锁串行化两个事务，rowcount==0 的一方收到 ConflictError。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional

from . import db as dbmod
from . import models, notifications, routing


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- 异常 ----------
class ServiceError(Exception):
    """业务规则错误，HTTP 层映射为 400。"""


class NotFound(ServiceError):
    """映射 404。"""


class PermissionDenied(ServiceError):
    """越权 / 患者隔离冲突，映射 403。"""


class ConflictError(ServiceError):
    """并发冲突（接单竞争、状态顺序），映射 409。"""


# ---------- 应用外壳（持锁 + 连接工厂 + 可选异步通知） ----------
class EscortApp:
    def __init__(self, db_path: Optional[str] = None, async_notify: bool = False):
        self.db_path = db_path
        self.async_notify = async_notify
        self.lock = threading.RLock()
        conn = self.connect()
        try:
            dbmod.init_db(conn)
        finally:
            conn.close()

    def connect(self):
        return dbmod.connect(self.db_path)


# ================= 基础查询 =================
def _get(conn, table: str, obj_id: int):
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (obj_id,)).fetchone()
    if row is None:
        raise NotFound(f"{table}#{obj_id} 不存在")
    return row


def get_appointment(conn, appointment_id: int):
    return _get(conn, "appointments", appointment_id)


def list_patients(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM patients ORDER BY id").fetchall()]


def list_escorts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM escorts ORDER BY id").fetchall()]


def list_appointments(conn, status: Optional[str] = None,
                      date: Optional[str] = None,
                      escort_id: Optional[int] = None,
                      patient_id: Optional[int] = None):
    sql = ("SELECT a.*, p.code AS patient_code, p.display_name, p.gender, p.age,"
           " p.contact_phone, p.mobility_note,"
           " e.name AS escort_name, e.code AS escort_code"
           " FROM appointments a"
           " JOIN patients p ON p.id=a.patient_id"
           " LEFT JOIN escorts e ON e.id=a.escort_id WHERE 1=1")
    args: list = []
    if status:
        sql += " AND a.status=?"
        args.append(status)
    if date:
        sql += " AND substr(a.scheduled_start,1,10)=?"
        args.append(date)
    if escort_id is not None:
        sql += " AND a.escort_id=?"
        args.append(escort_id)
    if patient_id is not None:
        sql += " AND a.patient_id=?"
        args.append(patient_id)
    sql += " ORDER BY a.scheduled_start"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ================= 视图序列化（字段最小化 / 患者隔离） =================
SENSITIVE_CATEGORIES = {"id_doc", "exam_result"}


def can_see_attachment(role: str, att: dict, appointment: dict,
                       viewer_escort_id: Optional[int],
                       granted_scope_codes: Optional[set] = None) -> bool:
    """敏感附件最小权限展示。

    - dispatch：全部可见
    - patient：visibility ∈ {patient, all}
    - escort：仅接单陪诊员；id_doc 需 escort/all；exam_result 需已授权 med_report
      且 visibility ∈ {escort, all}；consent_form/other 按 escort/all
    """
    vis = att["visibility"]
    if role == "dispatch":
        return True
    if role == "patient":
        return vis in ("patient", "all")
    if role == "escort":
        if viewer_escort_id is None or viewer_escort_id != appointment["escort_id"]:
            return False
        if att["category"] == "exam_result":
            return (models.SCOPE_REPORT in (granted_scope_codes or set())
                    and vis in ("escort", "all"))
        if att["category"] == "id_doc":
            return vis in ("escort", "all")
        return vis in ("escort", "all")
    return False


def serialize_appointment(conn, a: dict, role: str,
                          viewer_escort_id: Optional[int] = None,
                          patient_code: Optional[str] = None) -> dict:
    """按角色裁剪预约详情。患者端隐藏内部备注；未接单陪诊员看不到必要字段。"""
    d = dict(a)
    # 通用：先挂上授权与时间线等，再按角色裁剪
    scopes = granted_scopes(conn, a["id"])
    d["consents"] = scopes
    d["status_label"] = models.STATUS_LABELS.get(a["status"], a["status"])

    # 单表详情缺 join 字段时补齐患者编号与陪诊员信息
    if "patient_code" not in a or "display_name" not in a:
        prow0 = conn.execute(
            "SELECT code, display_name, gender, age, mobility_note FROM patients"
            " WHERE id=?", (a["patient_id"],)).fetchone()
        d.setdefault("patient_code", prow0["code"])
        d.setdefault("display_name", prow0["display_name"])
        d.setdefault("gender", prow0["gender"])
        d.setdefault("age", prow0["age"])
        d.setdefault("mobility_note", prow0["mobility_note"])
    if a.get("escort_id") is not None and "escort_name" not in a:
        erow = conn.execute(
            "SELECT name, code FROM escorts WHERE id=?",
            (a["escort_id"],)).fetchone()
        if erow:
            d["escort_name"] = erow["name"]
            d["escort_code"] = erow["code"]

    is_assigned_escort = role == "escort" and viewer_escort_id == a.get("escort_id") \
        and a.get("escort_id") is not None

    if role == "patient":
        d.pop("internal_note", None)
        d.pop("full_name", None)
        d.pop("id_document", None)
    elif role == "escort" and not is_assigned_escort:
        # 接单前：只展示完成接单决策所需的最小字段
        for k in ("special_requirements", "internal_note", "full_name",
                  "id_document", "contact_phone", "cancel_reason"):
            d.pop(k, None)
        # 授权范围在接单前也不逐项展示，避免信息过取
        d.pop("consents", None)

    # 附件（含敏感）按最小权限
    atts = [dict(r) for r in conn.execute(
        "SELECT * FROM attachments WHERE appointment_id=?", (a["id"],)).fetchall()]
    scope_codes = {s["scope_code"] for s in scopes}
    visible_atts = []
    for att in atts:
        if can_see_attachment(role, att, a,
                              viewer_escort_id if is_assigned_escort else None,
                              scope_codes):
            att["category_label"] = models.ATTACHMENT_CATEGORY_LABELS.get(
                att["category"], att["category"])
            visible_atts.append(att)
    d["attachments"] = visible_atts

    if role != "patient":
        row = conn.execute(
            "SELECT full_name, id_document, contact_phone FROM patients WHERE id=?",
            (a["patient_id"],)).fetchone()
        if role == "dispatch":
            d["full_name"] = row["full_name"]
            d["id_document"] = row["id_document"]
            d["contact_phone"] = row["contact_phone"]
        elif is_assigned_escort:
            # 陪诊员接单后：可看真实姓名与联系电话（必要字段），证件号仍掩码
            d["full_name"] = row["full_name"]
            d["id_document"] = _mask_id(row["id_document"])
            d["contact_phone"] = row["contact_phone"]
    else:
        patient = conn.execute(
            "SELECT full_name, id_document, contact_phone FROM patients WHERE id=?",
            (a["patient_id"],)).fetchone()
        d["id_document"] = _mask_id(patient["id_document"])
        d["full_name"] = patient["full_name"]
        d["contact_phone"] = patient["contact_phone"]
        if patient_code:
            owned = conn.execute(
                "SELECT 1 FROM patients WHERE id=? AND code=?",
                (a["patient_id"], patient_code)).fetchone()
            if not owned:
                raise PermissionDenied("无权访问该患者档案（患者隔离）")

    d["timeline"] = timeline(conn, a["id"])
    d["handoffs"] = list_handoffs(conn, a["id"])
    route = routing.active_route(conn, a["id"])
    d["route"] = route
    d["incidents"] = list_incidents(conn, a["id"])
    review = conn.execute(
        "SELECT * FROM reviews WHERE appointment_id=?", (a["id"],)).fetchone()
    d["review"] = dict(review) if review else None
    return d


def _mask_id(doc: str) -> str:
    if len(doc) <= 6:
        return doc[:1] + "*" * (len(doc) - 1)
    return doc[:3] + "*" * (len(doc) - 6) + doc[-3:]


def granted_scopes(conn, appointment_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM consents WHERE appointment_id=? AND granted=1 ORDER BY id",
        (appointment_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = models.SCOPE_LABELS.get(d["scope_code"], d["scope_code"])
        out.append(d)
    return out


def ensure_scope(conn, appointment_id: int, scope_code: str) -> None:
    """超出授权范围 -> PermissionDenied，调用方负责转人工确认。"""
    row = conn.execute(
        "SELECT 1 FROM consents WHERE appointment_id=? AND scope_code=? AND granted=1",
        (appointment_id, scope_code)).fetchone()
    if row is None:
        raise PermissionDenied(
            f"授权范围不含「{models.SCOPE_LABELS.get(scope_code, scope_code)}」，"
            "须转人工确认")


def timeline(conn, appointment_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, e.name AS actor_name FROM checkins c"
        " LEFT JOIN escorts e ON e.id=c.actor_id"
        " WHERE c.appointment_id=? ORDER BY c.id", (appointment_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stage_label"] = models.STAGE_LABELS.get(d["stage"], d["stage"])
        out.append(d)
    return out


def list_handoffs(conn, appointment_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM handoffs WHERE appointment_id=? ORDER BY seq",
        (appointment_id,)).fetchall()]


def list_incidents(conn, appointment_id: Optional[int] = None,
                   patient_id: Optional[int] = None) -> list[dict]:
    sql = ("SELECT i.*, a.code AS appointment_code, a.patient_id FROM incidents i"
           " LEFT JOIN appointments a ON a.id=i.appointment_id")
    where, args = [], []
    if appointment_id is not None:
        where.append("i.appointment_id=?")
        args.append(appointment_id)
    if patient_id is not None:
        where.append("a.patient_id=?")
        args.append(patient_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.id" + (" DESC" if appointment_id is None else "")
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ================= 创建预约与授权 =================
def create_appointment(conn, patient_id: int, department: str, exam_item: str,
                       scheduled_start: str, scheduled_end: str,
                       mobility_needs: str = "", special_requirements: str = "",
                       internal_note: str = "",
                       scope_codes: Optional[list[str]] = None) -> int:
    if _get(conn, "patients", patient_id) is None:
        raise NotFound("患者不存在")
    code = f"A-{uuid.uuid4().hex[:8].upper()}"
    cur = conn.execute(
        "INSERT INTO appointments(code,patient_id,department,exam_item,"
        "scheduled_start,scheduled_end,mobility_needs,special_requirements,"
        "internal_note,status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?, 'scheduled', ?, ?)",
        (code, patient_id, department, exam_item, scheduled_start, scheduled_end,
         mobility_needs, special_requirements, internal_note,
         now_str(), now_str()))
    appt_id = cur.lastrowid
    for sc in (scope_codes or []):
        if sc not in models.ALL_SCOPES:
            raise ServiceError(f"未知授权事项: {sc}")
        conn.execute(
            "INSERT INTO consents(appointment_id,scope_code,label,granted,created_at)"
            " VALUES (?,?,?,1,?)",
            (appt_id, sc, models.SCOPE_LABELS[sc], now_str()))
    # 初始路线
    routing.save_route(conn, appt_id, department, mobility_needs, reason="初始路线")
    return appt_id


# ================= 并发接单（核心） =================
def claim_appointment(app: EscortApp, appointment_id: int, escort_id: int):
    """同一预约并发接单只允许一人成功。

    BEGIN IMMEDIATE 立即获取 SQLite 保留锁 -> 写锁，第二个事务在 BEGIN 处
    排队；随后条件 UPDATE 保证只有 status='scheduled' 且无人接单时成功。
    另校验服务时段重叠：同一陪诊员同一时段不可持有两个任务。
    """
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = conn.execute(
                "SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
            if appt is None:
                conn.execute("ROLLBACK")
                raise NotFound("预约不存在")
            escort = conn.execute(
                "SELECT * FROM escorts WHERE id=?", (escort_id,)).fetchone()
            if escort is None or escort["status"] != "active":
                conn.execute("ROLLBACK")
                raise ServiceError("陪诊员不可用或已停用")

            overlap = conn.execute(
                "SELECT a.id FROM appointments a WHERE a.escort_id=?"
                " AND a.status IN ('claimed','arrived','waiting','in_exam')"
                " AND a.id != ? AND NOT ("
                " a.scheduled_end <= ? OR a.scheduled_start >= ?)",
                (escort_id, appointment_id,
                 appt["scheduled_start"], appt["scheduled_end"])).fetchone()
            if overlap is not None:
                conn.execute("ROLLBACK")
                raise ConflictError("该陪诊员在同一服务时段已有任务")

            cur = conn.execute(
                "UPDATE appointments SET status='claimed', escort_id=?,"
                " claimed_at=?, updated_at=?"
                " WHERE id=? AND status='scheduled' AND escort_id IS NULL",
                (escort_id, now_str(), now_str(), appointment_id))
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                raise ConflictError("手慢一步：该预约已被其他陪诊员接走")
            conn.execute("COMMIT")
        except Exception:
            # 连接可能仍处于事务中
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # 通知在事务外发送（异步或同步）
    _notify_claim(app, appointment_id, escort_id)
    return appointment_id


def _notify_claim(app: EscortApp, appointment_id: int, escort_id: int) -> None:
    conn = app.connect()
    try:
        row = conn.execute(
            "SELECT a.code, e.name, p.display_name FROM appointments a"
            " JOIN escorts e ON e.id=a.escort_id"
            " JOIN patients p ON p.id=a.patient_id WHERE a.id=?",
            (appointment_id,)).fetchone()
        nid = notifications.enqueue(
            conn, appointment_id, "dispatch", "调度台",
            f"预约 {row['code']}（{row['display_name']}）已由陪诊员 {row['name']} 接单")
    finally:
        conn.close()
    if app.async_notify:
        notifications.send_async(app, nid)
    else:
        c = app.connect()
        try:
            notifications.send_now(c, nid)
        finally:
            c.close()


# ================= 逐步签到（状态顺序） =================
# 允许的签到推进路径；claimed 可直接 arrived，其余严格按序
STAGE_SEQUENCE = {
    "arrived": {"claimed"},
    "waiting": {"arrived"},
    "in_exam": {"waiting"},
    "completed": {"in_exam"},
    "discharged": {"completed", "waiting"},  # 放弃检查直接离院也允许，但需备注
}


def checkin(app: EscortApp, appointment_id: int, escort_id: int, stage: str,
            location: str = "", note: str = "", role: str = "escort"):
    if stage not in models.STAGE_TO_STATUS:
        raise ServiceError("未知签到阶段")
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = get_appointment(conn, appointment_id)
            if appt["status"] in models.TERMINAL_STATUSES:
                raise ConflictError("服务已结束，不能重新开启或补签到")
            if role == "escort" and appt["escort_id"] != escort_id:
                raise PermissionDenied("只有接单陪诊员可以签到")
            # 重复签到同阶段：幂等成功，不再校验状态顺序
            dup = conn.execute(
                "SELECT 1 FROM checkins WHERE appointment_id=? AND stage=?",
                (appointment_id, stage)).fetchone()
            if dup:
                conn.execute("COMMIT")
                return appointment_id
            allowed = STAGE_SEQUENCE[stage]
            if appt["status"] not in allowed:
                order = " → ".join(
                    models.STAGE_LABELS[s] for s in models.STAGE_ORDER)
                raise ConflictError(
                    f"状态顺序错误：当前「{models.STATUS_LABELS[appt['status']]}」"
                    f"不能直接「{models.STAGE_LABELS[stage]}」。顺序为 {order}")
            conn.execute(
                "INSERT INTO checkins(appointment_id,stage,note,actor_id,location,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (appointment_id, stage, note, escort_id if role == "escort" else None,
                 location, now_str()))
            new_status = models.STAGE_TO_STATUS[stage]
            conn.execute(
                "UPDATE appointments SET status=?, updated_at=?,"
                " completed_at=CASE WHEN ?='discharged' THEN ? ELSE completed_at END"
                " WHERE id=?",
                (new_status, now_str(), stage, now_str(), appointment_id))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    return appointment_id


# ================= 交接（对象与时间必须留痕） =================
def add_handoff(conn, appointment_id: int, from_party: str, to_party: str,
                location: str, items: str = "", patient_confirmed: bool = False,
                note: str = "") -> int:
    if not (from_party and to_party and location):
        raise ServiceError("交接双方与交接地点必须填写，全程留痕")
    appt = get_appointment(conn, appointment_id)
    if appt["status"] == models.STATUS_DISCHARGED:
        # 终态后仍允许补录？需求要求已完成不能重新开启；交接记录本身允许归档补录，
        # 但时间必须显式给出，这里禁止以简化语义
        raise ConflictError("服务已离院归档，不能新增交接")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS s FROM handoffs WHERE appointment_id=?",
        (appointment_id,)).fetchone()["s"]
    cur = conn.execute(
        "INSERT INTO handoffs(appointment_id,seq,from_party,to_party,handoff_time,"
        "location,items,patient_confirmed,note,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (appointment_id, seq, from_party, to_party, now_str(), location, items,
         1 if patient_confirmed else 0, note, now_str()))
    return cur.lastrowid


# ================= 六类异常分别处理 =================
def _log_incident(conn, appointment_id: Optional[int], kind: str,
                  detail: str = "", old: str = "", new: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO incidents(appointment_id,kind,detail,old_value,new_value,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (appointment_id, kind, detail, old, new, now_str()))
    return cur.lastrowid


def reschedule(app: EscortApp, appointment_id: int, new_start: str, new_end: str,
               reason: str, by_role: str = "dispatch"):
    """临时改期：进入 rescheduling 中间态留痕，确认后回到 scheduled 并释放旧接单。

    已离院/已取消的终态服务不允许改期（不能重新开启）。
    """
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = get_appointment(conn, appointment_id)
            if appt["status"] in models.TERMINAL_STATUSES:
                raise ConflictError("服务已结束，不能改期重开")
            old_time = appt["scheduled_start"]
            # 记录改期事件；若已被接单，释放陪诊员以便重新派单
            conn.execute(
                "UPDATE appointments SET status='rescheduling', updated_at=? WHERE id=?",
                (now_str(), appointment_id))
            _log_incident(conn, appointment_id, models.INCIDENT_RESCHEDULE,
                          reason, old_time, new_start)
            conn.execute("COMMIT")
            # 确认改期：落新时间并回到待接单
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE appointments SET status='scheduled', scheduled_start=?,"
                " scheduled_end=?, escort_id=NULL, claimed_at=NULL, version=version+1,"
                " updated_at=? WHERE id=?",
                (new_start, new_end, now_str(), appointment_id))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    return appointment_id


def cancel_exam(app: EscortApp, appointment_id: int, reason: str):
    """检查取消：终态 cancelled，不可重新开启。"""
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = get_appointment(conn, appointment_id)
            if appt["status"] in models.TERMINAL_STATUSES:
                raise ConflictError("服务已结束，状态不可变更")
            conn.execute(
                "UPDATE appointments SET status='cancelled', cancel_reason=?,"
                " updated_at=? WHERE id=?",
                (reason, now_str(), appointment_id))
            _log_incident(conn, appointment_id, models.INCIDENT_EXAM_CANCELLED, reason)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    return appointment_id


def patient_refuse(app: EscortApp, appointment_id: int, detail: str):
    """患者拒绝陪同：终态 rejected 留痕。"""
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = get_appointment(conn, appointment_id)
            if appt["status"] in models.TERMINAL_STATUSES:
                raise ConflictError("服务已结束，状态不可变更")
            conn.execute(
                "UPDATE appointments SET status='rejected', cancel_reason=?,"
                " updated_at=? WHERE id=?",
                ("患者拒绝陪同：" + detail, now_str(), appointment_id))
            _log_incident(conn, appointment_id, models.INCIDENT_PATIENT_REFUSED, detail)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    return appointment_id


def change_route(app: EscortApp, appointment_id: int, reason: str,
                 new_steps: Optional[list[dict]] = None,
                 department: Optional[str] = None,
                 mobility_needs: Optional[str] = None):
    """路线变更：生成新版本路线并留痕，旧版本保留。"""
    with app.lock:
        conn = app.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            appt = get_appointment(conn, appointment_id)
            dept = department or appt["department"]
            mob = mobility_needs if mobility_needs is not None else appt["mobility_needs"]
            rid = routing.save_route(conn, appointment_id, dept, mob,
                                     reason=reason, overrides=new_steps)
            route = conn.execute("SELECT version FROM routes WHERE id=?",
                                 (rid,)).fetchone()
            _log_incident(conn, appointment_id, models.INCIDENT_ROUTE_CHANGE, reason,
                          f"v{route['version']-1}", f"v{route['version']}")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    return rid


def report_escort_late(conn, appointment_id: int, minutes: int, detail: str = ""):
    """陪诊员迟到：记录事件并通知调度台（不改变主状态机）。"""
    _log_incident(conn, appointment_id, models.INCIDENT_ESCORT_LATE,
                  f"迟到 {minutes} 分钟。{detail}")
    nid = notifications.enqueue(
        conn, appointment_id, "dispatch", "调度台",
        f"预约 #{appointment_id} 陪诊员迟到 {minutes} 分钟，请关注")
    return nid


def report_material_missing(conn, appointment_id: int, material: str,
                            detail: str = ""):
    """资料缺失：登记事件，生成跟进事项；不阻断候诊，但须补齐。"""
    iid = _log_incident(conn, appointment_id, models.INCIDENT_MATERIAL_MISSING,
                        f"缺少：{material}。{detail}")
    nid = notifications.enqueue(
        conn, appointment_id, "dispatch", "调度台",
        f"预约 #{appointment_id} 资料缺失：{material}，请协调补齐")
    return iid, nid


# ================= 越权操作 -> 转人工确认 =================
def request_escalation(conn, appointment_id: int, requested_scope: str,
                       requested_by: str, reason: str) -> int:
    if requested_scope not in models.ALL_SCOPES:
        raise ServiceError("未知授权事项")
    cur = conn.execute(
        "INSERT INTO escalations(appointment_id,requested_scope,reason,requested_by,"
        "status,created_at) VALUES (?,?,?,?,'pending',?)",
        (appointment_id, requested_scope, reason, requested_by, now_str()))
    nid = notifications.enqueue(
        conn, appointment_id, "dispatch", "调度台人工审核",
        f"预约 #{appointment_id} 申请越权操作"
        f"「{models.SCOPE_LABELS[requested_scope]}」，待人工确认：{reason}")
    return cur.lastrowid


def resolve_escalation(conn, escalation_id: int, approve: bool,
                       resolved_by: str, note: str = ""):
    row = _get(conn, "escalations", escalation_id)
    if row["status"] != "pending":
        raise ConflictError("该人工确认已处理")
    if approve:
        # 批准即视为本次追加授权
        conn.execute(
            "INSERT INTO consents(appointment_id,scope_code,label,granted,created_at)"
            " VALUES (?,?,?,1,?)",
            (row["appointment_id"], row["requested_scope"],
             models.SCOPE_LABELS[row["requested_scope"]], now_str()))
    conn.execute(
        "UPDATE escalations SET status=?, resolved_by=?, resolution_note=?, resolved_at=?"
        " WHERE id=?",
        ("approved" if approve else "denied", resolved_by, note, now_str(),
         escalation_id))


def perform_scoped_action(conn, appointment_id: int, scope_code: str,
                          actor: str, reason: str = ""):
    """陪诊员执行敏感动作的统一入口：无授权则自动转人工，不允许自行执行。"""
    try:
        ensure_scope(conn, appointment_id, scope_code)
    except PermissionDenied:
        eid = request_escalation(conn, appointment_id, scope_code, actor,
                                 reason or "陪诊员发起的超出授权范围操作")
        raise PermissionDenied(
            f"超出授权范围，已转人工确认（工单 #{eid}），审核通过前不得执行")
    return True


def list_escalations(conn, status: Optional[str] = None):
    sql = ("SELECT x.*, a.code AS appointment_code FROM escalations x"
           " LEFT JOIN appointments a ON a.id=x.appointment_id")
    args: list = []
    if status:
        sql += " WHERE x.status=?"
        args.append(status)
    sql += " ORDER BY x.id DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ================= 附件（敏感，最小权限） =================
def save_attachment(conn, appointment_id: int, filename: str, data: bytes,
                    category: str = "other", sensitive: bool = True,
                    visibility: str = models.VIS_DISPATCH) -> int:
    get_appointment(conn, appointment_id)
    store_dir = os.path.join(str(_attachments_root(conn)), f"appt_{appointment_id}")
    os.makedirs(store_dir, exist_ok=True)
    stored = f"{uuid.uuid4().hex[:12]}_{filename}"
    with open(os.path.join(store_dir, stored), "wb") as f:
        f.write(data)
    cur = conn.execute(
        "INSERT INTO attachments(appointment_id,filename,stored_path,category,"
        "sensitive,visibility,uploaded_at) VALUES (?,?,?,?,?,?,?)",
        (appointment_id, filename, os.path.join(store_dir, stored), category,
         1 if sensitive else 0, visibility, now_str()))
    return cur.lastrowid


def _attachments_root(conn) -> str:
    from . import config
    return str(config.ATTACHMENT_DIR)


def attachment_disk_path(conn, attachment_id: int) -> tuple[dict, str]:
    att = dict(_get(conn, "attachments", attachment_id))
    return att, att["stored_path"]


# ================= 评价 =================
def submit_review(conn, appointment_id: int, rating: int, tags: str = "",
                  comment: str = ""):
    if not (1 <= int(rating) <= 5):
        raise ServiceError("评分需在 1-5 之间")
    appt = get_appointment(conn, appointment_id)
    if appt["status"] != models.STATUS_DISCHARGED:
        raise ConflictError("仅已离院完成的服务可以评价")
    exists = conn.execute(
        "SELECT 1 FROM reviews WHERE appointment_id=?", (appointment_id,)).fetchone()
    if exists:
        raise ConflictError("该服务已评价，不可重复提交")
    conn.execute(
        "INSERT INTO reviews(appointment_id,escort_id,rating,tags,comment,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (appointment_id, appt["escort_id"], int(rating), tags, comment, now_str()))


def review_summary(conn, escort_id: Optional[int] = None) -> dict:
    where = ""
    args: list = []
    if escort_id is not None:
        where = "WHERE escort_id=?"
        args.append(escort_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(AVG(rating),0) AS avg_rating,"
        f" COALESCE(SUM(CASE WHEN rating=5 THEN 1 ELSE 0 END),0) AS five,"
        f" COALESCE(SUM(CASE WHEN rating=4 THEN 1 ELSE 0 END),0) AS four,"
        f" COALESCE(SUM(CASE WHEN rating<=3 THEN 1 ELSE 0 END),0) AS low"
        f" FROM reviews {where}", args).fetchone()
    latest = [dict(r) for r in conn.execute(
        f"SELECT r.*, a.code AS appointment_code FROM reviews r"
        f" JOIN appointments a ON a.id=r.appointment_id {where}"
        " ORDER BY r.id DESC LIMIT 10", args).fetchall()]
    return {"count": row["n"], "avg_rating": round(row["avg_rating"], 2),
            "five": row["five"], "four": row["four"], "low": row["low"],
            "latest": latest}


# ================= 演示资料写入辅助 =================
def write_demo_attachment(app: EscortApp, appointment_id: int, filename: str,
                          content: str, category: str, visibility: str) -> int:
    conn = app.connect()
    try:
        return save_attachment(conn, appointment_id, filename,
                               content.encode("utf-8"), category,
                               sensitive=category in SENSITIVE_CATEGORIES,
                               visibility=visibility)
    finally:
        conn.close()
