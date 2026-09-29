#!/usr/bin/env bash
# 单条本地启动命令：创建数据库与演示样例（预约 / 临时改期 / 交接 / 异常 / 评价），
# 然后启动 Web 服务。重复执行幂等：样例已存在则跳过。
#
#   bash run.sh            # 默认 http://127.0.0.1:8000
#   PORT=8080 bash run.sh  # 自定义端口
set -euo pipefail
cd "$(dirname "$0")"

export ESCORT_HOST="${ESCORT_HOST:-127.0.0.1}"
export ESCORT_PORT="${ESCORT_PORT:-${PORT:-8000}}"

# 首次运行用独立演示库，避免脏数据；如需重置：rm -rf data/
python3 -m app.seed
exec python3 -m app.server --host "$ESCORT_HOST" --port "$ESCORT_PORT" --fail-sms 2
