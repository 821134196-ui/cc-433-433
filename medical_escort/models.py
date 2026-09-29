"""领域常量：状态、阶段、授权事项与异常类型。"""
from __future__ import annotations

# ---- 预约状态 ----
STATUS_SCHEDULED = "scheduled"        # 已发布待接单
STATUS_CLAIMED = "claimed"            # 已接单
STATUS_ARRIVED = "arrived"            # 已到达
STATUS_WAITING = "waiting"            # 候诊中
STATUS_IN_EXAM = "in_exam"            # 检查中
STATUS_COMPLETED = "completed"        # 检查完成
STATUS_DISCHARGED = "discharged"      # 已离院（终态）
STATUS_CANCELLED = "cancelled"        # 检查取消（终态）
STATUS_REJECTED = "rejected"          # 患者拒绝陪同（终态）
STATUS_RESCHEDULING = "rescheduling"  # 改期待确认（中间态）

TERMINAL_STATUSES = {STATUS_DISCHARGED, STATUS_CANCELLED, STATUS_REJECTED}

# 签到阶段 -> 签到达成后预约所处状态
STAGE_ORDER = ["arrived", "waiting", "in_exam", "completed", "discharged"]
STAGE_TO_STATUS = {
    "arrived": STATUS_ARRIVED,
    "waiting": STATUS_WAITING,
    "in_exam": STATUS_IN_EXAM,
    "completed": STATUS_COMPLETED,
    "discharged": STATUS_DISCHARGED,
}
STAGE_LABELS = {
    "arrived": "到达签到",
    "waiting": "候诊签到",
    "in_exam": "进入检查",
    "completed": "检查完成",
    "discharged": "离院交接",
}
STATUS_LABELS = {
    "scheduled": "待接单",
    "claimed": "已接单",
    "arrived": "已到达",
    "waiting": "候诊中",
    "in_exam": "检查中",
    "completed": "检查完成",
    "discharged": "已离院",
    "cancelled": "已取消",
    "rejected": "拒绝陪同",
    "rescheduling": "改期待确认",
}

# ---- 授权事项 ----
SCOPE_PICKUP = "pickup"            # 接送到院
SCOPE_QUEUE = "queue"              # 代取号排队
SCOPE_ACCOMPANY = "accompany_check"  # 陪同检查
SCOPE_REPORT = "med_report"        # 领取检查结果
SCOPE_PAYMENT = "payment"          # 代缴费用
SCOPE_MEDICATION = "medication"    # 代取药

SCOPE_LABELS = {
    SCOPE_PICKUP: "接送到院",
    SCOPE_QUEUE: "代取号排队",
    SCOPE_ACCOMPANY: "陪同检查",
    SCOPE_REPORT: "领取检查结果",
    SCOPE_PAYMENT: "代缴费用",
    SCOPE_MEDICATION: "代取药",
}
ALL_SCOPES = list(SCOPE_LABELS.keys())

# ---- 异常事件 ----
INCIDENT_RESCHEDULE = "reschedule"
INCIDENT_EXAM_CANCELLED = "exam_cancelled"
INCIDENT_PATIENT_REFUSED = "patient_refused"
INCIDENT_ROUTE_CHANGE = "route_change"
INCIDENT_ESCORT_LATE = "escort_late"
INCIDENT_MATERIAL_MISSING = "material_missing"
INCIDENT_LABELS = {
    INCIDENT_RESCHEDULE: "临时改期",
    INCIDENT_EXAM_CANCELLED: "检查取消",
    INCIDENT_PATIENT_REFUSED: "患者拒绝陪同",
    INCIDENT_ROUTE_CHANGE: "路线变更",
    INCIDENT_ESCORT_LATE: "陪诊员迟到",
    INCIDENT_MATERIAL_MISSING: "资料缺失",
}

# ---- 附件可见性 ----
VIS_DISPATCH = "dispatch"
VIS_ESCORT = "escort"
VIS_PATIENT = "patient"
VIS_ALL = "all"

ATTACHMENT_CATEGORY_LABELS = {
    "id_doc": "证件影像",
    "exam_result": "检查结果",
    "consent_form": "授权书",
    "other": "其他资料",
}
