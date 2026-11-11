#!/usr/bin/env bash
# =============================================================================
# 单机 K3s + Nginx Ingress 安装脚本（WSL2 Ubuntu）
#
#   bash scripts/install_k3s.sh
#
# 做四件事：
#   1. 安装 K3s，禁用内置 Traefik（避免与 Nginx Ingress 抢 80/443）
#   2. kubeconfig 权限设为 644，复制到 ~/.kube/config
#   3. 安装 Helm 与 ingress-nginx
#   4. 等待节点与 Ingress Controller 就绪
#
# 需要 root 或 sudo。只跑一次，重复执行会跳过已安装的部分。
# =============================================================================
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES_FILE="${REPO_ROOT}/infrastructure/k3s/ingress/nginx-ingress-values.yaml"

[ -f "${VALUES_FILE}" ] || die "找不到 ${VALUES_FILE}，请在仓库根目录执行"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "需要 root 权限，但系统没有 sudo"
  SUDO="sudo"
fi

command -v curl >/dev/null 2>&1 || die "缺少 curl：sudo apt-get install -y curl"

# ---------------------------------------------------------------------------
info "步骤 1/4：安装 K3s"
if command -v k3s >/dev/null 2>&1; then
  warn "已检测到 K3s（$(k3s --version | head -n 1)），跳过安装"
else
  info "正在下载并安装 K3s（--disable traefik --write-kubeconfig-mode 644）..."
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_CHANNEL="${K3S_CHANNEL:-stable}" \
    INSTALL_K3S_EXEC="--disable traefik --write-kubeconfig-mode 644" \
    sh -s -
  info "K3s 安装完成"
fi

# ---------------------------------------------------------------------------
info "步骤 2/4：配置 kubeconfig"
mkdir -p "${HOME}/.kube"
if [ -f /etc/rancher/k3s/k3s.yaml ]; then
  ${SUDO} cp /etc/rancher/k3s/k3s.yaml "${HOME}/.kube/config"
  ${SUDO} chown "$(id -u):$(id -g)" "${HOME}/.kube/config"
  chmod 600 "${HOME}/.kube/config"
else
  die "找不到 /etc/rancher/k3s/k3s.yaml，K3s 可能安装失败"
fi
export KUBECONFIG="${HOME}/.kube/config"

info "等待节点就绪（最多 180 秒）..."
${SUDO} kubectl wait --for=condition=Ready node --all --timeout=180s \
  || die "节点未在 180 秒内就绪，执行 sudo journalctl -u k3s -n 100 查看原因"

# ---------------------------------------------------------------------------
info "步骤 3/4：安装 Helm"
if command -v helm >/dev/null 2>&1; then
  warn "已检测到 Helm（$(helm version --short)），跳过安装"
else
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

# ---------------------------------------------------------------------------
info "步骤 4/4：安装 Nginx Ingress Controller"
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null
helm repo update >/dev/null
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress \
  --create-namespace \
  -f "${VALUES_FILE}" \
  --wait \
  --timeout 10m

${SUDO} kubectl -n ingress rollout status ds/ingress-nginx-controller --timeout=300s

cat <<'EOF'

============================================================
 K3s + Nginx Ingress 安装完成
============================================================
 集群信息：   kubectl get nodes -o wide
 入口状态：   kubectl -n ingress get pods
 入口地址：   http://localhost/           （hostNetwork 直接占用 80）
              如果 80 被占用，改用 http://localhost:30080/

 下一步：
   1. 构建并导入镜像：bash scripts/import_images.sh
   2. 准备数据：      bash scripts/seed_data.sh
   3. 部署应用：      bash scripts/deploy.sh

 提示（WSL2）：
   - Windows 浏览器访问 localhost 依赖 WSL2 的 localhost 转发，若失败请用
     hostname -I 得到的 WSL IP 访问，并确认 Windows 防火墙放行 80 端口。
   - 不要把 WSL IP 写死到任何配置文件里，重启后可能变化。
============================================================
EOF
