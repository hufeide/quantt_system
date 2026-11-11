# Streamlit Web UI

面向回测与回放的多视图界面，包含：概览、新建回测、结果分析、行情回放、数据浏览。

## 启动

```bash
# 本地
.venv/bin/python -m streamlit run apps/web/app.py

# 容器 / K8s（必须绑定 0.0.0.0，探针路径 /_stcore/health）
.venv/bin/python -m streamlit run apps/web/app.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

页面所有图表由 plotly 本地渲染，不访问任何 CDN，可在内网/离线机器上使用。

## 环境变量

Web 侧以 `QUANT_*` 为主、`QR_*` 兼容（infra 的部署清单两个都注入，取值等价）。注意 API 服务侧相反：`QR_*` 为主、`QUANT_*` 兜底（见 apps/api/settings.py），两边都设同一组值即可。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUANT_API_BASE_URL` | `http://localhost:8000` | 后端地址（容器内为 service DNS），优先级最高 |
| `QR_API_URL` | 同上 | 兼容别名，`QUANT_API_BASE_URL` 未设置时使用 |
| `QR_API_PREFIX` | `/api` | API 路由前缀；容器内直连 API 时可设为空字符串（客户端会自动补 `/api` 并回退裸路径） |
| `QR_API_TIMEOUT` | `2.0` | API 请求超时（秒） |
| `QUANT_DATA_ROOT` / `QR_DATA_ROOT` | `data_files` | 行情数据根目录 |
| `QUANT_OUTPUT_ROOT` / `QR_OUTPUT_ROOT` | `data_files/backtests` | 回测产物根目录；只设置 `QUANT_DATA_ROOT`（未设产物目录）时取 `<data_root>/backtests`。API 侧不会自动推导，因此本地只设数据目录时请同时设产物目录，避免 Web 与 API 读写不同目录 |

## API 与本地降级

启动时先探测 `GET {QUANT_API_BASE_URL}/health`：

- 在线：新建回测通过 `POST {base}/api/backtests` 提交，概览会合并 API 返回的任务列表。
- 离线：界面提示"离线（本地模式）"，新建回测直接在本进程调用 `core.backtest.run_backtest`，产物写入 output_root。

结果分析、行情回放、数据浏览始终读取本地磁盘上的产物与行情，因此单机部署无需 API 也能完整使用。容器部署时请保证 Web 与 API 挂载同一个数据卷。

## 页面说明

- **概览**：最新回测的关键指标卡片、权益曲线、最近回测列表。
- **新建回测**：数据集/标的/日期区间、策略与参数（按 `param_schema` 自动生成控件）、初始资金与费率、撮合规则、风控（单标的最大权重、单笔最大金额）。
  风控默认值（0.95 / 100 万）比 `core` 的默认值（0.30 / 20 万）宽松，否则界面默认参数下订单会被风控静默拒绝、回测显示 0 成交。
- **结果分析**：权益与回撤曲线、仓位曲线、指标表、成交明细（可下载 CSV）、运行清单。
  若该次回测存在 RiskRejected 事件（常见于 0 成交），页面顶部会提示被拒绝的笔数、按规则聚合的原因，并给出明细表，便于判断是风控过严还是策略没触发。
- **行情回放**：开始/暂停/继续/单步/单步K线/全速运行、倍速、按时间跳转、检查点保存与恢复，展示模拟时钟、进度与事件流。
- **数据浏览**：数据集列表与统计、manifest、数据预览、结构校验。

## 测试

```bash
.venv/bin/python -m pytest tests/integration/test_web_smoke.py -q
```

测试使用 `streamlit.testing.v1.AppTest` 渲染全部页面并点击关键按钮，API 地址指向关闭端口以覆盖本地降级路径。
