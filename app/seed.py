"""演示种子：单条命令即可创建预约、临时改期、交接等样例。

所有姓名、证件、联系方式、检查结果均为演示数据。
重复执行不会产生重复数据（按固定编号判断）。
"""
from datetime import datetime, timedelta

from . import services
from .adapters import InProcessNotifier
from .db import get_conn, init_db

# fail_pattern 置空：种子流程中的通知直接成功（重试逻辑由测试与 --fail-sms 演示）
def _notifier(conn):
    return InProcessNotifier(conn, fail_pattern={})


def _today(hour, minute=0, day_offset=0):
    d = datetime.now().replace(microsecond=0) + timedelta(days=day_offset)
    return d.replace(hour=hour, minute=minute, second=0).isoformat()


def seed_demo(quiet=False):
    init_db()
    conn = get_conn()
    created = {"appointments": [], "patients": [], "escorts": []}
    try:
        # 陪诊员
        escorts = [
            ("墨蓝陪诊·林岚", "138****2103"),
            ("墨蓝陪诊·赵岩", "139****7741"),
            ("墨蓝陪诊·孙禾", "137****0588"),
        ]
        escort_ids = []
        for name, phone in escorts:
            row = conn.execute("SELECT id FROM escorts WHERE name=?", (name,)).fetchone()
            if row:
                escort_ids.append(row["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO escorts(name,phone_masked,rating_avg,rating_count) VALUES (?, ?, 5.0, 0)",
                    (name, phone))
                escort_ids.append(cur.lastrowid)
        created["escorts"] = escort_ids
        e1, e2, e3 = escort_ids

        # 患者匿名档案（全部为演示数据）
        patients = [
            ("P-1001", "演示·林一", "110101********0011", "女", 62, "135****6621",
             "家属要求全程不要提及真实诊断名称；行动慢，优先直梯。"),
            ("P-1002", "演示·陈二", "310104********0022", "男", 71, "136****8840",
             "听力弱，沟通需面向患者放慢语速；无家属陪同。"),
            ("P-1003", "演示·王三", "440106********0033", "女", 48, "133****1192",
             "仅授权检查陪同，拒绝代取任何资料。"),
        ]
        patient_ids = []
        for code, name, idm, gender, age, phone, note in patients:
            row = conn.execute("SELECT id FROM patients WHERE anon_code=?", (code,)).fetchone()
            if row:
                patient_ids.append(row["id"])
            else:
                patient_ids.append(services.create_patient(
                    conn, code, name, idm, gender, age, phone, note))
        created["patients"] = patient_ids
        p1, p2, p3 = patient_ids

        notifier = _notifier(conn)

        def appt_exists(code):
            return conn.execute("SELECT 1 FROM appointments WHERE code=?", (code,)).fetchone()

        # 1) 已接单且接近完成的服务：到达→候诊→检查完成 已签到，含交接，等待“离院”
        if not appt_exists("A-DEMO-001"):
            a1 = services.create_appointment(
                conn, p1, "放射科", _today(9, 0), exam_item="胸部 CT 平扫",
                mobility="slow_walk", duration_minutes=120,
                authorization_scope=["sign_in", "queue", "exam_escort", "wheelchair",
                                     "receive_report", "view_report", "handoff_family"],
                internal_note="家属电话在备用联系单；检查后需在留观区休息 15 分钟。",
                code="A-DEMO-001")
            services.accept_appointment(conn, a1["id"], e1, notifier)
            services.check_in(conn, a1["id"], e1, "arrived", "门诊大厅", "已与患者汇合", notifier)
            services.check_in(conn, a1["id"], e1, "waiting", "放射科登记处", "等待叫号", notifier)
            services.handoff(conn, a1["id"], e1, "门诊导医台", "陪诊员林岚", "门诊大厅",
                             ["就诊卡", "CT 申请单", "演示·知情同意书"], "8:55 院内汇合交接")
            services.check_in(conn, a1["id"], e1, "exam_done", "CT 3号机房", "影像已完成", notifier)
            services.handoff(conn, a1["id"], e1, "CT 3号机房技师", "陪诊员林岚", "CT 3号机房",
                             ["演示·CT 影像袋（敏感）"], "技师交接影像资料")
            services.register_attachment(
                conn, a1["id"], "demo_CT_report.txt", "演示·胸部 CT 报告（演示数据）",
                "sensitive", "view_report",
                "【演示数据】胸部 CT 平扫未见明显异常。本文件非真实报告。".encode("utf-8"))
            services.register_attachment(
                conn, a1["id"], "demo_consent.txt", "演示·检查知情同意书",
                "normal", "", "【演示数据】患者已知情同意。".encode("utf-8"))
            created["appointments"].append(a1["id"])

        # 2) 已完成且已评价的历史服务
        if not appt_exists("A-DEMO-002"):
            a2 = services.create_appointment(
                conn, p2, "超声科", _today(8, 30, day_offset=-2), exam_item="腹部 B 超",
                mobility="wheelchair", authorization_scope=["sign_in", "queue", "exam_escort", "wheelchair"],
                internal_note="需空腹，已提醒。", code="A-DEMO-002")
            services.accept_appointment(conn, a2["id"], e1, notifier)
            for stage, loc, note in [
                ("arrived", "门诊大厅", ""), ("waiting", "超声科登记处", ""),
                ("exam_done", "超声 B 超 2 室", ""),
            ]:
                services.check_in(conn, a2["id"], e1, stage, loc, note, notifier)
            services.handoff(conn, a2["id"], e1, "陪诊员林岚", "家属陈先生", "门诊出口",
                             ["演示·超声报告", "随身包"], "离院交接")
            services.check_in(conn, a2["id"], e1, "departed", "门诊出口", "家属接走", notifier)
            services.submit_review(conn, a2["id"], 5, ["准时", "耐心", "路线熟"],
                                   "全程安排妥当，老人很安心（演示评价）。")

        # 3) 临时改期样例：已接单后改到当天下午
        if not appt_exists("A-DEMO-003"):
            a3 = services.create_appointment(
                conn, p3, "放射科", _today(10, 30), exam_item="腰椎 DR",
                mobility="none",
                authorization_scope=["sign_in", "queue", "exam_escort"],
                internal_note="患者明确拒绝代取报告。", code="A-DEMO-003")
            services.accept_appointment(conn, a3["id"], e2, notifier)
            services.check_in(conn, a3["id"], e2, "arrived", "门诊大厅", "", notifier)
            services.report_exception(
                conn, a3["id"], e2, "reschedule",
                "放射科设备临时检修，登记处协调改至下午",
                new_time=_today(15, 0), duration_minutes=90)
            created["appointments"].append(a3["id"])

        # 4) 开放中待抢单（明天）
        if not appt_exists("A-DEMO-004"):
            a4 = services.create_appointment(
                conn, p2, "心内科检查区", _today(9, 30, day_offset=1), exam_item="运动平板试验",
                mobility="slow_walk",
                authorization_scope=["sign_in", "queue", "exam_escort", "payment_assist"],
                internal_note="检查中需观察心率，提前备好既往演示报告。", code="A-DEMO-004")
            created["appointments"].append(a4["id"])

        # 5) 患者拒绝陪同样例（终态 refused，不可重新开启）
        if not appt_exists("A-DEMO-005"):
            a5 = services.create_appointment(
                conn, p3, "采血处", _today(11, 0, day_offset=1), exam_item="空腹采血",
                mobility="none", authorization_scope=["sign_in", "queue"],
                code="A-DEMO-005")
            services.accept_appointment(conn, a5["id"], e3, notifier)
            services.report_exception(conn, a5["id"], e3, "patient_refused",
                                      "患者临场表示只愿自行采血，拒绝陪同进入")

        # 6) 资料缺失转人工的进行中单
        if not appt_exists("A-DEMO-006"):
            a6 = services.create_appointment(
                conn, p1, "放射科", _today(14, 0, day_offset=2), exam_item="钼靶",
                mobility="slow_walk",
                authorization_scope=["sign_in", "queue", "exam_escort", "handoff_family"],
                code="A-DEMO-006")
            services.accept_appointment(conn, a6["id"], e2, notifier)
            services.report_exception(conn, a6["id"], e2, "docs_missing",
                                      "登记处提示既往对比演示片未带，是否先检查后补交")
            created["appointments"].append(a6["id"])

        if not quiet:
            print("演示样例已就绪：")
            print("  A-DEMO-001 放射科 CT（进行中，等待离院签到，含两次交接与敏感报告）")
            print("  A-DEMO-002 超声 B 超（已完成已评价）")
            print("  A-DEMO-003 腰椎 DR（临时改期至当天下午）")
            print("  A-DEMO-004 运动平板试验（开放待接单，可演示并发抢单）")
            print("  A-DEMO-005 采血（患者拒绝陪同，终态）")
            print("  A-DEMO-006 钼靶（资料缺失，已转人工确认）")
            print(f"  陪诊员ID: 林岚={e1} 赵岩={e2} 孙禾={e3}；患者匿名编号 P-1001/1002/1003")
        return created
    finally:
        conn.close()


if __name__ == "__main__":
    seed_demo()
