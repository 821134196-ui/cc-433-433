"""零依赖 HTTP 服务：JSON API + 静态页面。

演示用身份切换（无真实登录）：请求头 X-Role=coordinator|escort|patient，
陪诊员另带 X-Escort-Id，患者带 X-Patient-Id。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, services
from .adapters import InProcessNotifier, InProcessRouter
from .db import get_conn, init_db
from .serializers import (
    calendar, dashboard, list_appointments, serialize_appointment,
)
from .services import DomainError

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

_CT = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class AppState:
    def __init__(self, fail_pattern=None):
        init_db()
        self.router = InProcessRouter()
        # 通知失败注入：默认短信通道前两次失败，用于演示通知重试
        self.fail_pattern = fail_pattern if fail_pattern is not None else {"sms": 0}

    def notifier(self, conn):
        return InProcessNotifier(
            conn, max_attempts=config.NOTIFY_MAX_ATTEMPTS,
            backoff=config.NOTIFY_BACKOFF_SECONDS, fail_pattern=self.fail_pattern,
        )


STATE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "EscortDemo/1.0"

    # ------------------------------------------------------------ 基础工具
    def _identity(self):
        role = self.headers.get("X-Role", "coordinator")
        if role not in ("coordinator", "escort", "patient"):
            role = "coordinator"
        escort_id = self.headers.get("X-Escort-Id")
        patient_id = self.headers.get("X-Patient-Id")
        return role, int(escort_id) if escort_id else None, int(patient_id) if patient_id else None

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, err):
        self._json({"error": err.code, "message": err.message, **err.extra}, err.status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DomainError("请求体必须是 JSON", "bad_json", 400)

    def log_message(self, fmt, *args):
        if os.environ.get("ESCORT_QUIET"):
            return
        super().log_message(fmt, *args)

    # ------------------------------------------------------------ 路由入口
    def do_GET(self):
        try:
            self._route_get()
        except DomainError as e:
            self._error(e)
        except Exception as e:  # noqa: BLE001 - 演示服务兜底
            self._json({"error": "internal", "message": str(e)}, 500)

    def do_POST(self):
        try:
            self._route_post()
        except DomainError as e:
            self._error(e)
        except Exception as e:  # noqa: BLE001
            self._json({"error": "internal", "message": str(e)}, 500)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    # ------------------------------------------------------------ GET
    def _route_get(self):
        path, qs = self._query()
        role, escort_id, patient_id = self._identity()
        conn = get_conn()
        try:
            def q(name, default=None):
                return qs.get(name, [default])[0]

            if path.startswith("/api/"):
                if path == "/api/meta":
                    return self._json({
                        "authz_items": [{"key": k, "label": v, "sensitive": k in services.SENSITIVE_SCOPES}
                                        for k, v in services.AUTHZ_ITEMS.items()],
                        "mobility": [{"key": k, "label": v} for k, v in config.MOBILITY_LABELS.items()],
                        "exceptions": [{"key": k, "label": v} for k, v in config.EXCEPTION_LABELS.items()],
                        "stages": [{"key": k, "label": v} for k, v in config.STAGE_LABELS.items()],
                    })
                if path == "/api/dashboard":
                    return self._json(dashboard(conn, role, escort_id))
                if path == "/api/calendar":
                    return self._json({"days": calendar(conn, role, escort_id, q("month"))})
                if path == "/api/appointments":
                    items = list_appointments(conn, role, escort_id, q("status"), q("date"),
                                              patient_id if role == "patient" else None)
                    return self._json({"items": items})
                if path == "/api/patients":
                    if role != "coordinator":
                        raise DomainError("仅协调员可查看档案列表", "forbidden", 403)
                    rows = conn.execute("SELECT * FROM patients ORDER BY id").fetchall()
                    return self._json({"items": [services_patient(r) for r in rows]})
                if path == "/api/escorts":
                    rows = conn.execute("SELECT * FROM escorts WHERE active=1 ORDER BY id").fetchall()
                    return self._json({"items": [dict(r) for r in rows]})
                if path == "/api/reviews/summary":
                    return self._json(services.review_summary(conn, escort_id if role == "escort" else None))
                if path == "/api/notifications":
                    if role == "escort":
                        rows = conn.execute(
                            "SELECT n.* FROM notifications n JOIN appointments a ON a.id=n.appointment_id"
                            " WHERE a.escort_id=? ORDER BY n.id DESC LIMIT 50", (escort_id,)).fetchall()
                    else:
                        rows = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
                    return self._json({"items": [dict(r) for r in rows]})
                if path == "/api/route":
                    route = STATE.router.plan(q("origin", "门诊大厅"), q("destination", "CT 3号机房"),
                                              alt=q("alt") == "1")
                    guide = STATE.router.current_guide(route, q("location", route[0]))
                    return self._json({"route": route, "guide": guide})

                # /api/appointments/{id}/...
                parts = [p for p in path.split("/") if p]
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "appointments":
                    appt_id = int(parts[2])
                    if len(parts) == 3:
                        row = services.get_appointment(conn, appt_id)
                        if role == "patient" and row["patient_id"] != patient_id:
                            raise DomainError("无权查看他人预约（患者隔离）", "isolated", 403)
                        return self._json(serialize_appointment(conn, row, role, escort_id))
                    if parts[3] == "attachments":
                        return self._json({"items": services.list_attachments(conn, appt_id, role, escort_id)})
                if len(parts) == 3 and parts[0] == "api" and parts[1] == "attachments":
                    row, data = services.read_attachment(conn, int(parts[2]), role, escort_id)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{urllib.parse.quote(row["filename"])}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                raise DomainError("接口不存在", "not_found", 404)
            # 静态资源
            return self._serve_static(path)
        finally:
            conn.close()

    # ------------------------------------------------------------ POST
    def _route_post(self):
        path, _ = self._query()
        role, escort_id, patient_id = self._identity()
        body = self._read_body()
        conn = get_conn()
        try:
            notifier = STATE.notifier(conn)
            parts = [p for p in path.split("/") if p]

            if path == "/api/appointments":
                if role == "escort":
                    raise DomainError("陪诊员不能创建预约", "forbidden", 403)
                pid = body.get("patient_id") or patient_id
                appt = services.create_appointment(
                    conn, int(pid), body["department"], body["scheduled_at"],
                    exam_item=body.get("exam_item", ""),
                    duration_minutes=int(body.get("duration_minutes", 90)),
                    mobility=body.get("mobility", "none"),
                    authorization_scope=body.get("authorization_scope", []),
                    internal_note=body.get("internal_note", ""),
                )
                return self._json(serialize_appointment(conn, appt, role, escort_id), 201)

            if path == "/api/patients":
                if role != "coordinator":
                    raise DomainError("仅协调员可建档", "forbidden", 403)
                pid = services.create_patient(
                    conn, body["anon_code"], body["demo_name"], body["demo_id_masked"],
                    body.get("gender", ""), body.get("age"),
                    body.get("contact_masked", ""), body.get("note", ""),
                )
                return self._json({"id": pid}, 201)

            if len(parts) == 3 and parts[:2] == ["api", "appointments"] and parts[2].isdigit() is False:
                raise DomainError("接口不存在", "not_found", 404)

            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "appointments":
                appt_id = int(parts[2])
                action = parts[3] if len(parts) > 3 else ""

                if action == "accept" and len(parts) == 4:
                    if role != "escort" or not escort_id:
                        raise DomainError("请以陪诊员身份接单", "forbidden", 403)
                    return self._json(services.accept_appointment(conn, appt_id, escort_id, notifier))
                if action == "checkin" and len(parts) == 4:
                    if role != "escort":
                        raise DomainError("仅陪诊员可签到", "forbidden", 403)
                    return self._json(services.check_in(
                        conn, appt_id, escort_id, body["stage"],
                        body.get("location", ""), body.get("note", ""), notifier))
                if action == "handoffs" and len(parts) == 4:
                    if role != "escort":
                        raise DomainError("仅陪诊员可登记交接", "forbidden", 403)
                    row = services.handoff(
                        conn, appt_id, escort_id, body["from_party"], body["to_party"],
                        body.get("location", ""), body.get("materials", []), body.get("note", ""))
                    return self._json(dict(row), 201)
                if action == "exceptions" and len(parts) == 4:
                    if role != "escort":
                        raise DomainError("仅陪诊员可登记异常", "forbidden", 403)
                    return self._json(services.report_exception(
                        conn, appt_id, escort_id, body["type"], body.get("detail", ""), **{
                            k: body[k] for k in
                            ("new_time", "old_route", "new_route", "late_minutes", "duration_minutes")
                            if k in body}))
                if action == "authorize" and len(parts) == 4:
                    if role != "escort":
                        raise DomainError("仅陪诊员发起授权校验", "forbidden", 403)
                    return self._json(services.authorize(conn, appt_id, escort_id, body["scope"]))
                if action == "reviews" and len(parts) == 4:
                    if role == "escort":
                        raise DomainError("陪诊员不能自评", "forbidden", 403)
                    services.submit_review(conn, appt_id, int(body["stars"]),
                                           body.get("tags", []), body.get("comment", ""))
                    return self._json({"ok": True}, 201)
                if action == "attachments" and len(parts) == 4:
                    if role != "coordinator":
                        raise DomainError("仅协调员可登记附件", "forbidden", 403)
                    aid = services.register_attachment(
                        conn, appt_id, body["filename"], body.get("label", ""),
                        body.get("sensitivity", "normal"), body.get("requires_scope", ""),
                        body.get("content", "(demo)").encode("utf-8"))
                    return self._json({"id": aid}, 201)
                raise DomainError("接口不存在", "not_found", 404)

            if len(parts) == 4 and parts[:2] == ["api", "manual-reviews"] and parts[3] == "decide":
                if role != "coordinator":
                    raise DomainError("仅协调员可处理人工确认", "forbidden", 403)
                return self._json(services.decide_manual_review(
                    conn, int(parts[2]), bool(body.get("approve")), body.get("decided_by", "coordinator")))

            if len(parts) == 4 and parts[:2] == ["api", "exceptions"] and parts[3] == "resolve":
                if role != "coordinator":
                    raise DomainError("仅协调员可处理异常确认", "forbidden", 403)
                return self._json(services.resolve_exception_manual(
                    conn, int(parts[2]), bool(body.get("approve")), body.get("decided_by", "coordinator")))

            raise DomainError("接口不存在", "not_found", 404)
        finally:
            conn.close()

    # ------------------------------------------------------------ 静态
    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        rel = urllib.parse.unquote(path.lstrip("/"))
        # 防目录穿越
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR + os.sep) and full != WEB_DIR:
            raise DomainError("非法路径", "forbidden", 403)
        if not os.path.isfile(full):
            raise DomainError("页面不存在", "not_found", 404)
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", _CT.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def services_patient(r):
    return {"id": r["id"], "anon_code": r["anon_code"], "demo_name": r["demo_name"],
            "demo_id_masked": r["demo_id_masked"], "gender": r["gender"], "age": r["age"],
            "contact_masked": r["contact_masked"], "note": r["note"],
            "_data_notice": "演示数据，非真实患者信息"}


def create_server(host=None, port=None, fail_pattern=None):
    global STATE
    STATE = AppState(fail_pattern=fail_pattern)
    httpd = ThreadingHTTPServer((host or config.HOST, port or config.PORT), Handler)
    return httpd


def main():
    import argparse
    ap = argparse.ArgumentParser(description="医疗陪诊预约与检查陪同交接平台（演示）")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--seed", action="store_true", help="启动前写入演示样例")
    ap.add_argument("--fail-sms", type=int, default=0, help="短信通道前 N 次发送失败（演示重试）")
    args = ap.parse_args()
    httpd = create_server(args.host, args.port, fail_pattern={"sms": args.fail_sms})
    if args.seed:
        from .seed import seed_demo
        seed_demo()
    print(f"陪诊平台已启动: http://{args.host}:{args.port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
