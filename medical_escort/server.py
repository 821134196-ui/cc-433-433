"""零依赖 HTTP 服务：JSON API + 静态前端。

演示鉴权（非生产）：每个请求带
  role=dispatch | escort | patient
  escort_id=<id>      （role=escort 时）
  patient_code=P-xxxx （role=patient 时，决定患者隔离边界）
"""
from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, models, notifications, service
from .service import (ConflictError, EscortApp, NotFound, PermissionDenied,
                      ServiceError)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")


class Handler(BaseHTTPRequestHandler):
    app: EscortApp = None  # 由 main 注入

    server_version = "MedicalEscort/1.0"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _qs(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[-1] for k, v in q.items()}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ServiceError("请求体不是合法 JSON")

    def _json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status: int, message: str, **extra):
        self._json({"error": message, **extra}, status)

    def _actor(self, qs=None, body=None) -> dict:
        qs = qs or {}
        body = body or {}
        role = body.get("role") or qs.get("role") or "dispatch"
        if role not in ("dispatch", "escort", "patient"):
            raise ServiceError("role 只能是 dispatch/escort/patient")
        escort_id = body.get("escort_id") or qs.get("escort_id")
        escort_id = int(escort_id) if escort_id else None
        return {"role": role, "escort_id": escort_id,
                "patient_code": body.get("patient_code") or qs.get("patient_code")}

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/meta":
                return self._meta()
            if path == "/api/appointments":
                return self._list_appointments()
            if path.startswith("/api/appointments/"):
                rest = path.split("/")[3:]
                if len(rest) == 1:
                    return self._get_appointment(int(rest[0]))
            if path.startswith("/api/attachments/"):
                parts = path.split("/")
                # /api/attachments/<id>/download
                if len(parts) == 5 and parts[4] == "download":
                    return self._download_attachment(None, int(parts[3]))
            if path == "/api/escorts":
                return self._list_escorts()
            if path == "/api/patients":
                return self._list_patients()
            if path == "/api/escalations":
                return self._list_escalations()
            if path == "/api/incidents":
                return self._list_incidents()
            if path == "/api/notifications":
                return self._list_notifications()
            if path == "/api/reviews/summary":
                return self._review_summary()
            self._err(404, "接口不存在")
        except PermissionDenied as e:
            self._err(403, str(e))
        except NotFound as e:
            self._err(404, str(e))
        except ConflictError as e:
            self._err(409, str(e))
        except ServiceError as e:
            self._err(400, str(e))
        except (ValueError, IndexError):
            self._err(400, "请求参数错误")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/appointments":
                return self._create_appointment(body)
            parts = path.split("/")
            # /api/appointments/<id>/<action>
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "appointments":
                appt_id = int(parts[3])
                action = parts[4]
                return self._appointment_action(appt_id, action, body)
            if path.startswith("/api/escalations/") and path.endswith("/resolve"):
                return self._resolve_escalation(int(path.split("/")[3]), body)
            if path == "/api/notifications/retry":
                return self._retry_notifications()
            self._err(404, "接口不存在")
        except PermissionDenied as e:
            self._err(403, str(e), escalation=True)
        except NotFound as e:
            self._err(404, str(e))
        except ConflictError as e:
            self._err(409, str(e))
        except ServiceError as e:
            self._err(400, str(e))
        except (ValueError, IndexError):
            self._err(400, "请求参数错误")

    # ---------- 静态 ----------
    def _serve_static(self, rel: str):
        rel = rel.replace("\\", "/").lstrip("/")
        # 防目录穿越
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(full):
            return self._err(404, "文件不存在")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        if full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- API 实现 ----------
    def _meta(self):
        self._json({
            "statuses": [{"value": k, "label": v}
                         for k, v in models.STATUS_LABELS.items()],
            "stages": [{"value": s, "label": models.STAGE_LABELS[s]}
                       for s in models.STAGE_ORDER],
            "scopes": [{"value": k, "label": v}
                       for k, v in models.SCOPE_LABELS.items()],
            "incidents": [{"value": k, "label": v}
                          for k, v in models.INCIDENT_LABELS.items()],
        })

    def _list_appointments(self):
        qs = self._qs()
        actor = self._actor(qs)
        conn = self.app.connect()
        try:
            kwargs = {}
            if qs.get("status"):
                kwargs["status"] = qs["status"]
            if qs.get("date"):
                kwargs["date"] = qs["date"]
            if actor["role"] == "escort" and qs.get("escort_id"):
                kwargs["escort_id"] = int(qs["escort_id"])
            if actor["role"] == "patient":
                if not actor["patient_code"]:
                    raise PermissionDenied("患者端需提供 patient_code")
                prow = conn.execute(
                    "SELECT id FROM patients WHERE code=?",
                    (actor["patient_code"],)).fetchone()
                if not prow:
                    raise PermissionDenied("患者档案不存在")
                kwargs["patient_id"] = prow["id"]
            rows = service.list_appointments(conn, **kwargs)
            out = [service.serialize_appointment(
                conn, a, actor["role"],
                actor["escort_id"] if actor["role"] == "escort" else None,
                actor["patient_code"]) for a in rows]
            self._json({"items": out})
        finally:
            conn.close()

    def _get_appointment(self, appt_id: int):
        qs = self._qs()
        actor = self._actor(qs)
        conn = self.app.connect()
        try:
            a = service.get_appointment(conn, appt_id)
            data = service.serialize_appointment(
                conn, dict(a), actor["role"],
                actor["escort_id"] if actor["role"] == "escort" else None,
                actor["patient_code"])
            # 患者隔离：不属于自己的档案 403
            if actor["role"] == "patient":
                prow = conn.execute(
                    "SELECT id FROM patients WHERE code=?",
                    (actor["patient_code"],)).fetchone()
                if not prow or prow["id"] != a["patient_id"]:
                    raise PermissionDenied("无权查看该预约（患者隔离）")
            # 陪诊员看他人接单详情：只返回最小字段（serialize 已裁剪）
            self._json(data)
        finally:
            conn.close()

    def _create_appointment(self, body: dict):
        actor = self._actor(body=body)
        if actor["role"] != "dispatch":
            raise PermissionDenied("仅调度台可以创建预约")
        required = ("patient_id", "department", "exam_item",
                    "scheduled_start", "scheduled_end")
        for k in required:
            if not body.get(k):
                raise ServiceError(f"缺少必填字段: {k}")
        conn = self.app.connect()
        try:
            appt_id = service.create_appointment(
                conn,
                int(body["patient_id"]), body["department"], body["exam_item"],
                body["scheduled_start"], body["scheduled_end"],
                body.get("mobility_needs", ""),
                body.get("special_requirements", ""),
                body.get("internal_note", ""),
                body.get("scope_codes") or [])
            # 可选：随预约上传演示附件声明
            self._json({"id": appt_id}, 201)
        finally:
            conn.close()

    def _appointment_action(self, appt_id: int, action: str, body: dict):
        actor = self._actor(body=body)
        if action == "claim":
            if actor["role"] != "escort" or not actor["escort_id"]:
                raise PermissionDenied("仅陪诊员可以接单")
            service.claim_appointment(self.app, appt_id, actor["escort_id"])
            return self._json({"ok": True, "id": appt_id})

        if action == "checkin":
            if actor["role"] != "escort" or not actor["escort_id"]:
                raise PermissionDenied("仅接单陪诊员可以签到")
            service.checkin(self.app, appt_id, actor["escort_id"],
                            body.get("stage", ""), body.get("location", ""),
                            body.get("note", ""))
            return self._json({"ok": True})

        # 以下操作调度台 / 陪诊员按规则触发
        conn = self.app.connect()
        try:
            if action == "reschedule":
                if actor["role"] != "dispatch":
                    raise PermissionDenied("改期需调度台确认")
                for k in ("new_start", "new_end", "reason"):
                    if not body.get(k):
                        raise ServiceError(f"缺少字段: {k}")
                service.reschedule(self.app, appt_id, body["new_start"],
                                   body["new_end"], body["reason"])
                return self._json({"ok": True})

            if action == "cancel":
                if actor["role"] != "dispatch":
                    raise PermissionDenied("检查取消需调度台处理")
                service.cancel_exam(self.app, appt_id, body.get("reason", ""))
                return self._json({"ok": True})

            if action == "refuse":
                service.patient_refuse(self.app, appt_id, body.get("detail", ""))
                return self._json({"ok": True})

            if action == "route-change":
                service.change_route(
                    self.app, appt_id, body.get("reason", "院内路线变更"),
                    new_steps=body.get("steps"),
                    department=body.get("department"),
                    mobility_needs=body.get("mobility_needs"))
                return self._json({"ok": True})

            if action == "late":
                if actor["role"] != "escort":
                    raise PermissionDenied("仅陪诊员端上报迟到")
                nid = service.report_escort_late(
                    conn, appt_id, int(body.get("minutes", 0)),
                    body.get("detail", ""))
                conn.commit()
                return self._json({"ok": True, "notification_id": nid})

            if action == "material-missing":
                iid, nid = service.report_material_missing(
                    conn, appt_id, body.get("material", "未注明"),
                    body.get("detail", ""))
                conn.commit()
                return self._json({"ok": True, "incident_id": iid,
                                   "notification_id": nid})

            if action == "handoffs":
                hid = service.add_handoff(
                    conn, appt_id, body.get("from_party", ""),
                    body.get("to_party", ""), body.get("location", ""),
                    body.get("items", ""),
                    bool(body.get("patient_confirmed", False)),
                    body.get("note", ""))
                conn.commit()
                return self._json({"ok": True, "id": hid}, 201)

            if action == "review":
                service.submit_review(conn, appt_id, int(body.get("rating", 0)),
                                      body.get("tags", ""), body.get("comment", ""))
                conn.commit()
                return self._json({"ok": True})

            if action == "action":
                # 陪诊员尝试执行授权事项；越权自动转人工
                if actor["role"] != "escort" or not actor["escort_id"]:
                    raise PermissionDenied("仅陪诊员可以发起授权动作")
                scope = body.get("scope", "")
                escort = conn.execute(
                    "SELECT name FROM escorts WHERE id=?",
                    (actor["escort_id"],)).fetchone()
                service.perform_scoped_action(
                    conn, appt_id, scope,
                    f"陪诊员 {escort['name'] if escort else actor['escort_id']}",
                    body.get("reason", ""))
                conn.commit()
                return self._json({"ok": True, "scope": scope})

            self._err(404, "未知操作")
        finally:
            conn.close()

    def _resolve_escalation(self, escalation_id: int, body: dict):
        actor = self._actor(body=body)
        if actor["role"] != "dispatch":
            raise PermissionDenied("仅调度台可进行人工确认")
        conn = self.app.connect()
        try:
            service.resolve_escalation(
                conn, escalation_id, bool(body.get("approve")),
                body.get("by", "调度台"), body.get("note", ""))
            conn.commit()
            self._json({"ok": True})
        finally:
            conn.close()

    def _retry_notifications(self):
        conn = self.app.connect()
        try:
            n = notifications.drain_pending(conn)
            self._json({"retried_ok": n})
        finally:
            conn.close()

    def _list_escorts(self):
        actor = self._actor(self._qs())
        if actor["role"] == "patient":
            raise PermissionDenied("患者端不可查看陪诊员名录")
        conn = self.app.connect()
        try:
            self._json({"items": service.list_escorts(conn)})
        finally:
            conn.close()

    def _list_patients(self):
        actor = self._actor(self._qs())
        if actor["role"] != "dispatch":
            raise PermissionDenied("仅调度台可查看患者档案")
        conn = self.app.connect()
        try:
            self._json({"items": service.list_patients(conn)})
        finally:
            conn.close()

    def _list_escalations(self):
        qs = self._qs()
        actor = self._actor(qs)
        if actor["role"] == "patient":
            raise PermissionDenied("患者端不可查看人工审核队列")
        conn = self.app.connect()
        try:
            self._json({"items": service.list_escalations(conn, qs.get("status"))})
        finally:
            conn.close()

    def _list_incidents(self):
        actor = self._actor(self._qs())
        conn = self.app.connect()
        try:
            if actor["role"] == "patient":
                # 患者仅能看本人预约上的事件，不提供全院事件流
                if not actor["patient_code"]:
                    raise PermissionDenied("患者端需提供 patient_code")
                prow = conn.execute(
                    "SELECT id FROM patients WHERE code=?",
                    (actor["patient_code"],)).fetchone()
                if not prow:
                    raise PermissionDenied("患者档案不存在")
                self._json({"items": service.list_incidents(
                    conn, patient_id=prow["id"])})
                return
            self._json({"items": service.list_incidents(conn)})
        finally:
            conn.close()

    def _list_notifications(self):
        conn = self.app.connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT n.*, a.code AS appointment_code FROM notifications n"
                " LEFT JOIN appointments a ON a.id=n.appointment_id"
                " ORDER BY n.id DESC LIMIT 50").fetchall()]
            self._json({"items": rows})
        finally:
            conn.close()

    def _review_summary(self):
        qs = self._qs()
        conn = self.app.connect()
        try:
            escort_id = int(qs["escort_id"]) if qs.get("escort_id") else None
            self._json(service.review_summary(conn, escort_id))
        finally:
            conn.close()

    def _download_attachment(self, _, attachment_id: int):
        qs = self._qs()
        actor = self._actor(qs)
        conn = self.app.connect()
        try:
            att, path = service.attachment_disk_path(conn, attachment_id)
            appt = dict(service.get_appointment(conn, att["appointment_id"]))
            scopes = {s["scope_code"]
                      for s in service.granted_scopes(conn, appt["id"])}
            allowed = service.can_see_attachment(
                actor["role"], att, appt,
                actor["escort_id"] if actor["role"] == "escort" else None,
                scopes)
            if actor["role"] == "patient":
                prow = conn.execute(
                    "SELECT id FROM patients WHERE code=?",
                    (actor["patient_code"],)).fetchone()
                if not prow or prow["id"] != appt["patient_id"]:
                    raise PermissionDenied("无权下载该附件（患者隔离）")
            if not allowed:
                raise PermissionDenied("无权查看该敏感附件（最小权限）")
            if not os.path.isfile(path):
                raise NotFound("附件文件缺失")
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{att["filename"]}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            conn.close()


def create_server(port: int = None, db_path: str = None,
                  async_notify: bool = True) -> ThreadingHTTPServer:
    app = EscortApp(db_path=db_path, async_notify=async_notify)
    Handler.app = app
    httpd = ThreadingHTTPServer(("0.0.0.0", port or config.PORT), Handler)
    httpd.app = app
    return httpd
