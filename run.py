#!/usr/bin/env python3
"""单条本地启动命令：

    python3 run.py

完成三件事：
1. 初始化 SQLite（data/escort.db）与本地附件目录；
2. 写入演示样例——预约、临时改期、交接记录、签到时间线、
   敏感附件、越权转人工，以及两条会触发失败重试的通知；
3. 启动 Web 服务（默认 http://127.0.0.1:8000）。

可选环境变量：
  ME_PORT=8000                 端口
  ME_DATA_DIR=./data           数据目录
  ME_NOTIFY_FAIL_FIRST_N=2     通知前 N 次发送失败（演示退避重试）
  ME_NO_SEED=1                 已有库时跳过种子（默认本就幂等）
  ME_DEMO_RETRY=0              设为 0 可关闭启动时的通知重试演示
"""
from __future__ import annotations

import os
import sys

from medical_escort import config, notifications
from medical_escort.seed import seed
from medical_escort.server import create_server
from medical_escort.service import EscortApp


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    config.ensure_dirs()
    app = EscortApp(async_notify=True)

    ids = seed(app)
    if ids:
        # 演示通知重试：种子里有 3 条 pending（接单通知已同步发出），
        # 其中前若干次“发送”会失败并退避重试。
        if os.environ.get("ME_DEMO_RETRY", "1") != "0":
            notifications.reset_fail_budget(
                int(os.environ.get("ME_NOTIFY_FAIL_FIRST_N", "2")))
            conn = app.connect()
            try:
                print("[notify] 模拟渠道抖动，重发 pending 通知（含退避重试）…")
                ok = notifications.drain_pending(conn)
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM notifications").fetchone()["c"]
                print(f"[notify] 完成：累计 {total} 条，本轮重发成功 {ok} 条；"
                      "页面「授权事项 → 人工确认队列」旁可手动再试。")
            finally:
                conn.close()

    port = config.PORT
    httpd = create_server(port=port, db_path=app.db_path, async_notify=True)
    print("")
    print("康柏陪诊平台已启动：")
    print(f"  本地访问：http://127.0.0.1:{port}/")
    print("  视角切换：页面右上角可在「调度台 / 陪诊员移动端 / 患者端」切换")
    print("  患者端演示编号：P-1001 ~ P-1004（均为虚构数据）")
    print("  Ctrl+C 停止服务")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
