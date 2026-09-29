"""平台测试：接单竞争 / 授权边界 / 状态顺序 / 通知重试 / 患者隔离。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 每个测试会话使用独立数据目录
_TMP = tempfile.mkdtemp(prefix="me_test_")
os.environ["ME_DATA_DIR"] = _TMP

from medical_escort import config, db as dbmod, models, notifications, service  # noqa: E402
from medical_escort.server import Handler  # noqa: E402

config.DB_PATH = os.path.join(_TMP, "test.db")
config.ATTACHMENT_DIR = pathlib.Path(_TMP) / "attachments"
config.NOTIFY_BACKOFF_SECONDS = 0.0


def http_get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def http_get_raw(url: str):
    """用于非 JSON 响应（如附件下载）：返回 (status, bytes)。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post(url: str, body: dict):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class PlatformTestBase(unittest.TestCase):
    app: service.EscortApp = None
    httpd: ThreadingHTTPServer = None
    port: int = 0

    @classmethod
    def setUpClass(cls):
        if PlatformTestBase.httpd is not None:
            return  # 基类初始化只需一次（各子类共享同一套数据与服务）
        shutil.rmtree(_TMP, ignore_errors=True)
        config.ensure_dirs()
        cls.app = service.EscortApp(db_path=config.DB_PATH, async_notify=False)
        PlatformTestBase.app = cls.app
        conn = cls.app.connect()
        # 2 名患者 + 2 名陪诊员
        patients = {}
        for code, dn, fn in ("P-9001", "甲*", "甲某"), ("P-9002", "乙*", "乙某"):
            cur = conn.execute(
                "INSERT INTO patients(code,display_name,full_name,id_document,gender,"
                "age,contact_phone,mobility_note) VALUES (?,?,?,?,?,?,?,?)",
                (code, dn, fn, "11010119900101123X", "女", 60, "138****0000", "步行"))
            patients[code] = cur.lastrowid
        escorts = {}
        for code, name in ("E-901", "测试员甲"), ("E-902", "测试员乙"):
            cur = conn.execute(
                "INSERT INTO escorts(code,name,phone,status) VALUES (?,?,?,'active')",
                (code, name, "135****0000"))
            escorts[code] = cur.lastrowid
        conn.commit()
        conn.close()
        PlatformTestBase.patients = patients
        PlatformTestBase.escorts = escorts

        Handler.app = cls.app
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        PlatformTestBase.httpd = httpd
        PlatformTestBase.port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        PlatformTestBase.thread = t
        PlatformTestBase._appt_seq = 0
        notifications.reset_fail_budget(0)

    @classmethod
    def tearDownClass(cls):
        if cls is not PlatformTestBase and PlatformTestBase.httpd is not None:
            return
        if PlatformTestBase.httpd is not None:
            PlatformTestBase.httpd.shutdown()
            PlatformTestBase.httpd.server_close()
            PlatformTestBase.httpd = None

    def setUp(self):
        notifications.reset_fail_budget(0)

    # ---- 工厂 ----
    def _free_slot(self):
        """分配全局唯一的 20 分钟时间槽（跨天网格，22 分钟一格），杜绝用例间冲突。"""
        n = PlatformTestBase._appt_seq
        PlatformTestBase._appt_seq += 1
        day_off, idx = divmod(n, 40)
        d = (datetime.date.today() + datetime.timedelta(days=day_off)).isoformat()
        start_min = 8 * 60 + idx * 22
        end_min = start_min + 20
        sh, sm = divmod(start_min, 60)
        eh, em = divmod(end_min, 60)
        return (f"{d} {sh:02d}:{sm:02d}", f"{d} {eh:02d}:{em:02d}")

    def make_appt(self, patient_code="P-9001", scopes=None,
                  start=None, end=None, dept="检验科"):
        if start is None or end is None:
            dstart, dend = self._free_slot()
            start = start or dstart
            end = end or dend
        conn = self.app.connect()
        try:
            return service.create_appointment(
                conn, self.patients[patient_code], dept, "测试检查",
                start, end,
                mobility_needs="步行", special_requirements="空腹",
                internal_note="内部备注-不得对患者展示",
                scope_codes=scopes or [models.SCOPE_ACCOMPANY])
        finally:
            conn.close()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class TestClaimCompetition(PlatformTestBase):
    """同一预约并发接单：只允许一人成功。"""

    def test_concurrent_claims_exactly_one_wins(self):
        appt = self.make_appt()
        barrier = threading.Barrier(2)
        results = []

        def worker(escort_code):
            barrier.wait()
            try:
                service.claim_appointment(
                    self.app, appt, self.escorts[escort_code])
                results.append((escort_code, "ok"))
            except service.ConflictError as e:
                results.append((escort_code, f"conflict:{e}"))

        t1 = threading.Thread(target=worker, args=("E-901",))
        t2 = threading.Thread(target=worker, args=("E-902",))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [r for r in results if r[1] == "ok"]
        conflicts = [r for r in results if r[1].startswith("conflict")]
        self.assertEqual(len(oks), 1, f"应有且仅有一人接单成功，实际：{results}")
        self.assertEqual(len(conflicts), 1)

        conn = self.app.connect()
        try:
            row = conn.execute(
                "SELECT status, escort_id FROM appointments WHERE id=?",
                (appt,)).fetchone()
            self.assertEqual(row["status"], "claimed")
            self.assertIn(row["escort_id"], list(self.escorts.values()))
        finally:
            conn.close()

    def test_concurrent_claims_over_http(self):
        """端到端：两个 HTTP 请求同时 POST /claim。"""
        appt = self.make_appt()
        barrier = threading.Barrier(2)
        codes = []

        def worker(escort_id):
            barrier.wait()
            status, _ = http_post(
                f"{self.base()}/api/appointments/{appt}/claim",
                {"role": "escort", "escort_id": escort_id})
            codes.append(status)

        t1 = threading.Thread(target=worker, args=(self.escorts["E-901"],))
        t2 = threading.Thread(target=worker, args=(self.escorts["E-902"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(codes), [200, 409])

    def test_double_claim_second_rejected(self):
        appt = self.make_appt()
        service.claim_appointment(self.app, appt, self.escorts["E-901"])
        with self.assertRaises(service.ConflictError):
            service.claim_appointment(self.app, appt, self.escorts["E-902"])

    def test_overlapping_slot_same_escort_rejected(self):
        """同一陪诊员同一时段不可持有两个任务。"""
        far = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        a1 = self.make_appt(start=f"{far} 14:00", end=f"{far} 15:00")
        a2 = self.make_appt(start=f"{far} 14:30", end=f"{far} 15:30")
        service.claim_appointment(self.app, a1, self.escorts["E-901"])
        with self.assertRaises(service.ConflictError):
            service.claim_appointment(self.app, a2, self.escorts["E-901"])
        # 换另一个陪诊员可以接
        service.claim_appointment(self.app, a2, self.escorts["E-902"])


class TestAuthorizationBoundary(PlatformTestBase):
    """授权边界：越权必须转人工；敏感附件最小权限。"""

    def test_out_of_scope_action_creates_escalation(self):
        appt = self.make_appt(scopes=[models.SCOPE_ACCOMPANY])
        service.claim_appointment(self.app, appt, self.escorts["E-901"])
        conn = self.app.connect()
        try:
            with self.assertRaises(service.PermissionDenied):
                service.perform_scoped_action(
                    conn, appt, models.SCOPE_PAYMENT, "测试员甲", "家属请求代垫费用")
            esc = conn.execute(
                "SELECT * FROM escalations WHERE appointment_id=?",
                (appt,)).fetchone()
            self.assertIsNotNone(esc)
            self.assertEqual(esc["status"], "pending")
            self.assertEqual(esc["requested_scope"], models.SCOPE_PAYMENT)
            # 审核通过前，consents 里仍无该项
            self.assertRaises(
                service.PermissionDenied,
                lambda: service.ensure_scope(conn, appt, models.SCOPE_PAYMENT))
            # 人工批准 → 追加授权 → 动作放行
            service.resolve_escalation(conn, esc["id"], True, "调度台", "电话确认")
            service.ensure_scope(conn, appt, models.SCOPE_PAYMENT)
            conn.commit()
        finally:
            conn.close()

    def test_denied_escalation_keeps_boundary(self):
        appt = self.make_appt()
        conn = self.app.connect()
        try:
            with self.assertRaises(service.PermissionDenied):
                service.perform_scoped_action(
                    conn, appt, models.SCOPE_MEDICATION, "测试员甲", "想代取药")
            esc = conn.execute(
                "SELECT id FROM escalations WHERE appointment_id=?",
                (appt,)).fetchone()
            service.resolve_escalation(conn, esc["id"], False, "调度台", "无法核实")
            conn.commit()
            with self.assertRaises(service.PermissionDenied):
                service.ensure_scope(conn, appt, models.SCOPE_MEDICATION)
        finally:
            conn.close()

    def test_http_out_of_scope_returns_403_and_escalation_flag(self):
        appt = self.make_appt()
        service.claim_appointment(self.app, appt, self.escorts["E-901"])
        status, body = http_post(
            f"{self.base()}/api/appointments/{appt}/action",
            {"role": "escort", "escort_id": self.escorts["E-901"],
             "scope": "payment", "reason": "测试"})
        self.assertEqual(status, 403)
        self.assertTrue(body["escalation"])

    def test_attachment_visibility_matrix(self):
        appt = self.make_appt(scopes=[models.SCOPE_ACCOMPANY])
        service.claim_appointment(self.app, appt, self.escorts["E-901"])
        conn = self.app.connect()
        try:
            atts = {}
            atts["id_doc"] = dict(conn.execute(
                "SELECT * FROM attachments WHERE id=?",
                (service.save_attachment(
                    conn, appt, "id.txt", b"id", "id_doc", True,
                    models.VIS_DISPATCH),)).fetchone())
            atts["id_escort"] = dict(conn.execute(
                "SELECT * FROM attachments WHERE id=?",
                (service.save_attachment(
                    conn, appt, "id2.txt", b"id", "id_doc", True,
                    models.VIS_ESCORT),)).fetchone())
            atts["report_denied"] = dict(conn.execute(
                "SELECT * FROM attachments WHERE id=?",
                (service.save_attachment(
                    conn, appt, "r.txt", b"report", "exam_result", True,
                    models.VIS_ESCORT),)).fetchone())
            atts["form"] = dict(conn.execute(
                "SELECT * FROM attachments WHERE id=?",
                (service.save_attachment(
                    conn, appt, "c.txt", b"consent", "consent_form", False,
                    models.VIS_ALL),)).fetchone())
            appt_row = dict(service.get_appointment(conn, appt))
            eid = self.escorts["E-901"]
            other_eid = self.escorts["E-902"]

            # 调度台：全部可见
            for att in atts.values():
                self.assertTrue(service.can_see_attachment(
                    "dispatch", att, appt_row, None))
            # 未接单的其他陪诊员：一律不可见
            for att in atts.values():
                self.assertFalse(service.can_see_attachment(
                    "escort", att, appt_row, other_eid))
            # 接单陪诊员：dispatch-only 证件不可见；escort 证件可见；
            # 检查结果未授权 med_report 不可见；授权书 all 可见
            self.assertFalse(service.can_see_attachment(
                "escort", atts["id_doc"], appt_row, eid))
            self.assertTrue(service.can_see_attachment(
                "escort", atts["id_escort"], appt_row, eid))
            self.assertFalse(service.can_see_attachment(
                "escort", atts["report_denied"], appt_row, eid,
                {models.SCOPE_ACCOMPANY}))
            self.assertTrue(service.can_see_attachment(
                "escort", atts["report_denied"], appt_row, eid,
                {models.SCOPE_REPORT}))
            self.assertTrue(service.can_see_attachment(
                "escort", atts["form"], appt_row, eid))
            # 患者：仅 patient/all 可见
            self.assertFalse(service.can_see_attachment(
                "patient", atts["id_doc"], appt_row, None))
            self.assertTrue(service.can_see_attachment(
                "patient", atts["form"], appt_row, None))
        finally:
            conn.close()

    def test_pre_claim_escort_sees_minimal_fields(self):
        appt = self.make_appt()
        conn = self.app.connect()
        try:
            row = dict(service.get_appointment(conn, appt))
            # 未接单陪诊员
            d = service.serialize_appointment(
                conn, row, "escort", self.escorts["E-902"])
            self.assertNotIn("special_requirements", d)
            self.assertNotIn("internal_note", d)
            self.assertNotIn("contact_phone", d)
            self.assertNotIn("consents", d)
            # 接单后必要字段可见
            conn2 = self.app.connect()
            try:
                service.claim_appointment(self.app, appt, self.escorts["E-901"])
            finally:
                conn2.close()
            row = dict(service.get_appointment(conn, appt))
            d = service.serialize_appointment(
                conn, row, "escort", self.escorts["E-901"])
            self.assertIn("special_requirements", d)
            self.assertIn("contact_phone", d)
            self.assertIn("consents", d)
            # 证件号仍掩码
            self.assertIn("*", d["id_document"])
        finally:
            conn.close()


class TestStatusOrder(PlatformTestBase):
    """到达→候诊→检查完成→离院逐步签到，终态不可重新开启。"""

    def _claimed(self):
        appt = self.make_appt()
        service.claim_appointment(self.app, appt, self.escorts["E-901"])
        return appt

    def test_happy_path_sequence(self):
        appt = self._claimed()
        for stage, expect in [("arrived", "arrived"), ("waiting", "waiting"),
                              ("in_exam", "in_exam"), ("completed", "completed"),
                              ("discharged", "discharged")]:
            service.checkin(self.app, appt, self.escorts["E-901"], stage,
                            location="地点")
            conn = self.app.connect()
            try:
                self.assertEqual(
                    service.get_appointment(conn, appt)["status"], expect)
            finally:
                conn.close()

    def test_skip_stage_rejected(self):
        appt = self._claimed()
        with self.assertRaises(service.ConflictError):
            service.checkin(self.app, appt, self.escorts["E-901"], "waiting")
        service.checkin(self.app, appt, self.escorts["E-901"], "arrived")
        with self.assertRaises(service.ConflictError):
            service.checkin(self.app, appt, self.escorts["E-901"], "completed")

    def test_terminal_cannot_reopen(self):
        appt = self._claimed()
        for stage in ("arrived", "waiting", "in_exam", "completed", "discharged"):
            service.checkin(self.app, appt, self.escorts["E-901"], stage)
        # 已离院：不能再签到、改期、新增交接
        with self.assertRaises(service.ConflictError):
            service.checkin(self.app, appt, self.escorts["E-901"], "waiting")
        with self.assertRaises(service.ConflictError):
            service.reschedule(self.app, appt,
                               f"{time.strftime('%Y-%m-%d')} 16:00",
                               f"{time.strftime('%Y-%m-%d')} 17:00", "尝试重开")
        conn = self.app.connect()
        try:
            with self.assertRaises(service.ConflictError):
                service.add_handoff(conn, appt, "甲", "乙", "地点")
        finally:
            conn.close()

    def test_cancelled_is_terminal(self):
        appt = self._claimed()
        service.cancel_exam(self.app, appt, "设备故障")
        with self.assertRaises(service.ConflictError):
            service.checkin(self.app, appt, self.escorts["E-901"], "arrived")
        with self.assertRaises(service.ConflictError):
            service.reschedule(self.app, appt,
                               f"{time.strftime('%Y-%m-%d')} 16:00",
                               f"{time.strftime('%Y-%m-%d')} 17:00", "x")

    def test_duplicate_checkin_is_idempotent(self):
        appt = self._claimed()
        service.checkin(self.app, appt, self.escorts["E-901"], "arrived")
        service.checkin(self.app, appt, self.escorts["E-901"], "arrived")  # 不报错
        conn = self.app.connect()
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM checkins WHERE appointment_id=? AND stage='arrived'",
                (appt,)).fetchone()["c"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_non_assigned_escort_cannot_checkin(self):
        appt = self._claimed()
        with self.assertRaises(service.PermissionDenied):
            service.checkin(self.app, appt, self.escorts["E-902"], "arrived")

    def test_handoff_requires_parties_and_location(self):
        appt = self._claimed()
        conn = self.app.connect()
        try:
            with self.assertRaises(service.ServiceError):
                service.add_handoff(conn, appt, "", "乙", "地点")
            with self.assertRaises(service.ServiceError):
                service.add_handoff(conn, appt, "甲", "乙", "")
            conn.commit()
        finally:
            conn.close()

    def test_reschedule_releases_escort_and_returns_to_scheduled(self):
        appt = self._claimed()
        t = time.strftime("%Y-%m-%d")
        service.reschedule(self.app, appt, f"{t} 16:00", f"{t} 17:00", "改期测试")
        conn = self.app.connect()
        try:
            row = service.get_appointment(conn, appt)
            self.assertEqual(row["status"], "scheduled")
            self.assertIsNone(row["escort_id"])
            self.assertEqual(row["version"], 1)
            inc = conn.execute(
                "SELECT * FROM incidents WHERE appointment_id=? AND kind='reschedule'",
                (appt,)).fetchone()
            self.assertIsNotNone(inc)
        finally:
            conn.close()


class TestNotificationRetry(PlatformTestBase):
    """通知适配器：失败注入、退避重试、次数留痕、终态。"""

    def test_retry_succeeds_after_transient_failures(self):
        notifications.reset_fail_budget(2)  # 前两次失败，第三次成功
        conn = self.app.connect()
        try:
            nid = notifications.enqueue(conn, None, "sms", "138****0000", "测试通知")
            ok = notifications.send_now(conn, nid, max_attempts=4, backoff=0)
            self.assertTrue(ok)
            row = conn.execute(
                "SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertEqual(row["attempts"], 3)
            self.assertTrue(row["sent_at"])
        finally:
            conn.close()

    def test_retry_exhausted_marks_failed(self):
        notifications.reset_fail_budget(99)
        conn = self.app.connect()
        try:
            nid = notifications.enqueue(conn, None, "sms", "x", "y")
            ok = notifications.send_now(conn, nid, max_attempts=3, backoff=0)
            self.assertFalse(ok)
            row = conn.execute(
                "SELECT status, attempts, last_error FROM notifications WHERE id=?",
                (nid,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], 3)
            self.assertIn("503", row["last_error"])
        finally:
            conn.close()

    def test_drain_pending_retries_all(self):
        notifications.reset_fail_budget(1)
        conn = self.app.connect()
        try:
            n1 = notifications.enqueue(conn, None, "sms", "a", "1")
            n2 = notifications.enqueue(conn, None, "sms", "b", "2")
            conn.commit()
            # 只重发本用例入队的两条（其他用例可能留有 pending 工单通知）
            for nid in (n1, n2):
                self.assertTrue(notifications.send_now(conn, nid, backoff=0))
            for nid in (n1, n2):
                self.assertEqual(conn.execute(
                    "SELECT status FROM notifications WHERE id=?",
                    (nid,)).fetchone()["status"], "sent")
        finally:
            conn.close()


class TestPatientIsolation(PlatformTestBase):
    """患者只能看本人数据；内部备注永不展示；跨患者访问 403。"""

    def test_patient_lists_only_own_appointments(self):
        self.make_appt("P-9001")
        self.make_appt("P-9002")
        status, body = http_get(
            f"{self.base()}/api/appointments?role=patient&patient_code=P-9001")
        self.assertEqual(status, 200)
        codes = {a["patient_code"] for a in body["items"]}
        self.assertEqual(codes, {"P-9001"})

    def test_patient_cannot_access_other_appointment(self):
        other = self.make_appt("P-9002")
        status, body = http_get(
            f"{self.base()}/api/appointments/{other}"
            "?role=patient&patient_code=P-9001")
        self.assertEqual(status, 403)
        self.assertIn("隔离", body["error"])

    def test_internal_note_hidden_from_patient(self):
        appt = self.make_appt("P-9001")
        status, body = http_get(
            f"{self.base()}/api/appointments/{appt}"
            "?role=patient&patient_code=P-9001")
        self.assertEqual(status, 200)
        payload = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("内部备注", payload)
        self.assertNotIn("internal_note", body)
        # 调度台可见
        status, body = http_get(
            f"{self.base()}/api/appointments/{appt}?role=dispatch")
        self.assertIn("内部备注", body["internal_note"])

    def test_patient_cannot_reach_staff_apis(self):
        for path in ("/api/patients", "/api/escalations", "/api/escorts"):
            status, _ = http_get(
                f"{self.base()}{path}?role=patient&patient_code=P-9001")
            self.assertEqual(status, 403, path)

    def test_patient_cannot_create_or_claim(self):
        appt = self.make_appt("P-9002")
        status, _ = http_post(f"{self.base()}/api/appointments",
                              {"role": "patient", "patient_code": "P-9001"})
        self.assertEqual(status, 403)
        status, _ = http_post(
            f"{self.base()}/api/appointments/{appt}/claim",
            {"role": "patient", "patient_code": "P-9001"})
        self.assertEqual(status, 403)

    def test_cross_patient_attachment_download_denied(self):
        appt = self.make_appt("P-9002")
        conn = self.app.connect()
        try:
            att_id = service.save_attachment(
                conn, appt, "r.txt", "报告".encode(), "consent_form",
                False, models.VIS_ALL)
            conn.commit()
        finally:
            conn.close()
        status, body = http_get_raw(
            f"{self.base()}/api/attachments/{att_id}/download"
            "?role=patient&patient_code=P-9001")
        self.assertEqual(status, 403)
        self.assertIn("隔离".encode(), body)
        # 本人可下载
        status, body = http_get_raw(
            f"{self.base()}/api/attachments/{att_id}/download"
            "?role=patient&patient_code=P-9002")
        self.assertEqual(status, 200)
        self.assertEqual(body, "报告".encode())

    def test_unknown_patient_code_denied(self):
        status, _ = http_get(
            f"{self.base()}/api/appointments?role=patient&patient_code=P-NOPE")
        self.assertEqual(status, 403)

    def test_staff_list_endpoints_ok(self):
        """回归：调度台可正常拉取工单与异常事件列表。"""
        status, body = http_get(f"{self.base()}/api/escalations?role=dispatch")
        self.assertEqual(status, 200)
        self.assertIn("items", body)
        status, body = http_get(f"{self.base()}/api/incidents?role=dispatch")
        self.assertEqual(status, 200)
        self.assertIn("items", body)
        status, body = http_get(f"{self.base()}/api/notifications?role=dispatch")
        self.assertEqual(status, 200)
        self.assertIn("items", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
