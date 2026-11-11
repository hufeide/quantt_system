#!/usr/bin/env bash
# =============================================================================
# 本地开发环境
#
#   bash scripts/dev_up.sh            # 只起 MongoDB + RabbitMQ（默认）
#   bash scripts/dev_up.sh app        # 前台说明如何启动 uvicorn / celery / streamlit
#   bash scripts/dev_up.sh run        # 后台启动三个进程，日志写到 .dev/logs/
#   bash scripts/dev_up.sh all        # docker compose 全量启动（含 api/worker/web 容器）
#   bash scripts/dev_up.sh status     # 查看状态
#   bash scripts/dev_up.sh down       # 停掉 compose 里的服务
#
# 单机开发推荐用 "infra + run"：中间件跑容器，Python 进程直接跑在 venv 里，
# 改代码不用重新 build 镜像。
# =============================================================================
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infrastructure/docker/docker-compose.yml"
LOG_DIR="${REPO_ROOT}/.dev/logs"
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
  else
    PYTHON="python"
  fi
fi
MODE="${1:-infra}"

command -v docker >/dev/null 2>&1 || die "缺少 docker"

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }

wait_healthy() {
  local name="$1" timeout="${2:-120}" waited=0 status=""
  while [ "${waited}" -lt "${timeout}" ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "${name}" 2>/dev/null || echo "missing")"
    [ "${status}" = "healthy" ] && { info "${name} 已就绪"; return 0; }
    sleep 3
    waited=$((waited + 3))
  done
  warn "${name} 在 ${timeout} 秒内未变为 healthy（当前：${status}）"
  return 1
}

start_infra() {
  info "启动 MongoDB 与 RabbitMQ（容器）"
  compose up -d mongodb rabbitmq
  wait_healthy quant-mongodb 180 || true
  wait_healthy quant-rabbitmq 180 || true
  info "MongoDB:  mongodb://quant:***@localhost:27017/"
  info "RabbitMQ: amqp://quant:***@localhost:5672/  管理台 http://localhost:15672"
}

print_app_commands() {
  cat <<EOF

------------------------------------------------------------
 在另一个终端里启动应用（建议先 source .venv/bin/activate）
------------------------------------------------------------
# 1) API
${PYTHON} -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload

# 2) Celery Worker
${PYTHON} -m celery -A apps.worker.celery_app worker \
    -Q replay,backtest,factor,optimization,maintenance -c 2 -l info

# 3) Streamlit
${PYTHON} -m streamlit run apps/web/app.py --server.port 8501 --server.address 0.0.0.0

------------------------------------------------------------
环境变量（与 docker-compose / K8s 完全一致，命名以代码为准）：
  export QUANT_ENV=dev
  export QUANT_LOG_LEVEL=INFO
  export QUANT_LOG_JSON=true
  export QUANT_MONGO_URI="mongodb://quant:quant_dev_pwd@localhost:27017/?authSource=admin"
  export QUANT_MONGO_DB=quant
  export QUANT_RABBITMQ_URL="amqp://quant:quant_dev_pwd@localhost:5672//"
  export QUANT_CELERY_BROKER_URL="$QUANT_RABBITMQ_URL"
  export CELERY_BROKER_URL="$QUANT_RABBITMQ_URL"
  export CELERY_RESULT_BACKEND=rpc://
  export QUANT_DATA_ROOT="${REPO_ROOT}/data_files"
  export QUANT_OUTPUT_ROOT="${REPO_ROOT}/data_files/backtests"
  export QR_DATA_ROOT="$QUANT_DATA_ROOT"        # worker 侧兼容名
  export QR_OUTPUT_ROOT="$QUANT_OUTPUT_ROOT"
  export QUANT_API_BASE_URL="http://localhost:8000"
  export QR_API_PREFIX=""

访问：
  Web       http://localhost:8501
  API 文档  http://localhost:8000/docs
------------------------------------------------------------
EOF
}

run_apps_background() {
  mkdir -p "${LOG_DIR}"
  info "后台启动 API / Worker / Streamlit，日志在 ${LOG_DIR}"
  ( cd "${REPO_ROOT}" && nohup ${PYTHON} -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 \
      > "${LOG_DIR}/api.log" 2>&1 & echo $! > "${LOG_DIR}/api.pid" )
  ( cd "${REPO_ROOT}" && nohup ${PYTHON} -m celery -A apps.worker.celery_app worker \
      -Q replay,backtest,factor,optimization,maintenance -c 2 -l info \
      > "${LOG_DIR}/worker.log" 2>&1 & echo $! > "${LOG_DIR}/worker.pid" )
  ( cd "${REPO_ROOT}" && nohup ${PYTHON} -m streamlit run apps/web/app.py \
      --server.port 8501 --server.address 0.0.0.0 --server.headless true \
      > "${LOG_DIR}/web.log" 2>&1 & echo $! > "${LOG_DIR}/web.pid" )
  sleep 5
  info "已启动："
  info "  tail -f ${LOG_DIR}/api.log"
  info "  tail -f ${LOG_DIR}/worker.log"
  info "  tail -f ${LOG_DIR}/web.log"
  info "停止：kill $(cat ${LOG_DIR}/*.pid)"
}

case "${MODE}" in
  infra|up)
    start_infra
    print_app_commands
    ;;
  app)
    print_app_commands
    ;;
  run)
    start_infra
    run_apps_background
    ;;
  all)
    info "docker compose 全量启动（首次会构建镜像，较慢）"
    compose up -d --build
    compose ps
    ;;
  status)
    compose ps
    ;;
  down)
    info "停止并移除容器（保留数据卷）"
    compose down
    ;;
  *)
    die "未知参数：${MODE}，可用：infra | app | run | all | status | down"
    ;;
esac
