# 运维手册

## 1. 日常部署

```bash
# 首次
bash scripts/install_k3s.sh
bash scripts/import_images.sh
QUANT_DATA_ROOT=/var/lib/quant-data bash scripts/seed_data.sh
bash scripts/deploy.sh

# 只改代码：重新构建镜像并滚动更新
TAG=1.0.1 bash scripts/import_images.sh
sed -i 's/newTag: "1.0.0"/newTag: "1.0.1"/g' infrastructure/k3s/kustomization.yaml
kubectl apply -k infrastructure/k3s
kubectl -n quant-core rollout status deploy/backtest-worker
```

注意：`imagePullPolicy: IfNotPresent` 时，**同 tag 重新导入不会生效**，必须换 tag。

回滚：

```bash
kubectl -n quant-app rollout undo deploy/api
kubectl -n quant-core rollout undo deploy/backtest-worker
kubectl rollout history deploy/api -n quant-app
```

## 2. 配置变更

| 内容 | 位置 | 生效方式 |
| --- | --- | --- |
| 非敏感配置 | `applications/configmap.yaml` | `kubectl apply -k` 后 `rollout restart` |
| 密码 / 连接串 | `applications/secret.yaml`（示例值） | 用 `kubectl create secret` 覆盖后重启 Pod |
| 告警规则 | `monitoring/prometheus.yaml` 的 ConfigMap | `kubectl -n quant-monitor rollout restart deploy/prometheus` |
| Grafana 仪表盘 | `infrastructure/monitoring/grafana/dashboards/*.json` | 重跑 `scripts/deploy.sh`（会重建 ConfigMap 并重启 Grafana） |

Secret 示例值里带 `REPLACE_ME`，`deploy.sh` 会自动检测并告警。生产环境请用
`kubectl create secret generic` 或 sealed-secrets，**不要把真实密码提交进 git**。

## 3. 数据备份

```bash
# MongoDB：元数据（策略、任务、run manifest 索引）
kubectl -n quant-data exec sts/mongodb -- \
  mongodump --uri="mongodb://quant:<pwd>@localhost:27017/?authSource=admin" \
            --archive=/tmp/quant-$(date +%F).gz --gzip
kubectl -n quant-data cp mongodb-0:/tmp/quant-$(date +%F).gz ./backup/

# Parquet：行情与回测产物（宿主机目录，直接用 rsync）
rsync -av --delete /var/lib/quant-data/normalized/ /backup/quant/normalized/
rsync -av /var/lib/quant-data/backtests/ /backup/quant/backtests/
```

恢复：

```bash
kubectl -n quant-data cp ./backup/quant-2026-11-11.gz mongodb-0:/tmp/
kubectl -n quant-data exec sts/mongodb -- mongorestore --gzip --archive=/tmp/quant-2026-11-11.gz
```

## 4. 日志与排查

```bash
kubectl -n quant-app logs -f deploy/api --tail=200
kubectl -n quant-core logs -f deploy/backtest-worker --tail=200
kubectl -n quant-data logs sts/mongodb --tail=100
kubectl get events -A --sort-by=.lastTimestamp | tail -30
kubectl -n quant-app describe pod -l app=api
```

日志是单行 JSON，可以直接 grep：

```bash
kubectl -n quant-core logs deploy/backtest-worker | grep '"run_id":"202611110001"'
kubectl -n quant-core logs deploy/backtest-worker | grep '"level":"ERROR"' | tail -20
```

Loki（Grafana Explore）：

```logql
{namespace="quant-core"} | json | run_id="202611110001"
{namespace="quant-app"} | json | level="ERROR"
sum by (event_type) (count_over_time({namespace="quant-core"} | json | __error__="" [5m]))
```

## 5. 常见故障

| 现象 | 排查 | 处理 |
| --- | --- | --- |
| Pod `ImagePullBackOff` | `kubectl describe pod` | 镜像未导入：`bash scripts/import_images.sh`，或检查 ghcr 权限 |
| api 探针失败 | `curl localhost:8000/health`（容器内） | 检查 `QUANT_MONGO_URI` 是否还是 REPLACE_ME |
| worker 反复重启 | `kubectl logs` 看 broker 连接 | RabbitMQ 未就绪或密码不一致；`celery inspect ping` 验证 |
| 回测任务一直 pending | `rabbitmq_queue_messages_ready` | 没有 worker 在线：`kubectl -n quant-core get pods` |
| 队列有积压但 Bar 处理速率为 0 | Grafana `backtest-performance` | worker 卡死：`kubectl -n quant-core rollout restart deploy/backtest-worker` |
| `/grafana/` 502 | `kubectl -n quant-monitor logs deploy/grafana` | 检查 `GF_SERVER_ROOT_URL` 与 `serve_from_sub_path` |
| Grafana 里 worker 指标恒为 0 | `kubectl -n quant-core exec deploy/backtest-worker -- ls /tmp/prometheus-multiproc` | 缺 `PROMETHEUS_MULTIPROC_DIR` 时父进程看不到子进程计数；该变量与 emptyDir 已在清单里配好 |
| 磁盘告警 `DiskSpaceLow` | `df -h`、`du -sh /var/lib/quant-data/*` | 清理旧 run：保留 manifest + metrics.json，删 `events.parquet` |
| MongoDB 启动失败 | `kubectl -n quant-data describe pod mongodb-0` | PVC 未绑定或 local-path 目录权限问题 |

## 6. 扩缩容

```bash
# 加一个 worker 副本（注意内存：每副本 requests 2Gi / limits 6Gi）
kubectl -n quant-core scale deploy/backtest-worker --replicas=2

# 只跑某类队列（按需拆分专用 worker）
kubectl -n quant-core create deploy factor-worker --image=ghcr.io/quant-replay/quant-replay-worker:1.0.0 \
  -- celery -A apps.worker.celery_app worker -Q factor -c 1 -l info
```

单机阶段优先降并发而不是加副本：`-c 2` → `-c 1` 往往比多开一个 Pod 更稳。

## 7. 清理

```bash
# 清掉已完成的 Job
kubectl -n quant-core delete job --field-selector status.successful=1

# 清理 30 天前的回测产物（保留 manifest 与 metrics）
find /var/lib/quant-data/backtests -maxdepth 2 -name events.parquet -mtime +30 -delete

# 彻底重装（会丢数据，谨慎）
kubectl delete -k infrastructure/k3s
```

## 8. 升级 K3s

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik --write-kubeconfig-mode 644" sh -
kubectl get nodes -o wide
```

升级前先备份 MongoDB 与 `/var/lib/quant-data`，升级后确认 `ingress-nginx` 仍能访问。
