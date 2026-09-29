"""角色视角序列化与列表查询。

- 患者端（patient）：永远不含 internal_note，证件/联系方式为演示脱敏值；
- 陪诊员抢单前（escort/open）：只看到科室、时间、行动辅助、授权“数量与名称摘要”，
  看不到患者身份与附件；
- 接单陪诊员（escort/assigned）：可看必要字段；敏感信息仍按授权范围裁剪；
- 协调员（coordinator）：全字段。
"""
from . import config
from .services import (
    AUTHZ_ITEMS, SENSITIVE_SCOPES, _grant_row, _loads, get_appointment,
    list_attachments, list_handoffs, review_summary, timeline,
)


def _patient_brief(conn, pid):
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    return {"id": p["id"], "anon_code": p["anon_code"]}


def patient_full(conn, pid, include_note):
    p = conn.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    d = {
        "id": p["id"], "anon_code": p["anon_code"],
        "demo_name": p["demo_name"], "demo_id_masked": p["demo_id_masked"],
        "gender": p["gender"], "age": p["age"],
        "contact_masked": p["contact_masked"],
        "_data_notice": "演示数据，非真实患者信息",
    }
    if include_note:
        d["note"] = p["note"]
    return d


def _escort_brief(conn, eid):
    if not eid:
        return None
    e = conn.execute("SELECT id,name,phone_masked,rating_avg,rating_count FROM escorts WHERE id=?", (eid,)).fetchone()
    return dict(e) if e else None


def serialize_appointment(conn, row, role, escort_id=None, with_related=True):
    scope = _loads(row["authorization_scope"], [])
    mine = escort_id is not None and row["escort_id"] == escort_id
    assigned = row["status"] == "assigned" or row["status"] == "completed"
    base = {
        "id": row["id"], "code": row["code"],
        "department": row["department"], "exam_item": row["exam_item"],
        "scheduled_at": row["scheduled_at"], "end_at": row["end_at"],
        "mobility": row["mobility"], "mobility_label": config.MOBILITY_LABELS.get(row["mobility"], row["mobility"]),
        "status": row["status"],
    }

    if role == "coordinator":
        base.update({
            "patient": patient_full(conn, row["patient_id"], include_note=True),
            "authorization_scope": scope,
            "authorization_labels": [AUTHZ_ITEMS[s] for s in scope],
            "escort": _escort_brief(conn, row["escort_id"]),
            "accepted_at": row["accepted_at"], "completed_at": row["completed_at"],
            "internal_note": row["internal_note"],
        })
    elif role == "patient":
        # 患者端：匿名编号 + 本人演示档案，绝不返回内部备注
        base.update({
            "patient": patient_full(conn, row["patient_id"], include_note=False),
            "authorization_scope": scope,
            "authorization_labels": [AUTHZ_ITEMS[s] for s in scope],
            "escort": _escort_brief(conn, row["escort_id"]),
        })
    elif role == "escort":
        grant = _grant_row(conn, row["id"], escort_id) if escort_id else None
        if mine and grant:
            # 已接单：必要字段可见
            base.update({
                "patient": patient_full(conn, row["patient_id"], include_note=False),
                "authorization_scope": scope,
                "authorization_labels": [AUTHZ_ITEMS[s] for s in scope],
                "sensitive_scopes": [s for s in scope if s in SENSITIVE_SCOPES],
                "escort": _escort_brief(conn, row["escort_id"]),
                "accepted_at": row["accepted_at"],
                "granted_fields": _loads(grant["granted_fields"], []),
                "mine": True,
            })
        else:
            # 抢单前：只给最少决策信息，患者身份不可见
            base.update({
                "patient": _patient_brief(conn, row["patient_id"]),
                "authorization_summary": {
                    "count": len(scope),
                    "items": [AUTHZ_ITEMS[s] for s in scope],
                    "has_sensitive": any(s in SENSITIVE_SCOPES for s in scope),
                },
                "mine": False,
            })
    if with_related:
        base["timeline"] = [dict(t) for t in timeline(conn, row["id"])]
        base["handoffs"] = [dict(h, materials=_loads(h["materials"], [])) for h in list_handoffs(conn, row["id"])]
        if role in ("coordinator", "patient") or (role == "escort" and mine):
            base["attachments"] = list_attachments(conn, row["id"], role, escort_id)
        else:
            base["attachments"] = []
        base["exceptions"] = [dict(e) for e in conn.execute(
            "SELECT * FROM service_exceptions WHERE appointment_id=? ORDER BY id", (row["id"],)
        ).fetchall()]
    return base


def list_appointments(conn, role, escort_id=None, status=None, date=None, patient_id=None):
    sql, args = "SELECT * FROM appointments WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if date:
        sql += " AND substr(scheduled_at,1,10)=?"; args.append(date)
    if patient_id is not None:
        sql += " AND patient_id=?"; args.append(patient_id)
    if role == "escort":
        # 陪诊员列表：开放单 + 自己的单
        sql += " AND (status='open' OR escort_id=?)"; args.append(escort_id or -1)
    sql += " ORDER BY scheduled_at"
    rows = conn.execute(sql, args).fetchall()
    return [serialize_appointment(conn, r, role, escort_id, with_related=False) for r in rows]


def calendar(conn, role, escort_id=None, month=None):
    """预约日历：按天聚合计数。"""
    sql = ("SELECT substr(scheduled_at,1,10) d, status, COUNT(*) c FROM appointments WHERE 1=1")
    args = []
    if month:
        sql += " AND substr(scheduled_at,1,7)=?"; args.append(month)
    if role == "escort":
        sql += " AND (status='open' OR escort_id=?)"; args.append(escort_id or -1)
    sql += " GROUP BY d, status ORDER BY d"
    days = {}
    for r in conn.execute(sql, args).fetchall():
        days.setdefault(r["d"], {})[r["status"]] = r["c"]
    return [{"date": d, "statuses": s} for d, s in sorted(days.items())]


def dashboard(conn, role, escort_id=None):
    out = {"review_summary": review_summary(conn, escort_id if role == "escort" else None)}
    if role == "escort":
        rows = conn.execute(
            "SELECT * FROM appointments WHERE escort_id=? AND status='assigned' ORDER BY scheduled_at",
            (escort_id,),
        ).fetchall()
        out["my_tasks"] = [serialize_appointment(conn, r, "escort", escort_id) for r in rows]
    else:
        out["open_count"] = conn.execute(
            "SELECT COUNT(*) c FROM appointments WHERE status='open'").fetchone()["c"]
        out["active_count"] = conn.execute(
            "SELECT COUNT(*) c FROM appointments WHERE status='assigned'").fetchone()["c"]
        out["manual_pending"] = [dict(r) for r in conn.execute(
            "SELECT * FROM manual_reviews WHERE status='pending' ORDER BY id").fetchall()]
    return out
