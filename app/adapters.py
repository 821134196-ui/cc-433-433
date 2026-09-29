"""进程内模拟适配器：通知（含失败注入与重试）、院内路线。

不依赖任何外部服务。通知写入 SQLite 留痕，支持“前 N 次失败、之后成功”的
确定性失败注入，用于测试通知重试；超时/失败在重试上限后落为 failed 并触发告警事件。
"""
import json
import time
from datetime import datetime


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


class NotificationError(Exception):
    pass


class InProcessNotifier:
    """带重试的进程内通知适配器。

    fail_pattern: {channel: 前 k 次发送抛错}，例如 {"sms": 2} 表示短信前两次失败。
    所有发送按 (channel, target) 维度计数，保证测试可复现。
    """

    def __init__(self, conn, max_attempts=3, backoff=0.02, fail_pattern=None):
        self.conn = conn
        self.max_attempts = max_attempts
        self.backoff = backoff
        # channel -> 该渠道仍需失败的次数。
        # 传入同一可变 dict 可让多次请求共享失败计数（服务进程级演示）。
        self._fail_left = fail_pattern if fail_pattern is not None else {}
        self.sent_log = []  # 进程内发送留痕

    def _do_send(self, channel, target, content):
        left = self._fail_left.get(channel, 0)
        if left > 0:
            self._fail_left[channel] = left - 1
            raise NotificationError(f"模拟{channel}通道暂时不可用")
        self.sent_log.append({"channel": channel, "target": target, "content": content, "at": now_iso()})

    def send(self, appointment_id, channel, target, content):
        cur = self.conn.execute(
            "INSERT INTO notifications(appointment_id,channel,target,content,status,attempts,created_at)"
            " VALUES (?,?,?,?, 'pending', 0, ?)",
            (appointment_id, channel, target, content, now_iso()),
        )
        nid = cur.lastrowid
        last_err = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._do_send(channel, target, content)
                self.conn.execute(
                    "UPDATE notifications SET status='sent', attempts=?, last_error='', sent_at=? WHERE id=?",
                    (attempt, now_iso(), nid),
                )
                return {"id": nid, "status": "sent", "attempts": attempt}
            except NotificationError as exc:
                last_err = str(exc)
                self.conn.execute(
                    "UPDATE notifications SET attempts=?, last_error=? WHERE id=?",
                    (attempt, last_err, nid),
                )
                if attempt < self.max_attempts:
                    time.sleep(self.backoff * attempt)
        # 重试耗尽：落 failed，产生一条告警事件留痕
        self.conn.execute(
            "UPDATE notifications SET status='failed', last_error=? WHERE id=?",
            (f"重试{self.max_attempts}次后仍失败: {last_err}", nid),
        )
        self.conn.execute(
            "INSERT INTO manual_reviews(appointment_id,reason,requested_action,status,created_at)"
            " VALUES (?,?,?, 'pending', ?)",
            (appointment_id, "通知发送失败（重试耗尽）", f"人工联系 {target}（{channel}）", now_iso()),
        )
        return {"id": nid, "status": "failed", "attempts": self.max_attempts}


# 演示用院内路线库（进程内模拟，不接真实导航）
ROUTE_GRAPH = {
    "门诊大厅": {"扶梯": "住院部一层连廊", "便民电梯": "住院部一层连廊"},
    "住院部一层连廊": {"2号电梯": "医技楼三层", "扶梯": "门诊大厅"},
    "医技楼三层": {"走廊A": "放射科登记处", "走廊B": "超声科登记处"},
    "放射科登记处": {"3号机房通道": "CT 3号机房", "1号机房通道": "DR 1号机房"},
    "超声科登记处": {"B区通道": "超声 B 超 2 室"},
    "CT 3号机房": {"出口通道": "放射科登记处"},
    "DR 1号机房": {"出口通道": "放射科登记处"},
    "超声 B 超 2 室": {"出口通道": "超声科登记处"},
}

# 从常用起点到常用目的地的规范路线（演示）
ROUTE_PRESETS = {
    ("门诊大厅", "CT 3号机房"): ["门诊大厅", "便民电梯", "住院部一层连廊", "2号电梯", "医技楼三层", "走廊A", "放射科登记处", "3号机房通道", "CT 3号机房"],
    ("门诊大厅", "超声 B 超 2 室"): ["门诊大厅", "便民电梯", "住院部一层连廊", "2号电梯", "医技楼三层", "走廊B", "超声科登记处", "B区通道", "超声 B 超 2 室"],
    ("医技楼三层", "CT 3号机房"): ["医技楼三层", "走廊A", "放射科登记处", "3号机房通道", "CT 3号机房"],
}

# 路线变更备用走法（如电梯维保，改走连廊平坡）
ALT_ROUTES = {
    ("门诊大厅", "CT 3号机房"): ["门诊大厅", "扶梯", "住院部一层连廊（平坡绕行）", "1号电梯", "医技楼三层", "走廊A", "放射科登记处", "3号机房通道", "CT 3号机房"],
}


class InProcessRouter:
    """院内路线模拟适配器：支持规范路线、备用路线与最短路径回退。"""

    def plan(self, origin, destination, alt=False):
        if alt and (origin, destination) in ALT_ROUTES:
            return ALT_ROUTES[(origin, destination)]
        if (origin, destination) in ROUTE_PRESETS:
            return ROUTE_PRESETS[(origin, destination)]
        return self._bfs(origin, destination)

    def _bfs(self, origin, destination):
        from collections import deque
        q = deque([(origin, [origin])])
        seen = {origin}
        while q:
            node, path = q.popleft()
            for via, nxt in ROUTE_GRAPH.get(node, {}).items():
                step = f"{via}→{nxt}"
                if nxt == destination:
                    return path + [via, nxt]
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, path + [via, nxt]))
        return [origin, destination]

    def current_guide(self, route, current_location):
        """根据当前位置给出下一步；已到达终点返回完成提示。"""
        if not route:
            return {"done": True, "next": ""}
        if current_location in route:
            idx = route.index(current_location)
            if idx == len(route) - 1:
                return {"done": True, "next": "已到达目的地", "remaining": 0}
            return {"done": False, "next": f"前往 {route[idx + 1]}", "remaining": len(route) - 1 - idx}
        # 当前位置不在路线上：提示回到最近的已知节点
        return {"done": False, "next": f"当前不在规划路线上，请先回到 {route[0]}", "remaining": len(route) - 1, "off_route": True}
