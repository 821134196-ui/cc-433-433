"""进程内模拟院内路线适配器。

按科室与行动辅助要求生成路线（含无障碍分支与电梯/扶梯选择）；
路线变更生成新版本，旧版本保留并标记 active=0。
"""
from __future__ import annotations

import json
import time
from typing import Optional

WHEELCHAIR_KEYWORDS = ("轮椅", "平车", "行动不便")

DEPARTMENT_ROUTES = {
    "放射科": [
        ("门诊大厅", "凭预约码在自助机登记报到", 6),
        ("门诊一层放射分诊台", "提交检查申请单，领取候检号", 5),
        ("放射科 DR/CT 候诊区", "按广播叫号进入检查室", 15),
        ("放射科检查室", "配合技师完成检查", 10),
        ("放射科出口", "领取取片凭证，约定结果回传方式", 4),
    ],
    "超声科": [
        ("门诊大厅", "凭预约码在自助机登记报到", 6),
        ("二层超声分诊台", "提交申请单并核验检查前准备", 5),
        ("超声候诊区", "叫号后进入诊室", 20),
        ("超声诊室", "配合医师完成检查", 12),
    ],
    "检验科": [
        ("门诊大厅", "凭预约码登记并打印条码", 5),
        ("二层采血大厅", "排队叫号采血/留取标本", 10),
        ("标本回执窗口", "确认出报告时间与领取方式", 3),
    ],
    "心电图室": [
        ("门诊大厅", "自助机登记报到", 5),
        ("三层心电图分诊台", "提交申请单候检", 6),
        ("心电图检查室", "平卧配合检查", 8),
    ],
}

DEFAULT_ROUTE = [
    ("门诊大厅", "凭预约码在自助机登记报到", 6),
    ("相应楼层分诊台", "提交申请单并候诊", 8),
    ("检查科室", "按叫号进入完成检查", 15),
]


def _step(idx, place, action, est, wheelchair=False):
    s = {"seq": idx, "place": place, "action": action, "est_min": est}
    if wheelchair:
        if place == "门诊大厅":
            s["access"] = "走东侧无障碍通道，乘 1 号无障碍电梯"
        else:
            s["access"] = "全程使用无障碍电梯与坡道"
    else:
        s["access"] = "扶梯/步行"
    return s


def build_route(department: str, mobility_needs: str = "") -> list[dict]:
    wheelchair = any(k in (mobility_needs or "") for k in WHEELCHAIR_KEYWORDS)
    base = DEPARTMENT_ROUTES.get(department, DEFAULT_ROUTE)
    steps = [_step(i + 1, p, a, e, wheelchair) for i, (p, a, e) in enumerate(base)]
    # 行动辅助额外节点
    if wheelchair:
        steps.insert(
            1,
            _step(2, "门诊服务中心", "租借轮椅/平车并确认陪护交接", 8, True),
        )
        for s in steps[2:]:
            s["seq"] += 1
    return steps


def save_route(conn, appointment_id: int, department: str,
               mobility_needs: str = "", reason: str = "",
               overrides: Optional[list[dict]] = None) -> int:
    """写入新版本路线，旧版本置为 inactive。返回版本号。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM routes WHERE appointment_id=?",
        (appointment_id,),
    ).fetchone()
    version = row["v"] + 1
    steps = overrides or build_route(department, mobility_needs)
    conn.execute("UPDATE routes SET active=0 WHERE appointment_id=?", (appointment_id,))
    cur = conn.execute(
        "INSERT INTO routes(appointment_id,version,steps_json,reason,active,created_at)"
        " VALUES (?,?,?,?,1,?)",
        (appointment_id, version, json.dumps(steps, ensure_ascii=False),
         reason, time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return cur.lastrowid


def active_route(conn, appointment_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM routes WHERE appointment_id=? AND active=1 ORDER BY version DESC LIMIT 1",
        (appointment_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["steps"] = json.loads(d.pop("steps_json"))
    return d
