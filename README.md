# Quant Replay — 单机分钟级量化回测系统

一套可在 **一台 Windows + WSL2 电脑**上完整跑起来的量化回测平台：
确定性的事件驱动回测核心 + FastAPI 业务接口 + Celery 异步任务 + MongoDB / Parquet 存储 +
K3s 单节点编排 + Prometheus / Grafana / Loki 可观测性。

设计目标不是"功能最多"，而是**回测结果可复现**：同一份数据、同一份策略、同一组参数，
无论跑在本地 Python、Docker 容器还是 K3s Pod 里，产出的成交、资金曲线和指标必须一致。

---

## 一、架构

```
┌───────────────────────────────────────────────────────────────────────┐
│  Windows 浏览器 / API Client                                          │
└───────────────────────────────┬───────────────────────────────────────┘
                                │  http://localhost/   (80)
┌───────────────────────────────▼───────────────────────────────────────┐
│  WSL2 Ubuntu                                                          │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  K3s（containerd + Flannel + CoreDNS）                          │  │
│  │                                                                 │  │
│  │  [ingress]        Nginx Ingress Controller                      │  │
│  │                     /            ──> web:8501                   │  │
│  │                     /api/*       ──> api:8000   (rewrite 去 /api)│  │
│  │                     /docs        ──> api:8000                   │  │
│  │                     /grafana/*   ──> grafana:3000               │  │
│  │                     /prometheus/*──> prometheus:9090            │  │
│  │                                                                 │  │
│  │  [quant-app]      FastAPI (业务接口)   Streamlit (Web UI)       │  │
│  │        │                  │                                     │  │
│  │        │            投递任务 (AMQP)                             │  │
│  │        ▼                  ▼                                     │  │
│  │  [quant-mq]       RabbitMQ  ── 队列: replay / backtest /        │  │
│  │                            factor / optimization / maintenance  │  │
│  │        │                                                        │  │
│  │        ▼                                                        │  │
│  │  [quant-core]     Celery Worker  ──>  统一回测核心 core/*       │  │
│  │                                        行情回放 / 策略 / 撮合 / │  │
│  │                                        账户 / 风控 / 因子      │  │
│  │        │                                                        │  │
│  │        ▼                                                        │  │
│  │  [quant-data]     MongoDB (元数据) + hostPath PV (Parquet 行情  │  │
│  │                   与回测产物, /var/lib/quant-data)              │  │
│  │                                                                 │  │
│  │  [quant-monitor]  Prometheus + Grafana + Loki +                  │  │
│  │                   node-exporter / kube-state-metrics / promtail  │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### 回测执行流程（事件驱动，顺序确定）

```
Parquet 分钟行情
      │
      ▼
 Replay Engine ──BarReceived──> Strategy Engine ──SignalGenerated──> Risk Engine
                                                                          │
                                                                          ▼
 Account Engine <──OrderFilled/OrderCancelled── Matching Engine <──OrderSubmitted
      │
      ├──> events.parquet / orders.parquet / trades.parquet
      ├──> positions.parquet / equity.parquet
      ├──> metrics.json
      └──> manifest.json（锁定数据版本、策略版本、参数、费用、随机种子）
```

**一条硬规则**：09:30–09:31 这根分钟 K 线，只有在 09:31 收盘后才允许使用它的
完整 OHLCV。回放引擎按模拟时钟推进，策略只能看到"当前时点已经结束"的 Bar。

---

## 二、目录结构

```
quant_system/
├── core/                     # 回测核心：不依赖 FastAPI / Celery / MongoDB / K8s
│   ├── market/               # 行情模型、交易日历、数据集读取
│   ├── replay/               # 回放引擎、检查点、会话
│   ├── strategy/             # 策略基类、指标、注册表
│   ├── matching/             # 撮合规则（滑点、成交量占比、涨跌停、T+1）
│   ├── account/              # 现金、持仓、权益曲线
│   ├── risk/                 # 仓位、单笔金额、回撤、下单频率限制
│   ├── factor/               # 因子计算
│   ├── event/                # 事件模型与事件存储
│   ├── backtest/             # 回测编排、指标计算、产物写出
│   ├── storage/              # Parquet 读写与 manifest
│   ├── config.py             # 纯 dataclass 配置（可从 YAML/JSON 加载）
│   └── logging.py            # 结构化 JSON 日志（run_id 等字段）
├── data/                     # 数据层：接入、标准化、校验、存储
├── apps/
│   ├── api/                  # FastAPI，入口 apps.api.main:app
│   ├── worker/               # Celery，入口 apps.worker.celery_app:celery_app
│   └── web/                  # Streamlit，入口 apps/web/app.py
├── infrastructure/
│   ├── docker/               # Dockerfile.api / .worker / .web + docker-compose.yml
│   ├── k3s/                  # 命名空间、Ingress、应用、Worker、数据库、监控
│   └── monitoring/           # Prometheus 规则、Grafana 数据源与 6 个仪表盘、Loki/Promtail
├── configs/                  # 回测 / 撮合 / 数据库配置样例
├── scripts/                  # install_k3s / import_images / deploy / dev_up / seed_data
├── tests/                    # unit / integration / replay / golden_backtest
├── docs/                     # ARCHITECTURE.md、OPERATIONS.md
├── .github/workflows/ci.yml  # ruff + pytest + golden backtest + docker build
├── pyproject.toml
└── README.md
```

依赖方向（必须保持）：

```
apps/api ─────┐
apps/worker ──┼──> core
apps/web ─────┘
data ─────────────> core 的数据接口
infrastructure ───> 只负责部署，不参与业务逻辑
```

---

## 三、快速开始

### 0. 前置

- Python 3.11+（推荐 3.12）
- Docker（构建镜像 / 本地中间件）
- K3s + kubectl + Helm（仅 K3s 部署需要）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 1. 本地运行（最快）

```bash
bash scripts/dev_up.sh          # 只起 MongoDB + RabbitMQ 容器，并打印启动命令
bash scripts/seed_data.sh       # 生成样例行情并构建 dataset=v001（幂等，已有则跳过）
QUANT_SEED_WITH_FACTORS=1 bash scripts/seed_data.sh   # 需要因子时再加上
```

另开终端启动三个进程：

```bash
python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload
python -m celery -A apps.worker.celery_app worker -Q replay,backtest,factor,optimization,maintenance -c 2 -l info
python -m streamlit run apps/web/app.py --server.port 8501 --server.address 0.0.0.0
```

- Web UI: <http://localhost:8501>
- API 文档: <http://localhost:8000/docs>

### 环境变量约定

命名以代码为准，本地 / Compose / K8s 三套环境完全一致：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `QUANT_DATA_ROOT` | 行情与产物根目录 | `data_files`（容器内 `/data`） |
| `QUANT_OUTPUT_ROOT` | 回测产物目录 | `data_files/backtests` |
| `QUANT_MONGO_URI` / `QUANT_MONGO_DB` | MongoDB 连接与库名 | `mongodb://localhost:27017` / `quant` |
| `QR_DATA_ROOT` / `QR_OUTPUT_ROOT` | worker 侧兼容名（tasks.py 读） | 与上面同值 |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Celery broker 与结果后端 | `amqp://guest:guest@localhost:5672//` / `rpc://` |
| `QUANT_API_BASE_URL` / `QR_API_PREFIX` | Web 访问 API 的地址与前缀 | `http://localhost:8000` / `/api` |
| `QUANT_METRICS_PORT` | worker 的 /metrics 端口（不设则不启） | 容器内 `9108` |
| `PROMETHEUS_MULTIPROC_DIR` | prefork 下 worker 指标目录（必设） | `/tmp/prometheus-multiproc` |
| `CELERY_CONCURRENCY` / `QUANT_MAX_WORKERS` | 并发数 | `2` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

> Web 在容器/K8s 内直连 API Service，因此把 `QR_API_PREFIX` 置空；
> 浏览器侧走 Ingress 时才用 `/api` 前缀。

### 2. Docker Compose（整套容器化，不含 K8s）

```bash
cd infrastructure/docker
cp .env.example .env
docker compose up -d --build
docker compose ps
```

| 服务 | 地址 |
| --- | --- |
| Web (Streamlit) | <http://localhost:8501> |
| API | <http://localhost:8000/docs> |
| RabbitMQ 管理台 | <http://localhost:15672> |
| MongoDB | localhost:27017 |

依赖顺序由 healthcheck 保证：mongo / rabbitmq 健康后才启动 api，api 健康后才启动 web。

### 3. K3s（单机正式形态）

```bash
bash scripts/install_k3s.sh      # 安装 K3s(禁用 Traefik) + Helm + Nginx Ingress
bash scripts/import_images.sh    # docker build -> save -> k3s ctr import
QUANT_DATA_ROOT=/var/lib/quant-data bash scripts/seed_data.sh   # K8s 里挂成 /data
bash scripts/deploy.sh           # kubectl apply -k + Grafana 仪表盘 + 等待 rollout
```

| 地址 | 服务 |
| --- | --- |
| <http://localhost/> | 量化平台（Streamlit） |
| <http://localhost/api/> | FastAPI |
| <http://localhost/docs> | Swagger UI |
| <http://localhost/grafana/> | Grafana |
| <http://localhost/prometheus/> | Prometheus |

> K3s 用的是自己的 containerd，**Docker 构建的镜像不会自动可见**，
> 必须走 `import_images.sh`（或私有 Registry）。详见 `docs/OPERATIONS.md`。

---

## 四、API 示例

> 路由以 <http://localhost/docs> 为准；下面是约定形态。
> 对外统一用 `/api` 前缀，Ingress 会把它 rewrite 掉：`/api/backtests` -> `/backtests`。
> 后端规范路由不带前缀，`/api` 前缀同时以 `include_in_schema=False` 挂了别名，直连也通。

```bash
# 健康检查
curl http://localhost/api/health

# 提交一个回测
curl -X POST http://localhost/api/backtests \
  -H 'Content-Type: application/json' \
  -d '{
        "strategy": "sma_cross",
        "strategy_params": {"fast": 5, "slow": 20},
        "symbols": ["000001.SZ", "600000.SH"],
        "start": "2025-01-02",
        "end": "2025-01-31",
        "dataset_version": "v001",
        "initial_cash": 1000000
      }'

# 查询状态 / 结果
curl http://localhost/api/backtests/<run_id>
curl http://localhost/api/backtests/<run_id>/metrics
curl http://localhost/api/backtests/<run_id>/equity

# 取消任务
curl -X POST http://localhost/api/backtests/<run_id>/cancel

# 数据集列表
curl http://localhost/api/datasets
```

回测产物落在 `$QUANT_OUTPUT_ROOT/run_id=<run_id>/`（本地默认 `data_files/backtests/`，K3s 里是 `/data/backtests/`）：

```
manifest.json  events.parquet  orders.parquet  trades.parquet
positions.parquet  equity.parquet  metrics.json  checkpoints/
```

---

## 五、监控

| 组件 | 作用 | 地址 |
| --- | --- | --- |
| Prometheus | 指标采集 + 告警规则 | /prometheus/ |
| Grafana | 6 个仪表盘 | /grafana/ |
| Loki | 日志聚合（LogQL） | 内部 :3100 |
| Promtail | 采集 Pod 日志并解析 JSON 字段 | DaemonSet |
| node-exporter / kube-state-metrics | 主机与 K8s 对象指标 | DaemonSet / Deployment |
| RabbitMQ / MongoDB exporter | 中间件指标 | :15692 / :9216 |

六个仪表盘（`infrastructure/monitoring/grafana/dashboards/`）：

1. `host-resources` — CPU / 内存 / 磁盘 / 网络
2. `k3s-cluster` — Pod 状态、重启次数、命名空间资源
3. `rabbitmq` — 队列积压、投递速率、消费者数
4. `data-quality` — 数据延迟、缺失分钟、重复 Bar
5. `backtest-performance` — Bar 处理速率、回测耗时 P50/P95/P99、撮合耗时
6. `strategy-performance` — 收益率、最大回撤、胜率、换手率

日志统一字段（`core/logging.py` 输出单行 JSON）：

```json
{"ts":"2026-11-11T09:31:00+0800","level":"INFO","logger":"core.replay.engine",
 "message":"bar processed","run_id":"202611110001","strategy_id":"sma_cross",
 "task_id":"a1b2c3","event_sequence":18432,"simulated_time":"2025-01-06T09:31:00",
 "event_type":"BarReceived"}
```

在 Grafana Explore 里按 run_id 追一次回测：

```logql
{namespace="quant-core"} | json | run_id="202611110001"
```

> Loki 只是运行日志，**不是回测事件的权威存储**。事件以 Parquet 为准。

---

## 六、复现性

一次回测能复现，靠的是 manifest 把所有输入锁死，而不是"记得当时用了什么参数"。

`manifest.json` 记录：

- 行情数据集版本（`dataset_version` + 内容哈希）
- 策略 ID、代码版本（git commit）、参数
- 因子版本
- 撮合规则：滑点、成交量占比、涨跌停、T+1、部分成交
- 手续费：佣金率、最低佣金、印花税、过户费
- 初始资金、交易日历、复权规则
- 随机种子 `seed` 与运行环境（Python / 关键库版本）

配套机制：

- **模拟时钟**：`ReplayEngine` 单调推进，事件序号 `event_sequence` 连续
- **检查点**：每 N 个事件落一次 checkpoint，`Seek` 从最近检查点恢复后重放，而不是直接改时间变量
- **Golden Backtest**：CI 里跑固定数据集 + 固定策略，与
  `tests/golden_backtest/expected_metrics.json` 比对（交易数、成交价、手续费、资金曲线、
  收益率、最大回撤），容差 1e-6。核心代码改动导致结果漂移会直接让流水线红掉

---

## 七、开发与测试

```bash
ruff check .            # 代码检查（CI 同款）
pytest tests/unit       # 单元测试
pytest tests/integration# 集成测试（需要 MongoDB / RabbitMQ）
python scripts/run_golden.py --check   # golden 回测对比
```

CI（`.github/workflows/ci.yml`）四个阶段：`Ruff` → `Pytest(unit/integration)` →
`Golden Backtest` → `Docker build`（main 分支推送 `ghcr.io/<owner>/quant-replay-{api,worker,web}`）。
pip 依赖通过 `actions/setup-python` 的 `cache: pip` 缓存。

---

## 八、WSL2 注意事项

- K3s 的 Ingress 用 `hostNetwork` 直接占用 80/443，Windows 浏览器访问 `localhost` 依赖
  WSL2 的 localhost 转发；转发异常时用 `hostname -I` 得到的 IP，并在 Windows 防火墙放行。
- **不要把 WSL IP 写死**到任何配置文件，重启后地址会变。
- 数据文件放在 WSL2 的 Linux 文件系统（`/var/lib/quant-data`），不要放 `/mnt/c`，
  Parquet 的顺序读性能差一个数量级。
- 单机内存预算（64GB 主机）：系统 3–5G、MongoDB 2–4G、RabbitMQ 0.5–1G、
  API+Web 1–2G、回测 Worker 4–8G、监控 1.5–3G，其余留给文件缓存。
  **初期不要同时跑大量参数优化任务**。

更多细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/OPERATIONS.md](docs/OPERATIONS.md)。
