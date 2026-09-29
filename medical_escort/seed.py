"""演示种子数据：患者匿名档案、陪诊员、服务时段、预约（含改期/交接/异常）、
敏感附件、评价与一条越权转人工样例。

所有姓名、证件号、检查结果均为虚构演示数据。
"""
from __future__ import annotations

import datetime as dt

from . import models, notifications, routing, service


def _day(offset: int, hm: str) -> str:
    d = dt.date.today() + dt.timedelta(days=offset)
    return f"{d.isoformat()} {hm}"


def seed(app: service.EscortApp, verbose: bool = True) -> dict:
    conn = app.connect()
    # 幂等：已有预约则跳过
    if conn.execute("SELECT COUNT(*) AS c FROM appointments").fetchone()["c"]:
        if verbose:
            print("[seed] 数据库已有数据，跳过种子。")
        conn.close()
        return {}

    try:
        # ---- 患者匿名档案（演示数据） ----
        patients = [
            # code, 脱敏名, 真实姓名, 证件号, 性别, 年龄, 电话, 行动
            ("P-1001", "王*", "王秀兰", "110101194203071234", "女", 83,
             "138****0217", "轮椅；长距离移动困难"),
            ("P-1002", "李*", "李建国", "310104195811224321", "男", 67,
             "139****8851", "步行缓慢；需搀扶上下检查床"),
            ("P-1003", "张*", "张敏", "440106198705147652", "女", 38,
             "137****3340", "步行；视力障碍需引导"),
            ("P-1004", "赵*", "赵鸿志", "120102193609288432", "男", 89,
             "136****6092", "平车；卧床"),
        ]
        patient_ids = {}
        for code, dn, fn, doc, g, age, phone, mob in patients:
            cur = conn.execute(
                "INSERT INTO patients(code,display_name,full_name,id_document,"
                "gender,age,contact_phone,mobility_note) VALUES (?,?,?,?,?,?,?,?)",
                (code, dn, fn, doc, g, age, phone, mob))
            patient_ids[code] = cur.lastrowid

        # ---- 陪诊员 ----
        escorts = [
            ("E-201", "陈岭", "135****1101", "active"),
            ("E-202", "林悦", "135****2202", "active"),
            ("E-203", "高航", "135****3303", "active"),
        ]
        escort_ids = {}
        for code, name, phone, st in escorts:
            cur = conn.execute(
                "INSERT INTO escorts(code,name,phone,status) VALUES (?,?,?,?)",
                (code, name, phone, st))
            escort_ids[code] = cur.lastrowid

        # ---- 服务时段（覆盖今天与明天） ----
        for eid in escort_ids.values():
            for off in (0, 1, 2):
                conn.execute(
                    "INSERT OR IGNORE INTO service_slots(escort_id,slot_start,slot_end)"
                    " VALUES (?,?,?)",
                    (eid, _day(off, "08:30"), _day(off, "12:00")))
                conn.execute(
                    "INSERT OR IGNORE INTO service_slots(escort_id,slot_start,slot_end)"
                    " VALUES (?,?,?)",
                    (eid, _day(off, "13:30"), _day(off, "17:30")))

        ids = {}

        # 1) 已完成并评价的全流程样例（昨天，陈岭）
        a1 = service.create_appointment(
            conn, patient_ids["P-1002"], "超声科", "腹部 B 超",
            _day(-1, "09:00"), _day(-1, "10:00"),
            mobility_needs="步行缓慢；需搀扶",
            special_requirements="空腹 8 小时；携带既往报告",
            internal_note="患者听力较弱，叫号时需主动提醒。",
            scope_codes=[models.SCOPE_PICKUP, models.SCOPE_QUEUE,
                         models.SCOPE_ACCOMPANY, models.SCOPE_REPORT])
        ids["completed"] = a1
        conn.execute(
            "UPDATE appointments SET status='claimed', escort_id=?, claimed_at=? WHERE id=?",
            (escort_ids["E-201"], service.now_str(), a1))
        service.add_handoff(conn, a1, "调度台", "陪诊员-陈岭", "门诊一层东门",
                            "检查申请单、医保卡", True, "晨间交接")
        for stage, loc, note in [
            ("arrived", "门诊一层东门", "接到患者，轮椅备用"),
            ("waiting", "二层超声候诊区", "已取号 B117"),
        ]:
            conn.execute(
                "INSERT INTO checkins(appointment_id,stage,note,actor_id,location,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (a1, stage, note, escort_ids["E-201"], loc, service.now_str()))
            conn.execute(
                "UPDATE appointments SET status=?, updated_at=? WHERE id=?",
                (models.STAGE_TO_STATUS[stage], service.now_str(), a1))
        service.add_handoff(conn, a1, "陪诊员-陈岭", "超声科分诊护士",
                            "二层超声分诊台", "申请单条码 B117", True)
        for stage, loc, note in [
            ("in_exam", "超声 3 诊室", "家属已在外等候"),
            ("completed", "超声 3 诊室", "检查顺利，约 2 小时出报告"),
        ]:
            conn.execute(
                "INSERT INTO checkins(appointment_id,stage,note,actor_id,location,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (a1, stage, note, escort_ids["E-201"], loc, service.now_str()))
            conn.execute(
                "UPDATE appointments SET status=?, updated_at=? WHERE id=?",
                (models.STAGE_TO_STATUS[stage], service.now_str(), a1))
        service.add_handoff(conn, a1, "陪诊员-陈岭", "家属-李先生",
                            "门诊一层落客区", "随身物品、取片凭证", True,
                            "离院交接，报告代领后电话通知")
        conn.execute(
            "INSERT INTO checkins(appointment_id,stage,note,actor_id,location,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (a1, "discharged", "交回家属，报告授权代领后将转交",
             escort_ids["E-201"], "门诊一层落客区", service.now_str()))
        conn.execute(
            "UPDATE appointments SET status='discharged', updated_at=?, completed_at=? WHERE id=?",
            (service.now_str(), service.now_str(), a1))
        service.submit_review(conn, a1, 5, "准时；耐心；交接清晰",
                              "陈师傅全程搀扶，老人很安心。")
        # 检查结果附件：授权代领 → 调度台与接单陪诊员可见
        service.save_attachment(
            conn, a1, "腹部B超报告_演示.txt",
            "【演示数据】腹部 B 超：未见明显异常（本报告为虚构）。".encode(),
            "exam_result", True, models.VIS_ALL)
        service.save_attachment(
            conn, a1, "授权书_演示.txt",
            "【演示】患者授权接送、取号、陪同检查、代领报告。".encode(),
            "consent_form", False, models.VIS_ALL)

        # 2) 今天 · 检查中（陈岭，轮椅患者），含交接 + 迟到 + 资料缺失 + 路线变更
        a2 = service.create_appointment(
            conn, patient_ids["P-1001"], "放射科", "胸部 CT 平扫",
            _day(0, "09:30"), _day(0, "11:00"),
            mobility_needs="轮椅；院内全程坡道",
            special_requirements="带既往影像片袋；无需空腹",
            internal_note="患者易焦虑，沟通放慢语速；家属要求检查后电话反馈。",
            scope_codes=[models.SCOPE_PICKUP, models.SCOPE_QUEUE,
                         models.SCOPE_ACCOMPANY])
        ids["in_exam"] = a2
        conn.execute(
            "UPDATE appointments SET status='claimed', escort_id=?, claimed_at=? WHERE id=?",
            (escort_ids["E-201"], service.now_str(), a2))
        for stage, loc in [("arrived", "门诊一层东门"),
                           ("waiting", "一层放射候诊区"),
                           ("in_exam", "CT 2 室")]:
            conn.execute(
                "INSERT INTO checkins(appointment_id,stage,note,actor_id,location,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (a2, stage, "", escort_ids["E-201"], loc, service.now_str()))
            conn.execute(
                "UPDATE appointments SET status=?, updated_at=? WHERE id=?",
                (models.STAGE_TO_STATUS[stage], service.now_str(), a2))
        service.add_handoff(conn, a2, "调度台", "陪诊员-陈岭", "门诊一层东门",
                            "申请单、医保卡、轮椅一台", True, "患者情绪平稳")
        service.add_handoff(conn, a2, "陪诊员-陈岭", "放射科技师",
                            "CT 2 室", "检查申请单", True, "进入扫描间前交接")
        service.report_escort_late(conn, a2, 12, "早高峰院内道路拥堵，已电话致歉")
        service.report_material_missing(conn, a2, "既往 CT 影像片袋",
                                        "家属忘带，已联系下午前送到，不影响本次平扫")
        routing.save_route(conn, a2, "放射科", "轮椅；院内全程坡道",
                           reason="一层东侧无障碍通道临时封闭，改走西门")
        service._log_incident(conn, a2, models.INCIDENT_ROUTE_CHANGE,
                              "一层东侧无障碍通道临时封闭，改走西门", "v1", "v2")
        # 证件影像：高敏感，仅调度台可见
        service.save_attachment(
            conn, a2, "身份证影像_演示.txt",
            "【演示数据】身份证正反面影像（虚构，仅调度台最小权限可见）。".encode(),
            "id_doc", True, models.VIS_DISPATCH)
        service.save_attachment(
            conn, a2, "检查须知_演示.txt",
            "【演示】CT 检查前去除金属物品，陪同孕妇请勿进入扫描间。".encode(),
            "other", False, models.VIS_ESCORT)

        # 3) 待接单（今天下午，视力障碍患者）——并发接单测试目标
        a3 = service.create_appointment(
            conn, patient_ids["P-1003"], "检验科", "血常规+肝肾功能",
            _day(0, "14:00"), _day(0, "15:00"),
            mobility_needs="步行；视力障碍需口头引导",
            special_requirements="空腹采血",
            internal_note="患者独自前来，结束后需引导至出租车上客点。",
            scope_codes=[models.SCOPE_PICKUP, models.SCOPE_QUEUE,
                         models.SCOPE_ACCOMPANY])
        ids["scheduled"] = a3

        # 4) 明天 · 临时改期样例（改期后回到待接单，version=1）
        a4 = service.create_appointment(
            conn, patient_ids["P-1004"], "心电图室", "常规心电图",
            _day(1, "09:00"), _day(1, "09:40"),
            mobility_needs="平车；卧床",
            special_requirements="检查前静息 10 分钟",
            internal_note="平车需提前与物业协调电梯。",
            scope_codes=[models.SCOPE_PICKUP, models.SCOPE_ACCOMPANY])
        ids["rescheduled"] = a4
        conn.execute(
            "UPDATE appointments SET status='claimed', escort_id=?, claimed_at=? WHERE id=?",
            (escort_ids["E-203"], service.now_str(), a4))
        conn.execute(
            "UPDATE appointments SET status='rescheduling', updated_at=? WHERE id=?",
            (service.now_str(), a4))
        service._log_incident(conn, a4, models.INCIDENT_RESCHEDULE,
                              "科室设备检修，检查顺延", _day(1, "09:00"),
                              _day(2, "10:30"))
        conn.execute(
            "UPDATE appointments SET status='scheduled', scheduled_start=?,"
            " scheduled_end=?, escort_id=NULL, claimed_at=NULL, version=1,"
            " updated_at=? WHERE id=?",
            (_day(2, "10:30"), _day(2, "11:10"), service.now_str(), a4))
        service.save_attachment(
            conn, a4, "心电图申请单_演示.txt",
            "【演示】常规心电图申请单（虚构）。".encode(),
            "other", False, models.VIS_DISPATCH)

        # 5) 检查取消终态样例（明天）
        a5 = service.create_appointment(
            conn, patient_ids["P-1003"], "超声科", "甲状腺超声",
            _day(1, "15:00"), _day(1, "15:40"),
            mobility_needs="步行；视力障碍需引导",
            special_requirements="颈部勿佩戴饰品",
            internal_note="",
            scope_codes=[models.SCOPE_QUEUE, models.SCOPE_ACCOMPANY])
        ids["cancelled"] = a5
        conn.execute(
            "UPDATE appointments SET status='cancelled', cancel_reason=?,"
            " updated_at=? WHERE id=?",
            ("医生停诊，检查取消，费用原路退回", service.now_str(), a5))
        service._log_incident(conn, a5, models.INCIDENT_EXAM_CANCELLED,
                              "医生停诊")

        # 6) 患者拒绝陪同终态样例（今天上午，高航已接单后拒绝）
        a6 = service.create_appointment(
            conn, patient_ids["P-1002"], "检验科", "糖化血红蛋白",
            _day(0, "10:30"), _day(0, "11:10"),
            mobility_needs="步行缓慢",
            special_requirements="无需空腹",
            internal_note="",
            scope_codes=[models.SCOPE_ACCOMPANY])
        ids["rejected"] = a6
        conn.execute(
            "UPDATE appointments SET status='claimed', escort_id=?, claimed_at=? WHERE id=?",
            (escort_ids["E-202"], service.now_str(), a6))
        conn.execute(
            "UPDATE appointments SET status='rejected', cancel_reason=?,"
            " updated_at=? WHERE id=?",
            ("患者拒绝陪同：到场后患者表示女儿已临时赶来陪同",
             service.now_str(), a6))
        service._log_incident(conn, a6, models.INCIDENT_PATIENT_REFUSED,
                              "到场后患者表示女儿已临时赶来陪同")

        # 7) 越权转人工样例：陈岭在 a2 上尝试代缴费用（未授权）→ 工单待处理
        try:
            service.perform_scoped_action(
                conn, a2, models.SCOPE_PAYMENT, "陪诊员 陈岭",
                "检查费需补缴，家属电话请求代垫但授权书未含代缴费用")
        except service.PermissionDenied:
            pass  # 预期：已生成 pending escalation

        # ---- 通知：前两次发送失败以演示重试（由调用方 drain） ----
        notifications.enqueue(conn, a3, "sms", "137****3340",
                              "【康柏陪诊】您明日 14:00 检验科检查已安排陪诊，请保持电话畅通。")
        notifications.enqueue(conn, None, "dispatch", "调度台",
                              "【系统】明日平车心电图预约已改期，请重新派单。")

        conn.commit()
        if verbose:
            print("[seed] 演示数据写入完成：")
            print(f"  - 患者 {len(patients)} 人，陪诊员 {len(escorts)} 人")
            print(f"  - 预约 6 条：检查中 {a2}、待接单 {a3}、改期 {a4}、"
                  f"取消 {a5}、拒绝陪同 {a6}、已完成 {a1}")
            print("  - 交接记录、签到时间线、迟到/资料缺失/路线变更事件已留痕")
            print("  - 敏感附件按 dispatch/escort/patient 分级可见")
            print("  - 1 条越权代缴费用申请已进入人工确认队列")
    finally:
        conn.close()
    return ids
