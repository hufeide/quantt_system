# 架构说明

## 1. 分层

| 层 | 组件 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 用户访问 | 浏览器 / Streamlit / API Client | 交互 | 不直接算指标 |
| 网络入口 | Nginx Ingress | 路由、超时、body 大小 | 不做业务鉴权以外的逻辑 |
| 业务服务 | `apps/api` (FastAPI) | 策略/回测/数据/任务管理，提交异步任务 | 不同步跑回测 |
| 任务调度 | RabbitMQ + `apps/worker` (Celery) | 队列、并发、重试、状态机 | 不实现撮合规则 |
| 计算核心 | `core/*` | 行情、回放、策略、撮合、账户、风控、因子 | 不 import FastAPI / Celery / pymongo |
| 数据存储 | MongoDB / Parquet / DuckDB | 元数据 / 行情与产物 / 本地分析 | 不成为同一份数据的多个权威源 |
| 可观测性 | Prometheus / Grafana / Loki | 指标、看板、日志 | 不作为回测事件存储 |

依赖方向：`apps/* → core`，`data → core 的数据接口`，`core` 零基础设施依赖。
这条约束保证了同一个回测核心可以跑在本地进程、Docker 容器、K3s Pod 里而行为一致。

## 2. 核心模块

| 模块 | 关键类/文件 | 说明 |
| --- | --- | --- |
| Market Data | `core/market/models.py`, `store.py`, `calendar.py` | Bar 模型、Parquet 数据集读取、交易日历 |
| Replay Engine | `core/replay/engine.py`, `session.py`, `checkpoint.py` | 模拟时钟、事件推进、Start/Pause/Resume/Step/Seek |
| Strategy Engine | `core/strategy/base.py`, `builtin.py`, `registry.py` | 只能看到当前时点已结束的 Bar |
| Matching Engine | `core/matching/engine.py`, `models.py` | 滑点、成交量占比、涨跌停、T+1、部分成交 |
| Account Engine | `core/account/engine.py` | 现金、持仓、手续费、权益曲线 |
| Risk Engine | `core/risk/engine.py` | 仓位/金额/回撤/下单频率/黑名单 |
| Factor Engine | `core/factor/engine.py` | 因子计算与版本化 |
| Event Store | `core/event/model.py` | 事件模型与 `event_sequence` |
| Backtest | `core/backtest/runner.py`, `metrics.py`, `artifacts.py` | 编排、指标、产物写出 |

## 3. 事件顺序保证

同一根 Bar 的生命周期严格按序：

```
BarReceived(n) → SignalGenerated(n) → OrderSubmitted → OrderFilled/OrderCancelled(n+1) → AccountUpdated
```

- 默认 `fill_on_same_bar = False`：信号在 n 产生，最早在 n+1 成交，避免"用未来信息成交"。
- 每个事件带连续递增的 `event_sequence`，checkpoint 记录该序号；
  `Seek` 只能从检查点恢复后按序重放，不能直接跳时间变量。
- 日志与事件共用同一组关联字段，便于用 `run_id` + `event_sequence` 对齐。

## 4. 数据权威源

| 数据 | 权威存储 | 说明 |
| --- | --- | --- |
| 原始行情 | 原始文件 + manifest | 只追加，不改写 |
| 标准化行情 | 版本化 Parquet（`dataset=v001`） | 回测只读这一份 |
| 业务元数据 | MongoDB | 策略、任务、run 状态、manifest 索引 |
| 回测产物 | Parquet（`backtests/run_id=...`） | 事件、订单、成交、持仓、权益、指标 |
| 运行日志 | Loki | 只用于排查，可随时丢弃 |

禁止让 MongoDB、DuckDB、ClickHouse 同时成为同一份行情数据的权威源。

## 5. 部署拓扑

六个 Namespace 只是逻辑隔离，单节点共享同一份 CPU / 内存 / 磁盘：

| Namespace | 工作负载 | 资源预算 |
| --- | --- | --- |
| `ingress` | ingress-nginx（DaemonSet + hostNetwork） | 0.25–0.5 Gi |
| `quant-app` | api（Deployment）、web（Deployment） | 1–2 Gi |
| `quant-core` | backtest-worker（Deployment） | 4–8 Gi |
| `quant-mq` | rabbitmq（StatefulSet），redis 可选 | 0.5–1 Gi |
| `quant-data` | mongodb（StatefulSet + 20Gi PVC） | 2–4 Gi |
| `quant-monitor` | prometheus / grafana / loki / exporters | 1.5–3 Gi |

存储：

- MongoDB、RabbitMQ 用 `local-path` StorageClass（K3s 自带 provisioner）。
- 行情与回测产物用 `hostPath` PV（`/var/lib/quant-data`），因为 api 与 worker 在不同
  namespace 却必须看到同一份文件；两个 PV 指向同一路径，各绑一个 PVC。
- 多节点阶段替换为 NFS / Longhorn / 对象存储，PVC 名称不变。

## 6. 关键决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 编排 | 单节点 K3s，禁用 Traefik | 保留标准 K8s 语义，未来加节点不改清单；入口统一交给 Nginx |
| 入口 | Nginx Ingress + hostNetwork | 直接占 80/443，WSL2 里访问最直接，少一层 NodePort |
| /api 前缀 | Ingress 正则 rewrite 掉（`/api/x` -> `/x`） | 后端规范路由无前缀，`/api` 只是给直连客户端的 `include_in_schema=False` 别名；对外统一走 `/api` |
| /grafana | 不 rewrite，配 `serve_from_sub_path` | 剥掉前缀会导致静态资源 404 |
| Worker 形态 | 固定副本 Deployment | 单机资源有限，每个回测一个 Job 会让内存峰值不可控 |
| Job 模板 | 提供但默认 `suspend: true` 且不入 kustomization | 保留演进路径，避免误 apply 白跑回测 |
| 共享数据 | hostPath PV | 单机无共享文件系统，读写性能优先 |
| 指标发现 | Pod 注解 + `kubernetes_sd` | 不引入 prometheus-operator，单机够用 |
| 日志字段 | 低基数进 label，高基数进 structured_metadata | 避免 run_id 造成 Loki 索引膨胀 |

## 7. 扩展路径

1. **加 Worker 节点**：新节点 join K3s，把 `backtest-worker` 的 replicas 调大；
   数据存储换成 NFS / Longhorn 后即可跨节点跑。
2. **任务隔离**：当出现"长回测挤占短任务"时，启用 `workers/backtest-job.yaml`，
   按 run_id 生成 Job，并给不同队列配不同 ResourceQuota。
3. **大规模分析**：Parquet → ClickHouse 作为分析副本，仍以 Parquet 为权威源。
4. **多环境**：把 `infrastructure/k3s` 拆成 base + overlays（kustomize），
   当前单环境刻意保持扁平。
