"""领域服务：预约、抢单、授权、签到时间线、交接、异常改期、附件最小权限、评价。

角色与数据可见性：
- coordinator（平台协调员）：全字段。
- escort（陪诊员）：抢单前只看科室/时间/行动辅助与授权“摘要”；
  抢单成功后通过 escort_grants 获得必要字段（演示姓名/证件/授权明细/普通附件），
  敏感附件（检查结果等）还要求其查看权限在授权范围内。
- patient（患者端）：匿名编号视角，内部备注恒不返回。
"""
import json
import os
import uuid
from datetime import datetime, timedelta

from . import config
from .adapters import InProcessNotifier, InProcessRouter, now_iso
from .db import get_conn, transaction


class DomainError(Exception):
    """业务规则错误（HTTP 层映射为 4xx）。"""

    def __init__(self, message, code="domain_error", status=400, extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.extra = extra or {}


# 预约可包含的授权事项清单（授权范围只能从这里取）
AUTHZ_ITEMS = {
    "sign_in": "代为报到签到",
    "queue": "排队等候与叫号跟进",
    "exam_escort": "检查室全程陪同",
    "receive_report": "代取检查报告",
    "view_report": "查看检查结果",
    "wheelchair": "使用/推送轮椅",
    "payment_assist": "协助自助缴费（不接触密码）",
    "medicine": "代取口服药品",
    "handoff_family": "与家属/病房交接",
}

# 各事项是否属于敏感操作（默认敏感；接单本身不自动放行）
SENSITIVE_SCOPES = {"view_report", "receive_report", "payment_assist", "medicine"}

# 接单成功后默认对陪诊员开放的“必要字段”
GRANTED_FIELDS_BASE = ["demo_name", "demo_id_masked", "contact_masked", "authorization_scope", "mobility"]

# 终态：不可再推进、不可重新开启
TERMINAL_STATUSES = {"completed", "cancelled", "refused"}


def _loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        raise DomainError(f"时间格式错误: {s!r}，应为 YYYY-MM-DDTHH:MM", "bad_time", 422)


# ---------------------------------------------------------------- 预约与档案

def create_patient(conn, anon_code, demo_name, demo_id_masked, gender="", age=None,
                   contact_masked="", note=""):
    cur = conn.execute(
        "INSERT INTO patients(anon_code,demo_name,demo_id_masked,gender,age,contact_masked,note)"
        " VALUES (?,?,?,?,?,?,?)",
        (anon_code, demo_name, demo_id_masked, gender, age, contact_masked, note),
    )
    return cur.lastrowid


def create_appointment(conn, patient_id, department, scheduled_at, exam_item="",
                       duration_minutes=90, mobility="none", authorization_scope=None,
                       internal_note="", code=None):
    if mobility not in config.MOBILITY_LABELS:
        raise DomainError(f"未知行动辅助要求: {mobility}", "bad_mobility", 422)
    scope = authorization_scope or []
    bad = [s for s in scope if s not in AUTHZ_ITEMS]
    if bad:
        raise DomainError(f"授权事项不存在: {bad}", "bad_scope", 422)
    start = parse_dt(scheduled_at)
    end = start + timedelta(minutes=duration_minutes)
    code = code or f"A-{start.strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    cur = conn.execute(
        "INSERT INTO appointments(code,patient_id,department,exam_item,scheduled_at,end_at,"
        "mobility,authorization_scope,status,internal_note,created_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
        (code, patient_id, department, exam_item, start.isoformat(), end.isoformat(),
         mobility, _dumps(scope), internal_note, now_iso()),
    )
    return get_appointment(conn, cur.lastrowid)


def get_appointment(conn, appt_id):
    row = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if not row:
        raise DomainError("预约不存在", "not_found", 404)
    return row


# ---------------------------------------------------------------- 抢单（并发安全）

def accept_appointment(conn, appt_id, escort_id, notifier=None):
    """陪诊员抢单。

    使用条件 UPDATE：只有 status='open' 且 escort_id IS NULL 才能成功；
    数据库行锁 + 条件保证同一时段并发抢单仅一人成功。
    """
    appt = get_appointment(conn, appt_id)
    if appt["status"] in TERMINAL_STATUSES:
        raise DomainError("该预约已结束，不能接单", "terminal", 409)
    with transaction(conn):
        cur = conn.execute(
            "UPDATE appointments SET status='assigned', escort_id=?, accepted_at=? "
            "WHERE id=? AND status='open' AND escort_id IS NULL",
            (escort_id, now_iso(), appt_id),
        )
        if cur.rowcount == 0:
            # 区分：是自己已接，还是已被他人接走
            owner = conn.execute("SELECT escort_id FROM appointments WHERE id=?", (appt_id,)).fetchone()
            if owner and owner["escort_id"] == escort_id:
                raise DomainError("你已接下此单，请勿重复接单", "already_yours", 409)
            raise DomainError("手慢了，该单已被其他陪诊员接走", "already_taken", 409)
        conn.execute(
            "INSERT INTO escort_grants(appointment_id,escort_id,granted_fields,granted_at)"
            " VALUES (?,?,?,?)",
            (appt_id, escort_id, _dumps(GRANTED_FIELDS_BASE), now_iso()),
        )
    result = {"ok": True, "appointment_id": appt_id, "escort_id": escort_id}
    if notifier is not None:
        result["notify"] = notifier.send(
            appt_id, "app_push", f"escort:{escort_id}", f"接单成功：{appt['department']} {appt['scheduled_at'][5:16]}"
        )
    return result


# ---------------------------------------------------------------- 授权校验

def _grant_row(conn, appt_id, escort_id):
    return conn.execute(
        "SELECT * FROM escort_grants WHERE appointment_id=? AND escort_id=?",
        (appt_id, escort_id),
    ).fetchone()


def ensure_assigned(conn, appt, escort_id):
    if appt["escort_id"] != escort_id or not _grant_row(conn, appt["id"], escort_id):
        raise DomainError("只有接单陪诊员可查看该必要字段", "forbidden_field", 403)


def authorize(conn, appt_id, escort_id, scope_key):
    """校验某操作是否在授权范围内；超范围返回人工确认请求而不是直接执行。"""
    appt = get_appointment(conn, appt_id)
    ensure_assigned(conn, appt, escort_id)
    scope = set(_loads(appt["authorization_scope"], []))
    if scope_key in scope:
        return {"allowed": True, "scope": scope_key}
    # 超出授权范围：登记人工审核，状态 pending，操作挂起
    cur = conn.execute(
        "INSERT INTO manual_reviews(appointment_id,reason,requested_action,status,created_at)"
        " VALUES (?,?,?, 'pending', ?)",
        (appt_id, f"操作超出授权范围：{AUTHZ_ITEMS.get(scope_key, scope_key)}",
         f"escort:{escort_id} 请求 {scope_key}", now_iso()),
    )
    raise DomainError(
        f"「{AUTHZ_ITEMS.get(scope_key, scope_key)}」不在授权范围内，已转人工确认",
        "out_of_scope", 403, {"manual_review_id": cur.lastrowid, "status": "pending"},
    )


def decide_manual_review(conn, review_id, approve, decided_by="coordinator"):
    row = conn.execute("SELECT * FROM manual_reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        raise DomainError("人工审核单不存在", "not_found", 404)
    if row["status"] != "pending":
        raise DomainError("该审核已处理", "review_closed", 409)
    status = "approved" if approve else "rejected"
    conn.execute(
        "UPDATE manual_reviews SET status=?, decided_by=?, decided_at=? WHERE id=?",
        (status, decided_by, now_iso(), review_id),
    )
    return {"id": review_id, "status": status}


# ---------------------------------------------------------------- 时间线签到

STAGE_ORDER = {s: i for i, s in enumerate(config.TIMELINE_STAGES)}


def check_in(conn, appt_id, escort_id, stage, location="", note="", notifier=None):
    appt = get_appointment(conn, appt_id)
    ensure_assigned(conn, appt, escort_id)
    if appt["status"] in TERMINAL_STATUSES:
        raise DomainError("服务已结束，不能再签到", "terminal", 409)
    if stage not in STAGE_ORDER:
        raise DomainError(f"未知签到阶段: {stage}", "bad_stage", 422)
    # 顺序约束：必须按 arrived → waiting → exam_done → departed
    prev = conn.execute(
        "SELECT stage FROM timeline_events WHERE appointment_id=? ORDER BY id", (appt_id,)
    ).fetchall()
    done = [r["stage"] for r in prev]
    expected = config.TIMELINE_STAGES[len(done)] if len(done) < len(config.TIMELINE_STAGES) else None
    if stage in done:
        raise DomainError(f"{config.STAGE_LABELS[stage]}已签到，不能重复签到", "duplicate_stage", 409)
    if expected != stage:
        raise DomainError(
            f"签到顺序错误：请先完成「{config.STAGE_LABELS[expected]}」再签到「{config.STAGE_LABELS[stage]}」",
            "bad_order", 409,
        )
    with transaction(conn):
        conn.execute(
            "INSERT INTO timeline_events(appointment_id,stage,location,note,actor_escort_id,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (appt_id, stage, location, note, escort_id, now_iso()),
        )
        if stage == "departed":
            conn.execute(
                "UPDATE appointments SET status='completed', completed_at=? WHERE id=?",
                (now_iso(), appt_id),
            )
    result = {"ok": True, "stage": stage}
    if notifier is not None and stage in ("arrived", "departed"):
        word = "陪诊员已到达" if stage == "arrived" else "患者已安全离院，服务完成"
        result["notify"] = notifier.send(appt_id, "sms", f"patient:{appt['patient_id']}", word)
    return result


def timeline(conn, appt_id):
    return conn.execute(
        "SELECT * FROM timeline_events WHERE appointment_id=? ORDER BY id", (appt_id,)
    ).fetchall()


# ---------------------------------------------------------------- 交接记录

def handoff(conn, appt_id, escort_id, from_party, to_party, location="",
            materials=None, note=""):
    appt = get_appointment(conn, appt_id)
    ensure_assigned(conn, appt, escort_id)
    if appt["status"] in TERMINAL_STATUSES:
        raise DomainError("服务已结束，不能新增交接", "terminal", 409)
    if not from_party.strip() or not to_party.strip():
        raise DomainError("交接对象不能为空（交接双方必须留痕）", "handoff_party_required", 422)
    materials = materials or []
    cur = conn.execute(
        "INSERT INTO handoffs(appointment_id,from_party,to_party,location,materials,note,"
        "actor_escort_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (appt_id, from_party, to_party, location, _dumps(materials), note, escort_id, now_iso()),
    )
    return conn.execute("SELECT * FROM handoffs WHERE id=?", (cur.lastrowid,)).fetchone()


def list_handoffs(conn, appt_id):
    return conn.execute(
        "SELECT * FROM handoffs WHERE appointment_id=? ORDER BY id", (appt_id,)
    ).fetchall()


# ---------------------------------------------------------------- 异常处理

def report_exception(conn, appt_id, escort_id, exc_type, detail="", **kw):
    """六类异常分别处理。

    - reschedule 临时改期：写异常留痕并改预约时间（旧时间保留）；
    - cancelled 检查取消 / patient_refused 患者拒绝陪同：预约进入终态；
    - route_changed 路线变更：记录新旧路线（若改道本身需进入管制区域则转人工）；
    - escort_late 陪诊员迟到：记录迟到分钟并通知；
    - docs_missing 资料缺失：默认转人工（是否继续由协调员决定）。
    """
    appt = get_appointment(conn, appt_id)
    ensure_assigned(conn, appt, escort_id)
    if appt["status"] in TERMINAL_STATUSES:
        raise DomainError("服务已结束，不能登记异常", "terminal", 409)
    if exc_type not in config.EXCEPTION_LABELS:
        raise DomainError(f"未知异常类型: {exc_type}", "bad_exception", 422)

    old_time, new_time = appt["scheduled_at"], None
    old_route, new_route = kw.get("old_route", ""), kw.get("new_route", "")
    late_minutes = kw.get("late_minutes")
    requires_manual = 0
    new_status = None

    if exc_type == "reschedule":
        new_time = kw.get("new_time")
        if not new_time:
            raise DomainError("改期必须提供新时间", "new_time_required", 422)
        parse_dt(new_time)
        if parse_dt(new_time) <= parse_dt(old_time):
            raise DomainError("新时间必须晚于原预约时间", "bad_new_time", 422)
        end = (parse_dt(new_time) + timedelta(minutes=kw.get("duration_minutes", 90))).isoformat()
    elif exc_type in ("cancelled", "patient_refused"):
        new_status = "cancelled" if exc_type == "cancelled" else "refused"
    elif exc_type == "route_changed":
        new_route = kw.get("new_route", "")
        if not new_route:
            raise DomainError("路线变更必须提供新路线", "new_route_required", 422)
        # 进入未授权区域（演示规则：路线含“病区”）需人工确认
        requires_manual = 1 if "病区" in new_route else 0
    elif exc_type == "escort_late":
        late_minutes = int(kw.get("late_minutes", 0) or 0)
        if late_minutes <= 0:
            raise DomainError("迟到分钟数必须大于 0", "bad_late", 422)
    elif exc_type == "docs_missing":
        requires_manual = 1  # 资料是否可后补需协调员判定

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO service_exceptions(appointment_id,type,detail,old_time,new_time,"
            "old_route,new_route,late_minutes,requires_manual,manual_status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'none', ?)",
            (appt_id, exc_type, detail, old_time, new_time, old_route, new_route,
             late_minutes, requires_manual, now_iso()),
        )
        exc_id = cur.lastrowid
        if exc_type == "reschedule":
            end = (parse_dt(new_time) + timedelta(minutes=kw.get("duration_minutes", 90))).isoformat()
            conn.execute(
                "UPDATE appointments SET scheduled_at=?, end_at=? WHERE id=?",
                (new_time, end, appt_id),
            )
        elif new_status:
            conn.execute(
                "UPDATE appointments SET status=?, completed_at=? WHERE id=?",
                (new_status, now_iso(), appt_id),
            )
        if requires_manual:
            conn.execute(
                "UPDATE service_exceptions SET manual_status='pending' WHERE id=?", (exc_id,)
            )
            conn.execute(
                "INSERT INTO manual_reviews(appointment_id,reason,requested_action,status,created_at)"
                " VALUES (?,?,?, 'pending', ?)",
                (appt_id, f"异常需人工确认：{config.EXCEPTION_LABELS[exc_type]}",
                 f"exception:{exc_id}", now_iso()),
            )
    return {"id": exc_id, "type": exc_type, "requires_manual": bool(requires_manual),
            "new_status": new_status, "new_time": new_time}


def resolve_exception_manual(conn, exc_id, approve, decided_by="coordinator"):
    row = conn.execute("SELECT * FROM service_exceptions WHERE id=?", (exc_id,)).fetchone()
    if not row:
        raise DomainError("异常记录不存在", "not_found", 404)
    if not row["requires_manual"]:
        raise DomainError("该异常无需人工确认", "not_manual", 409)
    if row["manual_status"] != "pending":
        raise DomainError("该异常已处理", "closed", 409)
    conn.execute(
        "UPDATE service_exceptions SET manual_status=? WHERE id=?",
        ("approved" if approve else "rejected", exc_id),
    )
    return {"id": exc_id, "manual_status": "approved" if approve else "rejected"}


# ---------------------------------------------------------------- 附件（最小权限）

def register_attachment(conn, appt_id, filename, label="", sensitivity="normal",
                        requires_scope="", data=b""):
    appt = get_appointment(conn, appt_id)
    os.makedirs(config.ATTACHMENT_DIR, exist_ok=True)
    stored = f"appt{appt_id}_{uuid.uuid4().hex[:8]}_{os.path.basename(filename)}"
    path = os.path.join(config.ATTACHMENT_DIR, stored)
    with open(path, "wb") as fh:
        fh.write(data or b"(demo)")
    cur = conn.execute(
        "INSERT INTO attachments(appointment_id,filename,label,sensitivity,requires_scope,"
        "stored_name,size,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (appt_id, filename, label, sensitivity, requires_scope, stored, len(data or b"(demo)"), now_iso()),
    )
    return cur.lastrowid


def list_attachments(conn, appt_id, role, escort_id=None):
    """按最小权限列出附件元数据。

    - sensitive 附件：接单陪诊员仅当其 requires_scope 在本单授权范围内才可见；
    - 未接单陪诊员：不可见任何附件；
    - 患者/协调员：可见（患者端是本人档案视角）。
    """
    appt = get_appointment(conn, appt_id)
    rows = conn.execute("SELECT * FROM attachments WHERE appointment_id=?", (appt_id,)).fetchall()
    scope = set(_loads(appt["authorization_scope"], []))
    out = []
    for r in rows:
        visible, reason = True, ""
        if role == "escort":
            grant = _grant_row(conn, appt_id, escort_id) if escort_id else None
            if not grant or appt["escort_id"] != escort_id:
                visible, reason = False, "接单后可见"
            elif r["sensitivity"] == "sensitive":
                if r["requires_scope"] and r["requires_scope"] not in scope:
                    visible, reason = False, "超出授权范围，需人工确认"
        item = dict(id=r["id"], filename=r["filename"], label=r["label"],
                    sensitivity=r["sensitivity"], size=r["size"], visible=visible)
        if not visible:
            item["reason"] = reason
        out.append(item)
    return out


def read_attachment(conn, att_id, role, escort_id=None):
    r = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not r:
        raise DomainError("附件不存在", "not_found", 404)
    if role == "escort":
        appt = get_appointment(conn, r["appointment_id"])
        grant = _grant_row(conn, r["appointment_id"], escort_id) if escort_id else None
        if not grant or appt["escort_id"] != escort_id:
            raise DomainError("接单后才可查看附件", "forbidden_field", 403)
        if r["sensitivity"] == "sensitive" and r["requires_scope"]:
            scope = set(_loads(appt["authorization_scope"], []))
            if r["requires_scope"] not in scope:
                cur = conn.execute(
                    "INSERT INTO manual_reviews(appointment_id,reason,requested_action,status,created_at)"
                    " VALUES (?,?,?, 'pending', ?)",
                    (r["appointment_id"], f"敏感附件查看超授权：{r['filename']}",
                     f"escort:{escort_id} 查看附件 {att_id}", now_iso()),
                )
                raise DomainError("该敏感附件超出授权范围，已转人工确认", "out_of_scope", 403,
                                  {"manual_review_id": cur.lastrowid})
    path = os.path.join(config.ATTACHMENT_DIR, r["stored_name"])
    with open(path, "rb") as fh:
        return r, fh.read()


# ---------------------------------------------------------------- 评价

def submit_review(conn, appt_id, stars, tags=None, comment=""):
    appt = get_appointment(conn, appt_id)
    if appt["status"] != "completed":
        raise DomainError("仅已完成服务可评价", "not_completed", 409)
    if not (1 <= int(stars) <= 5):
        raise DomainError("评分需在 1-5 星", "bad_stars", 422)
    if conn.execute("SELECT 1 FROM reviews WHERE appointment_id=?", (appt_id,)).fetchone():
        raise DomainError("该服务已评价", "already_reviewed", 409)
    with transaction(conn):
        conn.execute(
            "INSERT INTO reviews(appointment_id,escort_id,stars,tags,comment,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (appt_id, appt["escort_id"], int(stars), _dumps(tags or []), comment, now_iso()),
        )
        row = conn.execute(
            "SELECT AVG(stars) a, COUNT(*) c FROM reviews WHERE escort_id=?",
            (appt["escort_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE escorts SET rating_avg=?, rating_count=? WHERE id=?",
            (round(row["a"], 2), row["c"], appt["escort_id"]),
        )
    return {"ok": True}


def review_summary(conn, escort_id=None):
    where = "WHERE r.escort_id=?" if escort_id else ""
    args = (escort_id,) if escort_id else ()
    rows = conn.execute(
        f"SELECT r.stars, r.tags FROM reviews r JOIN appointments a ON a.id=r.appointment_id {where}",
        args,
    ).fetchall()
    tag_count = {}
    dist = {i: 0 for i in range(1, 6)}
    total = 0
    for r in rows:
        dist[r["stars"]] += 1
        total += r["stars"]
        for t in _loads(r["tags"], []):
            tag_count[t] = tag_count.get(t, 0) + 1
    return {
        "count": len(rows),
        "avg": round(total / len(rows), 2) if rows else 0,
        "distribution": dist,
        "tags": sorted(tag_count.items(), key=lambda x: -x[1]),
    }
