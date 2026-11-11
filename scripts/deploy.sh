#!/usr/bin/env bash
# =============================================================================
# 一键部署到 K3s
#
#   bash scripts/deploy.sh
#   NAMESPACE_WAIT=600 bash scripts/deploy.sh     # 自定义等待秒数
#
# 步骤：
#   1. kubectl apply -k infrastructure/k3s（命名空间、配置、数据库、应用、监控）
#   2. 用仓库里的文件生成 Grafana 数据源 / 仪表盘 ConfigMap
#      （kustomize 不允许引用 kustomization 目录之外的文件，所以放在这里做）
#   3. 等待关键工作负载 rollout
#   4. 打印访问地址
# =============================================================================
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL="${KUBECTL:-kubectl}"
WAIT="${NAMESPACE_WAIT:-300}"
GRAFANA_DIR="${REPO_ROOT}/infrastructure/monitoring/grafana"

command -v "${KUBECTL}" >/dev/null 2>&1 || die "找不到 kubectl，请先执行 scripts/install_k3s.sh"
"${KUBECTL}" cluster-info >/dev/null 2>&1 || die "无法连接集群，检查 KUBECONFIG 或 k3s 服务状态"

# ---------------------------------------------------------------------------
info "步骤 1/4：应用 K8s 清单（kubectl apply -k）"
"${KUBECTL}" apply -k "${REPO_ROOT}/infrastructure/k3s"

# ---------------------------------------------------------------------------
info "步骤 2/4：生成 Grafana 数据源与仪表盘 ConfigMap"
"${KUBECTL}" -n quant-monitor create configmap grafana-datasources \
  --from-file=datasources.yaml="${GRAFANA_DIR}/datasources.yaml" \
  --dry-run=client -o yaml | "${KUBECTL}" apply -f -

"${KUBECTL}" -n quant-monitor create configmap grafana-dashboards \
  --from-file="${GRAFANA_DIR}/dashboards" \
  --dry-run=client -o yaml | "${KUBECTL}" apply -f -

info "重启 Grafana 以加载新的 provisioning 配置"
"${KUBECTL}" -n quant-monitor rollout restart deployment/grafana >/dev/null

# ---------------------------------------------------------------------------
info "步骤 3/4：等待工作负载就绪（超时 ${WAIT}s）"
wait_for() {
  local target="$1"
  if ! "${KUBECTL}" -n "${target%%/*}" rollout status "${target##*/}" --timeout="${WAIT}s"; then
    warn "${target} 未在 ${WAIT} 秒内就绪，继续后续检查"
    return 1
  fi
}

FAILED=0
wait_for "quant-data/mongodb"        || FAILED=1
wait_for "quant-mq/rabbitmq"         || FAILED=1
wait_for "quant-app/api"             || FAILED=1
wait_for "quant-app/web"             || FAILED=1
wait_for "quant-core/backtest-worker" || FAILED=1
wait_for "quant-monitor/prometheus"  || FAILED=1
wait_for "quant-monitor/loki"        || FAILED=1
wait_for "quant-monitor/grafana"     || FAILED=1

# ---------------------------------------------------------------------------
info "步骤 4/4：部署结果"
"${KUBECTL}" get pods -A -l app.kubernetes.io/part-of=quant-replay -o wide || true

if "${KUBECTL}" -n quant-app get secret quant-secret -o yaml 2>/dev/null | grep -q "REPLACE_ME"; then
  warn "检测到 Secret 里仍是 REPLACE_ME 占位符，数据库连接会失败！"
  warn "请按 infrastructure/k3s/applications/secret.yaml 顶部注释替换为真实密码。"
fi

cat <<EOF

============================================================
 部署完成
============================================================
 量化平台    http://localhost/
 API 文档    http://localhost/docs
 API 根路径  http://localhost/api/
 Grafana     http://localhost/grafana/   （默认 admin / 见 grafana-admin Secret）
 Prometheus  http://localhost/prometheus/

 排查命令：
   kubectl -n quant-app logs -f deploy/api
   kubectl -n quant-core logs -f deploy/backtest-worker
   kubectl -n quant-app describe pod -l app=api
   kubectl get events -A --sort-by=.lastTimestamp | tail -30
============================================================
EOF

[ "${FAILED}" -eq 0 ] || die "部分工作负载未就绪，请按上面的排查命令定位"
