"""全局配置：默认使用工作区下的 data 目录，可通过环境变量覆盖。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ESCORT_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("ESCORT_DB_PATH", os.path.join(DATA_DIR, "escort.db"))
ATTACHMENT_DIR = os.path.join(DATA_DIR, "attachments")

HOST = os.environ.get("ESCORT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ESCORT_PORT", "8000"))

# 通知适配器重试参数（进程内模拟）
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_BACKOFF_SECONDS = 0.02  # 演示/测试环境用极短退避

# 检查时间线允许的签到阶段（顺序即状态机）
TIMELINE_STAGES = ["arrived", "waiting", "exam_done", "departed"]
STAGE_LABELS = {
    "arrived": "到达",
    "waiting": "候诊",
    "exam_done": "检查完成",
    "departed": "离院",
}

# 行动辅助要求选项
MOBILITY_OPTIONS = ["none", "wheelchair", "slow_walk", "blind_assist", "stretcher"]
MOBILITY_LABELS = {
    "none": "无需辅助",
    "wheelchair": "轮椅推送",
    "slow_walk": "慢行搀扶",
    "blind_assist": "视障引导",
    "stretcher": "平车转运",
}

# 预约异常类型
EXCEPTION_TYPES = [
    "reschedule",   # 临时改期
    "cancelled",    # 检查取消
    "patient_refused",  # 患者拒绝陪同
    "route_changed",    # 路线变更
    "escort_late",      # 陪诊员迟到
    "docs_missing",     # 资料缺失
]
EXCEPTION_LABELS = {
    "reschedule": "临时改期",
    "cancelled": "检查取消",
    "patient_refused": "患者拒绝陪同",
    "route_changed": "路线变更",
    "escort_late": "陪诊员迟到",
    "docs_missing": "资料缺失",
}
