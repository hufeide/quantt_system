#!/usr/bin/env bash
# =============================================================================
# 生成样例行情并构建标准化数据集（幂等，可重复执行）
#
#   bash scripts/seed_data.sh
#   QUANT_SEED_WITH_FACTORS=1 bash scripts/seed_data.sh   # 同时计算因子
#   FORCE=1 bash scripts/seed_data.sh                     # 已有数据集时强制重建
#   QUANT_DATA_ROOT=/var/lib/quant-data bash scripts/seed_data.sh   # K3s 部署时
#
# 环境变量：
#   PYTHON                   python 解释器（默认 python，建议先 activate .venv）
#   QUANT_DATA_ROOT          数据根目录（默认 <repo>/data_files；K3s 用 /var/lib/quant-data）
#   QUANT_DATASET_VERSION    数据集版本（默认 v001）
#   SEED_START / SEED_END    样例数据的日期区间（默认 2025-01-02 ~ 2025-01-10，
#                            与仓库自带的 3360 行样例一致，保证 checksum 可复现）
#   SEED_SYMBOLS             逗号分隔的标的列表
#   SEED_SOURCE              数据源（默认 synthetic）
#   SEED_SEED                随机种子（默认 42）
#   QUANT_SEED_WITH_FACTORS  1 = 同时计算因子（默认 0，因子是派生数据可随时补算）
#   FORCE                    1 = 忽略已有数据集强制重建
#
# 实际工作全部由数据层 CLI 完成（python -m data.cli build/validate/stats）：
#   build 先写 raw/source=<source>/frequency=1m/date=.../part.csv，
#   再校验质量，再原子替换 normalized/dataset=<v>/... + manifest.json + quality_report.json。
# 退出码：0 成功；1 数据质量未通过（数据集与报告仍会写出）；2 数据集不存在（validate/stats）。
# =============================================================================
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# 优先使用仓库内虚拟环境，避免系统 python 缺少 pandas/duckdb 等依赖
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
  else
    PYTHON="python"
  fi
fi
DATA_ROOT="${QUANT_DATA_ROOT:-${REPO_ROOT}/data_files}"
DATASET="${QUANT_DATASET_VERSION:-v001}"
SEED_START="${SEED_START:-2025-01-02}"
SEED_END="${SEED_END:-2025-01-10}"
SEED_SYMBOLS="${SEED_SYMBOLS:-600000.SH,000001.SZ}"
SEED_SOURCE="${SEED_SOURCE:-synthetic}"
SEED_SEED="${SEED_SEED:-42}"
WITH_FACTORS="${QUANT_SEED_WITH_FACTORS:-0}"
FORCE="${FORCE:-0}"

MANIFEST="${DATA_ROOT}/normalized/dataset=${DATASET}/manifest.json"
QUALITY_REPORT="${DATA_ROOT}/normalized/dataset=${DATASET}/quality_report.json"
FACTOR_DIR="${DATA_ROOT}/factors/version=${DATASET}"

command -v "${PYTHON}" >/dev/null 2>&1 || die "找不到 Python 解释器：${PYTHON}"
"${PYTHON}" -c "import data.cli" >/dev/null 2>&1 || die "无法导入 data.cli，请先 pip install -e ."

info "数据根目录：${DATA_ROOT}"
info "数据集版本：${DATASET}"
info "日期区间：  ${SEED_START} ~ ${SEED_END}"
info "标的：      ${SEED_SYMBOLS}"
info "数据源：    ${SEED_SOURCE}（seed=${SEED_SEED}，factors=${WITH_FACTORS}）"

# 容器里 /data 是挂载点；裸机首次运行时目录可能还不存在
mkdir -p "${DATA_ROOT}" "${DATA_ROOT}/backtests"

FACTOR_ARGS=()
if [ "${WITH_FACTORS}" = "1" ]; then
  FACTOR_ARGS=(--with-factors)
fi

has_factors() {
  [ -d "${FACTOR_DIR}" ] && [ -n "$(find "${FACTOR_DIR}" -name '*.parquet' -print -quit 2>/dev/null)" ]
}

# ---------------------------------------------------------------------------
info "步骤 1/3：生成原始行情并构建标准化数据集"
if [ -f "${MANIFEST}" ] && [ "${FORCE}" != "1" ] \
   && { [ "${WITH_FACTORS}" != "1" ] || has_factors; }; then
  info "已存在数据集 ${DATASET}（${MANIFEST}），跳过构建"
  info "需要重建请加 FORCE=1；只补因子请加 QUANT_SEED_WITH_FACTORS=1"
else
  set +e
  "${PYTHON}" -m data.cli build \
    --root "${DATA_ROOT}" \
    --version "${DATASET}" \
    --frequency 1m \
    --source "${SEED_SOURCE}" \
    --symbols "${SEED_SYMBOLS}" \
    --start "${SEED_START}" \
    --end "${SEED_END}" \
    --seed "${SEED_SEED}" \
    ${FACTOR_ARGS[@]+"${FACTOR_ARGS[@]}"}
  BUILD_RC=$?
  set -e
  case "${BUILD_RC}" in
    0) info "数据集构建成功" ;;
    1) warn "数据质量校验未通过：数据集与报告已写出，请查看 ${QUALITY_REPORT}" ;;
    *) die "数据集构建失败（退出码 ${BUILD_RC}）" ;;
  esac
fi

# ---------------------------------------------------------------------------
info "步骤 2/3：数据质量校验（normalized）"
"${PYTHON}" -m data.cli validate --root "${DATA_ROOT}" --version "${DATASET}" --frequency 1m \
  || warn "数据校验有告警，回测仍可执行，但结果可信度下降"

# ---------------------------------------------------------------------------
info "步骤 3/3：数据集统计"
"${PYTHON}" -m data.cli stats --root "${DATA_ROOT}" --version "${DATASET}" --frequency 1m

NORMALIZED_DIR="${DATA_ROOT}/normalized/dataset=${DATASET}"
COUNT="$("${PYTHON}" - "${NORMALIZED_DIR}" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
print(len(list(root.rglob("*.parquet"))))
PY
)"
info "Parquet 文件数量：${COUNT}（${NORMALIZED_DIR}）"
[ "${COUNT}" -gt 0 ] || die "没有生成任何 Parquet 文件，检查上面的日志"

if [ "${WITH_FACTORS}" = "1" ]; then
  has_factors && info "因子产物：${FACTOR_DIR}" || warn "已请求因子但未找到 parquet 产物"
fi

cat <<EOF

样例数据准备完成。
  dataset_version = ${DATASET}
  data_root       = ${DATA_ROOT}
  manifest        = ${MANIFEST}

下一步：
  python scripts/run_backtest.py --help          # 命令行跑一次回测
  bash scripts/dev_up.sh                         # 本地起 API / Worker / Web
  # K3s：数据要放在 /var/lib/quant-data，再执行 bash scripts/deploy.sh
EOF
