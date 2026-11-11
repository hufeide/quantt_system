#!/usr/bin/env bash
# =============================================================================
# 构建镜像并导入 K3s 的 containerd
#
#   bash scripts/import_images.sh                 # 默认 tag 1.0.0
#   TAG=1.0.1 bash scripts/import_images.sh
#
# 背景（方案.md 第二章）：
#   Docker 和 K3s 的 containerd 不是同一套运行时，docker build 出来的镜像
#   K3s 看不到。单机阶段用 docker save + k3s ctr images import 导入。
#   有了私有 Registry 之后改成 docker push 即可，不再需要本脚本。
# =============================================================================
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${TAG:-1.0.0}"
REGISTRY="${REGISTRY:-ghcr.io/quant-replay}"
IMAGES=("api" "worker" "web")
TMP_DIR="${TMPDIR:-/tmp}/quant-replay-images"

command -v docker >/dev/null 2>&1 || die "缺少 docker，请先安装 Docker"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "需要 sudo 才能调用 k3s ctr"
  SUDO="sudo"
fi

mkdir -p "${TMP_DIR}"
cd "${REPO_ROOT}"

for name in "${IMAGES[@]}"; do
  dockerfile="infrastructure/docker/Dockerfile.${name}"
  image="${REGISTRY}/quant-replay-${name}:${TAG}"
  tar="${TMP_DIR}/quant-replay-${name}-${TAG}.tar"

  info "构建 ${image}（${dockerfile}）"
  docker build -f "${dockerfile}" -t "${image}" .

  info "导出 ${tar}"
  docker save -o "${tar}" "${image}"

  info "导入 K3s containerd（namespace: k8s.io）"
  ${SUDO} k3s ctr -n k8s.io images import "${tar}"
  rm -f "${tar}"
done

info "校验 K3s 中的镜像："
${SUDO} k3s ctr -n k8s.io images ls -q | grep "quant-replay" || true

cat <<EOF

导入完成。清单里引用的镜像名是：
  ${REGISTRY}/quant-replay-api:${TAG}
  ${REGISTRY}/quant-replay-worker:${TAG}
  ${REGISTRY}/quant-replay-web:${TAG}

如果改过 REGISTRY 或 TAG，请同步修改
infrastructure/k3s/kustomization.yaml 里的 images.newTag，或直接：

  kubectl apply -k infrastructure/k3s

注意：imagePullPolicy 是 IfNotPresent，重新导入同 tag 的镜像不会自动生效，
需要换 tag，或者删掉 Pod 让它重建。
EOF
