"""端到端测试：接单竞争、授权边界、状态顺序、通知重试、患者隔离，以及六类异常。

直接跑：python3 -m unittest -v   或   python3 tests/test_platform.py
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

# 必须在导入 app 之前指定独立数据目录
_TMP = tempfile.mkdtemp(prefix="escort-test-")
os.environ["ESCORT_DATA_DIR"] = _TMP
os.environ["ESCORT_QUIET"] = "1"

from app import config  # noqa: E402
from app import services  # noqa: E402
from app.adapters import InProcessNotifier, InProcessRouter  # noqa: E401,E402
from app.db import get_conn, init_db  # noqa: E402
from app.seed import seed_demo  # noqa: E402

config.NOTIFY_BACKOFF_SECONDS = 0  # 测试不等待


def _today(h=9, off=0):
    d = datetime.now() + timedelta(days=off)
    return d.replace(hour=h, minute=0, second=0, microsecond=0).isoformat()


class ServiceTestBase(unittest.TestCase):
    _world_seq = 0

    def setUp(self):
        self.db_path = os.path.join(_TMP, f"test_{os.getpid()}_{id(self)}.db")
        os.environ["ESCORT_DB_PATH"] = self.db_path
        # 让 config 指向本用例库
        config.DB_PATH = self.db_path
        init_db(self.db_path)

    def conn(self):
        return get_conn(self.db_path)

    def notifier(self, conn, fail=None):
        return InProcessNotifier(conn, max_attempts=3, backoff=0, fail_pattern=dict(fail or {}))

    def make_world(self, conn, scope=None, notify=False):
        """建 3 名陪诊员 + 1 患者 + 1 个开放预约，返回 (ids, appt)。"""
        ServiceTestBase._world_seq += 1
        seq = ServiceTestBase._world_seq
        es = []
        for name in ("测试陪诊甲", "测试陪诊乙", "测试陪诊丙"):
            cur = conn.execute("INSERT INTO escorts(name) VALUES (?)", (f"{name}{seq}",))
            es.append(cur.lastrowid)
        pid = services.create_patient(conn, f"T-{seq}", "演示·测试", "110101********9999",
                                      "女", 60, "130****0000", "内部备注：测试用，勿对外")
        appt = services.create_appointment(
            conn, pid, "放射科", _today(10), exam_item="CT",
            mobility="wheelchair",
            authorization_scope=scope if scope is not None else
            ["sign_in", "queue", "exam_escort", "wheelchair", "view_report"])
        n = self.notifier(conn) if notify else None
        return es, pid, appt, n


class TestAcceptRace(ServiceTestBase):
    def test_parallel_accept_only_one_wins(self):
        conn = self.conn()
        escorts, _, appt, _ = self.make_world(conn)

        results, barrier = [], threading.Barrier(len(escorts) * 4)

        def worker(eid):
            c = self.conn()  # 每线程独立连接
            barrier.wait()
            try:
                services.accept_appointment(c, appt["id"], eid)
                results.append(("ok", eid))
            except services.DomainError as e:
                results.append((e.code, eid))
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(eid,)) for eid in escorts * 4]
        for t in threads: t.start()
        for t in threads: t.join()

        winners = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(winners), 1, f"应有且仅有一人抢单成功，实际: {results}")
        winner = winners[0][1]
        row = conn.execute("SELECT status, escort_id FROM appointments WHERE id=?", (appt["id"],)).fetchone()
        self.assertEqual(row["status"], "assigned")
        self.assertEqual(row["escort_id"], winner)
        # 只有赢家拿到字段授权
        grants = conn.execute("SELECT escort_id FROM escort_grants WHERE appointment_id=?",
                              (appt["id"],)).fetchall()
        self.assertEqual([g["escort_id"] for g in grants], [winner])

    def test_terminal_cannot_be_accepted(self):
        conn = self.conn()
        es, _, appt, n = self.make_world(conn, scope=["sign_in"], notify=True)
        services.accept_appointment(conn, appt["id"], es[0], n)
        services.report_exception(conn, appt["id"], es[0], "cancelled", "科室停诊")
        with self.assertRaises(services.DomainError) as cm:
            services.accept_appointment(conn, appt["id"], es[1])
        self.assertEqual(cm.exception.code, "terminal")


class TestAuthorizationBoundary(ServiceTestBase):
    def test_fields_hidden_before_accept(self):
        from app.serializers import serialize_appointment
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        view = serialize_appointment(conn, appt, "escort", es[0])
        # 抢单前：只有匿名编号，无演示姓名/证件/附件
        self.assertNotIn("demo_name", view["patient"])
        self.assertEqual(view["patient"]["anon_code"], "T-3")
        self.assertIn("authorization_summary", view)
        self.assertEqual(view["attachments"], [])
        # 直接尝试授权校验 → 禁止
        with self.assertRaises(services.DomainError) as cm:
            services.authorize(conn, appt["id"], es[0], "sign_in")
        self.assertEqual(cm.exception.code, "forbidden_field")

    def test_in_scope_allowed_out_of_scope_to_manual(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn, scope=["sign_in", "queue"])
        services.accept_appointment(conn, appt["id"], es[0])
        self.assertTrue(services.authorize(conn, appt["id"], es[0], "queue")["allowed"])
        # 未授权的敏感操作：不执行，转人工
        with self.assertRaises(services.DomainError) as cm:
            services.authorize(conn, appt["id"], es[0], "view_report")
        self.assertEqual(cm.exception.code, "out_of_scope")
        self.assertEqual(cm.exception.extra["status"], "pending")
        mr = conn.execute("SELECT status, requested_action FROM manual_reviews WHERE id=?",
                          (cm.exception.extra["manual_review_id"],)).fetchone()
        self.assertEqual(mr["status"], "pending")
        self.assertIn("view_report", mr["requested_action"])

    def test_sensitive_attachment_minimum_privilege(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn, scope=["sign_in"])  # 不含 view_report
        aid = services.register_attachment(
            conn, appt["id"], "r.txt", "演示·CT报告", "sensitive", "view_report",
            b"demo report")
        services.accept_appointment(conn, appt["id"], es[0])
        # 列表可见但标记不可见+原因
        listed = services.list_attachments(conn, appt["id"], "escort", es[0])
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["visible"])
        # 读取被拦截并转人工
        with self.assertRaises(services.DomainError) as cm:
            services.read_attachment(conn, aid, "escort", es[0])
        self.assertEqual(cm.exception.code, "out_of_scope")
        # 协调员批准后，业务上仍需授权——这里验证协调员视角始终可见
        coord_list = services.list_attachments(conn, appt["id"], "coordinator")
        self.assertTrue(all(x["visible"] for x in coord_list))

    def test_sensitive_attachment_accessible_when_scope_present(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn, scope=["sign_in", "view_report"])
        aid = services.register_attachment(conn, appt["id"], "r.txt", "报告",
                                           "sensitive", "view_report", b"OK")
        services.accept_appointment(conn, appt["id"], es[0])
        _row, data = services.read_attachment(conn, aid, "escort", es[0])
        self.assertEqual(data, b"OK")

    def test_patient_view_never_contains_internal_note(self):
        from app.serializers import serialize_appointment
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        view = serialize_appointment(conn, appt, "patient")
        self.assertNotIn("internal_note", view)
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("勿对外", raw)


class TestStateOrder(ServiceTestBase):
    def _assigned(self, conn, es, scope=None):
        _, _, appt, n = self.make_world(conn, scope=scope, notify=True)
        services.accept_appointment(conn, appt["id"], es[0], n)
        return appt

    def test_checkin_must_follow_order(self):
        conn = self.conn()
        es, _, _, _ = self.make_world(conn)
        appt = self._assigned(conn, es)
        with self.assertRaises(services.DomainError) as cm:
            services.check_in(conn, appt["id"], es[0], "waiting", "登记处")
        self.assertEqual(cm.exception.code, "bad_order")
        # 正常推进
        services.check_in(conn, appt["id"], es[0], "arrived", "大厅")
        with self.assertRaises(services.DomainError) as cm:
            services.check_in(conn, appt["id"], es[0], "arrived", "大厅")
        self.assertEqual(cm.exception.code, "duplicate_stage")
        services.check_in(conn, appt["id"], es[0], "waiting", "登记处")
        services.check_in(conn, appt["id"], es[0], "exam_done", "机房")
        services.check_in(conn, appt["id"], es[0], "departed", "出口")
        row = services.get_appointment(conn, appt["id"])
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])

    def test_completed_cannot_reopen(self):
        conn = self.conn()
        es, _, _, _ = self.make_world(conn)
        appt = self._assigned(conn, es)
        for s, loc in [("arrived", "大厅"), ("waiting", "登记处"),
                       ("exam_done", "机房"), ("departed", "出口")]:
            services.check_in(conn, appt["id"], es[0], s, loc, notifier=self.notifier(conn))
        with self.assertRaises(services.DomainError) as cm:
            services.check_in(conn, appt["id"], es[0], "arrived", "大厅")
        self.assertEqual(cm.exception.code, "terminal")
        with self.assertRaises(services.DomainError) as cm:
            services.handoff(conn, appt["id"], es[0], "甲", "乙")
        self.assertEqual(cm.exception.code, "terminal")
        with self.assertRaises(services.DomainError) as cm:
            services.report_exception(conn, appt["id"], es[0], "reschedule",
                                      new_time=_today(15))
        self.assertEqual(cm.exception.code, "terminal")

    def test_handoff_requires_both_parties(self):
        conn = self.conn()
        es, _, _, _ = self.make_world(conn)
        appt = self._assigned(conn, es)
        with self.assertRaises(services.DomainError) as cm:
            services.handoff(conn, appt["id"], es[0], "  ", "家属")
        self.assertEqual(cm.exception.code, "handoff_party_required")
        row = services.handoff(conn, appt["id"], es[0], "导医台", "陪诊员甲",
                               "门诊大厅", ["就诊卡"])
        self.assertIsNotNone(row["created_at"])


class TestExceptions(ServiceTestBase):
    def test_reschedule_keeps_old_time(self):
        conn = self.conn()
        es = [conn.execute("INSERT INTO escorts(name) VALUES (?)", (n,)).lastrowid
              for n in ("改期陪诊甲", "改期陪诊乙")]
        _, _, appt, _ = self.make_world(conn)
        services.accept_appointment(conn, appt["id"], es[0])
        old = appt["scheduled_at"]
        r = services.report_exception(conn, appt["id"], es[0], "reschedule",
                                      "设备检修", new_time=_today(16))
        self.assertFalse(r["requires_manual"])
        row = services.get_appointment(conn, appt["id"])
        self.assertEqual(row["scheduled_at"], _today(16))
        exc = conn.execute("SELECT old_time,new_time FROM service_exceptions WHERE id=?",
                           (r["id"],)).fetchone()
        self.assertEqual(exc["old_time"], old)
        self.assertEqual(exc["new_time"], _today(16))
        # 改到更早应被拒
        _, _, appt2, _ = self.make_world(conn)
        services.accept_appointment(conn, appt2["id"], es[1])
        with self.assertRaises(services.DomainError):
            services.report_exception(conn, appt2["id"], es[1], "reschedule",
                                      new_time=_today(7))

    def test_cancel_and_refused_are_terminal(self):
        conn = self.conn()
        es = []
        for _ in range(2):
            cur = conn.execute("INSERT INTO escorts(name) VALUES (?)", ("e",))
            es.append(cur.lastrowid)
        for kind, status in [("cancelled", "cancelled"), ("patient_refused", "refused")]:
            _, _, appt, _ = self.make_world(conn)
            services.accept_appointment(conn, appt["id"], es[0])
            r = services.report_exception(conn, appt["id"], es[0], kind, "x")
            self.assertEqual(r["new_status"], status)
            self.assertEqual(services.get_appointment(conn, appt["id"])["status"], status)

    def test_route_change_manual_only_for_restricted_area(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        services.accept_appointment(conn, appt["id"], es[0])
        r = services.report_exception(conn, appt["id"], es[0], "route_changed",
                                      "电梯维保", new_route="住院部一层连廊→医技楼三层")
        self.assertFalse(r["requires_manual"])
        _, _, appt2, _ = self.make_world(conn)
        services.accept_appointment(conn, appt2["id"], es[0])
        r2 = services.report_exception(conn, appt2["id"], es[0], "route_changed",
                                       "需穿行病区", new_route="经住院病区通道→医技楼")
        self.assertTrue(r2["requires_manual"])
        pend = conn.execute("SELECT status FROM manual_reviews ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(pend["status"], "pending")

    def test_late_requires_positive_minutes(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        services.accept_appointment(conn, appt["id"], es[0])
        with self.assertRaises(services.DomainError):
            services.report_exception(conn, appt["id"], es[0], "escort_late",
                                      late_minutes=0)
        r = services.report_exception(conn, appt["id"], es[0], "escort_late",
                                      "堵车", late_minutes=18)
        self.assertEqual(r["type"], "escort_late")

    def test_docs_missing_goes_manual_and_resolve(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        services.accept_appointment(conn, appt["id"], es[0])
        r = services.report_exception(conn, appt["id"], es[0], "docs_missing", "缺既往片")
        self.assertTrue(r["requires_manual"])
        out = services.resolve_exception_manual(conn, r["id"], True)
        self.assertEqual(out["manual_status"], "approved")
        with self.assertRaises(services.DomainError):
            services.resolve_exception_manual(conn, r["id"], False)  # 已处理


class TestNotificationRetry(ServiceTestBase):
    def test_retries_then_succeeds(self):
        conn = self.conn()
        n = InProcessNotifier(conn, max_attempts=3, backoff=0, fail_pattern={"sms": 2})
        r = n.send(None, "sms", "patient:1", "到达提醒")
        self.assertEqual(r["status"], "sent")
        self.assertEqual(r["attempts"], 3)
        row = conn.execute("SELECT attempts,status FROM notifications WHERE id=?",
                           (r["id"],)).fetchone()
        self.assertEqual(row["attempts"], 3)

    def test_exhausted_marks_failed_and_raises_manual(self):
        conn = self.conn()
        _, _, appt, _ = self.make_world(conn)
        n = InProcessNotifier(conn, max_attempts=3, backoff=0, fail_pattern={"sms": 9})
        r = n.send(appt["id"], "sms", "patient:1", "离院提醒")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["attempts"], 3)
        mr = conn.execute("SELECT reason,status FROM manual_reviews").fetchall()
        self.assertTrue(any("通知发送失败" in x["reason"] for x in mr))

    def test_arrived_and_departed_trigger_patient_sms(self):
        conn = self.conn()
        es, _, appt, _ = self.make_world(conn)
        n = self.notifier(conn)
        services.accept_appointment(conn, appt["id"], es[0], n)
        services.check_in(conn, appt["id"], es[0], "arrived", "大厅", notifier=n)
        services.check_in(conn, appt["id"], es[0], "waiting", "登记处", notifier=n)
        services.check_in(conn, appt["id"], es[0], "exam_done", "机房", notifier=n)
        services.check_in(conn, appt["id"], es[0], "departed", "出口", notifier=n)
        sms = conn.execute("SELECT COUNT(*) c FROM notifications WHERE channel='sms'").fetchone()["c"]
        self.assertEqual(sms, 2)  # 到达、离院各一条；候诊/完成不打扰


class TestRouter(unittest.TestCase):
    def test_preset_and_alt_and_guide(self):
        r = InProcessRouter()
        route = r.plan("门诊大厅", "CT 3号机房")
        self.assertEqual(route[0], "门诊大厅")
        self.assertEqual(route[-1], "CT 3号机房")
        alt = r.plan("门诊大厅", "CT 3号机房", alt=True)
        self.assertIn("平坡绕行", "".join(alt))
        g0 = r.current_guide(route, "门诊大厅")
        self.assertFalse(g0["done"])
        g_end = r.current_guide(route, "CT 3号机房")
        self.assertTrue(g_end["done"])
        g_off = r.current_guide(route, "未知地点")
        self.assertTrue(g_off.get("off_route"))


class TestReviewRules(ServiceTestBase):
    def test_only_completed_reviewable_once(self):
        conn = self.conn()
        es, _, appt, n = self.make_world(conn, scope=["sign_in"], notify=True)
        services.accept_appointment(conn, appt["id"], es[0], n)
        with self.assertRaises(services.DomainError) as cm:
            services.submit_review(conn, appt["id"], 5)
        self.assertEqual(cm.exception.code, "not_completed")
        for s, loc in [("arrived", "大厅"), ("waiting", "登记处"),
                       ("exam_done", "机房"), ("departed", "出口")]:
            services.check_in(conn, appt["id"], es[0], s, loc, notifier=n)
        services.submit_review(conn, appt["id"], 5, ["准时"], "好")
        with self.assertRaises(services.DomainError) as cm:
            services.submit_review(conn, appt["id"], 4)
        self.assertEqual(cm.exception.code, "already_reviewed")
        summary = services.review_summary(conn, es[0])
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["avg"], 5.0)


class TestSeedIdempotent(ServiceTestBase):
    def test_seed_twice_stable(self):
        conn = self.conn()
        seed_demo(quiet=True)
        n1 = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]
        seed_demo(quiet=True)
        n2 = conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]
        self.assertEqual(n1, n2)
        self.assertGreaterEqual(n1, 6)


# ---------------------------------------------------------------- HTTP 层
class HttpTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="escort-http-")
        os.environ["ESCORT_DATA_DIR"] = cls.tmp
        config.DB_PATH = os.path.join(cls.tmp, "http.db")
        config.ATTACHMENT_DIR = os.path.join(cls.tmp, "attachments")
        init_db(config.DB_PATH)
        from app import server
        cls.server_mod = server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        server.STATE = server.AppState(fail_pattern={"sms": 0})
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.server_mod.STATE.fail_pattern = {"sms": 0}
        self.conn = get_conn(config.DB_PATH)
        self.tag = f"h{id(self)}"

    def tearDown(self):
        self.conn.close()

    def req(self, method, path, body=None, role="coordinator", escort_id=None, patient_id=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Role": role}
        if escort_id: headers["X-Escort-Id"] = str(escort_id)
        if patient_id: headers["X-Patient-Id"] = str(patient_id)
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def seed_open_appt(self, scope=None, patient_code=None):
        code = patient_code or f"HP-{abs(hash(self.tag)) % 100000}"
        status, p = self.req("POST", "/api/patients", {
            "anon_code": code, "demo_name": "演示·HTTP", "demo_id_masked": "110101********0000"})
        pid = p["id"]
        status, a = self.req("POST", "/api/appointments", {
            "patient_id": pid, "department": "放射科", "scheduled_at": _today(13),
            "exam_item": "CT", "mobility": "wheelchair",
            "authorization_scope": scope or ["sign_in", "queue", "exam_escort", "view_report"],
            "internal_note": "仅内部可见备注"})
        return pid, a["id"]

    def escort(self, name):
        cur = self.conn.execute("INSERT INTO escorts(name) VALUES (?)", (name,))
        return cur.lastrowid


class TestHttpAcceptRace(HttpTestBase):
    def test_http_concurrent_accept_one_winner(self):
        appt_pid, appt_id = self.seed_open_appt()
        escorts = [self.escort(f"并发陪诊{i}") for i in range(12)]
        barrier = threading.Barrier(12)
        out = []
        lock = threading.Lock()

        def worker(eid):
            barrier.wait()
            st, body = self.req("POST", f"/api/appointments/{appt_id}/accept", {},
                                role="escort", escort_id=eid)
            with lock:
                out.append((st, body.get("error", "ok")))

        ts = [threading.Thread(target=worker, args=(e,)) for e in escorts]
        for t in ts: t.start()
        for t in ts: t.join()
        ok = [x for x in out if x[0] == 200]
        taken = [x for x in out if x[1] == "already_taken"]
        self.assertEqual(len(ok), 1, out)
        self.assertEqual(len(taken), 11, out)
        st, detail = self.req("GET", f"/api/appointments/{appt_id}", role="escort",
                              escort_id=escorts[0])
        # 未抢到的陪诊员仍看不到患者身份
        self.assertNotIn("demo_name", detail["patient"])


class TestHttpPatientIsolation(HttpTestBase):
    def test_patient_cannot_see_others_appointment(self):
        p1, a1 = self.seed_open_appt(patient_code=f"ISO-A-{id(self)}")
        p2, a2 = self.seed_open_appt(patient_code=f"ISO-B-{id(self)}")
        # 患者2 直接访问患者1 的预约 → 403
        st, body = self.req("GET", f"/api/appointments/{a1}", role="patient", patient_id=p2)
        self.assertEqual(st, 403)
        self.assertEqual(body["error"], "isolated")
        # 列表只见本人
        st, listing = self.req("GET", "/api/appointments", role="patient", patient_id=p2)
        self.assertEqual(st, 200)
        ids = {i["id"] for i in listing["items"]}
        self.assertIn(a2, ids)
        self.assertNotIn(a1, ids)
        # 患者端任何详情中都不出现内部备注
        st, detail = self.req("GET", f"/api/appointments/{a2}", role="patient", patient_id=p2)
        self.assertNotIn("internal_note", detail)
        self.assertNotIn("仅内部可见备注", json.dumps(detail, ensure_ascii=False))


class TestHttpFlow(HttpTestBase):
    def test_full_flow_with_handoff_exception_and_manual(self):
        pid, appt_id = self.seed_open_appt(scope=["sign_in", "queue"])
        eid = self.escort("流程陪诊")
        # 接单前不可见
        st, before = self.req("GET", f"/api/appointments/{appt_id}", role="escort", escort_id=eid)
        self.assertNotIn("demo_name", before["patient"])
        # 接单
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/accept", {},
                         role="escort", escort_id=eid)
        self.assertEqual(st, 200)
        st, after = self.req("GET", f"/api/appointments/{appt_id}", role="escort", escort_id=eid)
        self.assertIn("demo_name", after["patient"])
        # 越权操作转人工
        st, denied = self.req("POST", f"/api/appointments/{appt_id}/authorize",
                              {"scope": "view_report"}, role="escort", escort_id=eid)
        self.assertEqual(st, 403)
        self.assertEqual(denied["error"], "out_of_scope")
        # 签到顺序错误
        st, bad = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                           {"stage": "departed", "location": "出口"},
                           role="escort", escort_id=eid)
        self.assertEqual(bad["error"], "bad_order")
        # 正确推进 + 交接留痕
        for stage, loc in [("arrived", "门诊大厅"), ("waiting", "放射科登记处")]:
            st, _ = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                             {"stage": stage, "location": loc}, role="escort", escort_id=eid)
            self.assertEqual(st, 200)
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/handoffs", {
            "from_party": "导医台", "to_party": "流程陪诊", "location": "门诊大厅",
            "materials": ["就诊卡"]}, role="escort", escort_id=eid)
        self.assertEqual(st, 201)
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                         {"stage": "exam_done", "location": "机房"},
                         role="escort", escort_id=eid)
        self.assertEqual(st, 200)
        # 资料缺失 → 人工队列
        st, exc = self.req("POST", f"/api/appointments/{appt_id}/exceptions",
                           {"type": "docs_missing", "detail": "缺申请单"},
                           role="escort", escort_id=eid)
        self.assertTrue(exc["requires_manual"])
        # 离院完成 → 不可重开
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                         {"stage": "departed", "location": "出口"},
                         role="escort", escort_id=eid)
        self.assertEqual(st, 200)
        st, reopen = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                              {"stage": "arrived"}, role="escort", escort_id=eid)
        self.assertEqual(reopen["error"], "terminal")
        # 患者端评价
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/reviews",
                         {"stars": 5, "tags": ["准时"]}, role="patient", patient_id=pid)
        self.assertEqual(st, 201)
        st, summ = self.req("GET", "/api/reviews/summary")
        self.assertGreaterEqual(summ["count"], 1)

    def test_notification_retry_via_server_state(self):
        self.server_mod.STATE.fail_pattern = {"sms": 2}
        pid, appt_id = self.seed_open_appt()
        eid = self.escort("通知陪诊")
        self.req("POST", f"/api/appointments/{appt_id}/accept", {},
                 role="escort", escort_id=eid)
        st, _ = self.req("POST", f"/api/appointments/{appt_id}/checkin",
                         {"stage": "arrived", "location": "大厅"},
                         role="escort", escort_id=eid)
        self.assertEqual(st, 200)
        row = self.conn.execute(
            "SELECT attempts,status FROM notifications WHERE channel='sms' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempts"], 3)  # 前两次模拟失败后重试成功


if __name__ == "__main__":
    unittest.main(verbosity=2)
