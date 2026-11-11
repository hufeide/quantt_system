# Celery Worker

回测、因子、参数优化与维护任务的异步执行层。所有计算都调用 `core` 模块，Worker 只负责编排、进度上报与结果归一化。

## 启动

```bash
# 本地开发（消费全部队列）
.venv/bin/python -m apps.worker

# 只消费回测与因子队列，并发 4
.venv/bin/python -m apps.worker --queues backtest,factor --concurrency 4

# 生产（K8s / systemd）直接用 celery 入口
celery -A apps.worker.celery_app:celery_app worker \
  -Q replay,backtest,factor,optimization,maintenance -c 2 -l info
```

Worker 是常驻前台进程，由 systemd / K3s 负责拉起与重启，不要在脚本里后台常驻。

## 队列

| 队列 | 任务 | 说明 |
| --- | --- | --- |
| `backtest` | `quant.run_backtest` | 单次回测，失败自动重试 2 次 |
| `factor` | `quant.compute_factors` | 计算并落盘因子（Parquet） |
| `optimization` | `quant.optimize` | 网格参数优化，按 sharpe 排序 |
| `maintenance` | `quant.maintenance` | 数据校验、清理旧 run、检查点裁剪 |
| `replay` | 预留 | 交互式回放的长任务 |

队列名固定为以上五个，与部署配置保持一致。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUANT_CELERY_BROKER_URL` | 无 | 首选 broker 地址（K8s configmap） |
| `QUANT_RABBITMQ_URL` | 无 | broker 兜底地址 |
| `CELERY_BROKER_URL` | `amqp://guest:guest@localhost:5672//` | 本地/临时覆盖，优先级最高 |
| `QUANT_CELERY_RESULT_BACKEND` | 无 | 结果后端（如 redis） |
| `CELERY_RESULT_BACKEND` | `rpc://` | 结果后端本地覆盖 |
| `QUANT_WORKER_QUEUES` | 全部五个队列 | 默认消费队列（`--queues` 优先） |
| `CELERY_CONCURRENCY` | `2` | 并发进程数（回测吃内存，单机不要开太大） |
| `CELERY_TASK_ALWAYS_EAGER` / `QUANT_CELERY_TASK_ALWAYS_EAGER` | `0` | 设为 `1` 时任务在进程内同步执行，不需要 broker |
| `CELERY_TASK_TIME_LIMIT` | `3600` | 单任务硬超时（秒） |
| `QR_DATA_ROOT` | `data_files` | 行情根目录（config 未带 data_root 时的默认值） |
| `QR_OUTPUT_ROOT` | `data_files/backtests` | 回测产物根目录（config 未带 output_root 时的默认值） |
| `QUANT_LOG_LEVEL` / `LOG_LEVEL` | `INFO` | 日志级别 |
| `QUANT_LOG_JSON` | `1` | stdout 单行 JSON 日志（Loki 解析用） |
| `QUANT_METRICS_PORT` | 未设置 | 设置后在该端口暴露 Prometheus `/metrics` |
| `PROMETHEUS_MULTIPROC_DIR` | 未设置 | prefork 模式下让父进程汇总子进程指标 |

## 日志与指标

- 日志：stdout 单行 JSON，字段包含 `run_id` / `task_id` / `strategy_id` / `event_sequence` / `simulated_time` / `event_type`（由 `core.logging.log_context` 注入）。
- 指标（worker 独占命名，避免与 API 进程内指标冲突）：
  - `quant_worker_bars_processed_total{strategy}`
  - `quant_worker_backtest_total{strategy,status}`
  - `quant_worker_backtest_duration_seconds{strategy}`
  - `quant_factor_rows_total`
  - `quant_optimization_runs_total`

## 部署要点

- 优雅退出：`worker_prefetch_multiplier=1` + `task_acks_late=True` 已开启，SIGTERM 后未完成任务会重新投递，配合 `terminationGracePeriodSeconds=60`。
- 存活探针：`celery -A apps.worker.celery_app:celery_app inspect ping -d celery@$HOSTNAME`（需要 broker 可连）。
- 回测产物写入 `<output_root>/run_id=<run_id>/`，容器内请把 output_root 挂到持久卷。

## 任务返回结构

`run_backtest_task(config, run_id)` 返回 `run_id`、`metrics`、`artifacts`（相对名 -> 绝对路径）、`fingerprint`、`elapsed_seconds`、`output_dir` 等字段。

`optimize_task(config, grid, metric="sharpe")` 返回 `count`、`best`、`results`（已按 metric 降序排序）。

`maintenance_task(kind, ...)` 的 `cleanup` / `prune_checkpoints` 默认 `dry_run=True`，只预览不删除。

## 测试

测试通过 `CELERY_TASK_ALWAYS_EAGER=1` 同步执行，无需 RabbitMQ：

```bash
.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q
```
