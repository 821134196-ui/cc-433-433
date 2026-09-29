"""全局配置：数据文件、附件目录均放在本地工作目录下。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ME_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "escort.db"
ATTACHMENT_DIR = DATA_DIR / "attachments"

# 服务端口
PORT = int(os.environ.get("ME_PORT", "8000"))

# 通知适配器：允许通过环境变量注入失败率，方便演示“通知重试”
NOTIFY_FAIL_FIRST_N = int(os.environ.get("ME_NOTIFY_FAIL_FIRST_N", "0"))
NOTIFY_MAX_ATTEMPTS = int(os.environ.get("ME_NOTIFY_MAX_ATTEMPTS", "4"))
NOTIFY_BACKOFF_SECONDS = float(os.environ.get("ME_NOTIFY_BACKOFF_SECONDS", "0.05"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
