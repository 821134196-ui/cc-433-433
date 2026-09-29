"""进程内模拟通知适配器。

- 落库 notifications 表；支持失败注入（ME_NOTIFY_FAIL_FIRST_N）演示退避重试。
- send_now：同步发送，内部按指数退避重试；测试用。
- send_async：后台守护线程发送；服务端用。
- pending 记录可在进程重启后由 drain_pending 继续重发。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import config, models

# 全局“前 N 次发送失败”的注入开关（进程级计数器）
_fail_budget_lock = threading.Lock()
_fail_budget = config.NOTIFY_FAIL_FIRST_N


def reset_fail_budget(n: Optional[int] = None) -> None:
    """测试钩子：重置失败注入。None 表示回到环境变量配置。"""
    global _fail_budget
    with _fail_budget_lock:
        _fail_budget = config.NOTIFY_FAIL_FIRST_N if n is None else n


def _consume_fail() -> bool:
    global _fail_budget
    with _fail_budget_lock:
        if _fail_budget > 0:
            _fail_budget -= 1
            return True
        return False


class NotificationError(RuntimeError):
    pass


def _attempt_send(row) -> None:
    """模拟一次外部渠道调用，可能抛出瞬时故障。"""
    if _consume_fail():
        raise ConnectionError("模拟渠道瞬时不可用（短信网关 503）")


def send_now(conn, notification_id: int,
             max_attempts: Optional[int] = None,
             backoff: Optional[float] = None) -> bool:
    """对单条通知做退避重试，返回最终是否成功。

    每次尝试都更新 attempts/last_error；全部失败保持 status=pending。
    """
    max_attempts = max_attempts or config.NOTIFY_MAX_ATTEMPTS
    backoff = config.NOTIFY_BACKOFF_SECONDS if backoff is None else backoff
    delay = backoff
    for attempt in range(1, max_attempts + 1):
        row = conn.execute(
            "SELECT * FROM notifications WHERE id=?", (notification_id,)
        ).fetchone()
        if row is None:
            return False
        try:
            _attempt_send(row)
        except Exception as exc:  # 瞬时故障：记录后退避重试
            conn.execute(
                "UPDATE notifications SET attempts=?, last_error=? WHERE id=?",
                (attempt, str(exc), notification_id),
            )
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= 2
            continue
        conn.execute(
            "UPDATE notifications SET status='sent', attempts=?, last_error='', sent_at=? "
            "WHERE id=?",
            (attempt, _now(), notification_id),
        )
        return True
    conn.execute(
        "UPDATE notifications SET status='failed' WHERE id=?", (notification_id,)
    )
    return False


def enqueue(conn, appointment_id: Optional[int], channel: str, target: str,
            content: str) -> int:
    cur = conn.execute(
        "INSERT INTO notifications(appointment_id,channel,target,content,status,attempts,created_at)"
        " VALUES (?,?,?,?,'pending',0,?)",
        (appointment_id, channel, target, content, _now()),
    )
    return cur.lastrowid


def send_async(app, notification_id: int) -> None:
    """后台守护线程发送（瞬时故障在线程内完成退避重试）。"""
    def job():
        conn = app.connect()
        try:
            send_now(conn, notification_id)
        finally:
            conn.close()

    threading.Thread(target=job, daemon=True).start()


def drain_pending(conn) -> int:
    """重发所有 pending 通知（重试上限内），返回成功条数。

    status='failed' 表示已达最大重试次数的终态，不在自动重发范围内；
    需要人工介入后重新入队。
    """
    rows = conn.execute(
        "SELECT id FROM notifications WHERE status='pending'"
    ).fetchall()
    ok = 0
    for r in rows:
        if send_now(conn, r["id"]):
            ok += 1
    return ok


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
