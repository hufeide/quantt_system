"""Streamlit UI for the quant replay & backtest system.

Start it with:

    streamlit run apps/web/app.py

Pages: overview, new backtest, result analysis, market replay, data browser.
The UI prefers the FastAPI service (QR_API_URL) and degrades to running core
locally, so a single node works with no services at all.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from apps.web import backend as be  # noqa: E402
from apps.web.plots import equity_figure, exposure_figure  # noqa: E402
from core.strategy import list_strategies  # noqa: E402

PAGES = ("概览", "新建回测", "结果分析", "行情回放", "数据浏览")


# --------------------------------------------------------------------------- helpers
def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _param_inputs(strategy: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Render one widget per declared strategy parameter."""
    schema: dict[str, dict[str, Any]] = spec.get("param_schema") or {}
    defaults: dict[str, Any] = spec.get("default_params") or {}
    params: dict[str, Any] = dict(defaults)
    if not schema:
        st.caption("该策略没有可配置参数。")
        return params

    columns = st.columns(min(3, len(schema)))
    for index, (key, field) in enumerate(schema.items()):
        widget_key = f"param_{strategy}_{key}"
        default = defaults.get(key, field.get("default"))
        column = columns[index % len(columns)]
        kind = field.get("type", "number")
        if kind == "integer":
            params[key] = int(
                column.number_input(
                    key,
                    min_value=int(field.get("min", 0)),
                    max_value=int(field.get("max", 1_000_000)),
                    value=int(default if default is not None else 0),
                    step=1,
                    key=widget_key,
                )
            )
        elif kind == "boolean":
            params[key] = bool(column.checkbox(key, value=bool(default), key=widget_key))
        elif kind == "string":
            params[key] = column.text_input(key, value=str(default or ""), key=widget_key)
        else:
            params[key] = float(
                column.number_input(
                    key,
                    min_value=float(field.get("min", 0.0)),
                    max_value=float(field.get("max", 1e9)),
                    value=float(default if default is not None else 0.0),
                    key=widget_key,
                )
            )
    return params


def _dataset_options(settings: be.Settings, client: be.ApiClient) -> list[dict[str, Any]]:
    datasets = be.list_datasets(settings.data_root)
    if datasets:
        return datasets
    if client.health():
        remote = []
        for item in client.list_datasets():
            frequency = item.get("frequency") or "1m"
            remote.append(
                {
                    "version": item.get("version", ""),
                    "frequencies": [frequency],
                    "frequency": frequency,
                    "symbols": item.get("symbols") or [],
                    "rows": int(item.get("rows") or 0),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "manifest": item.get("manifest") or {},
                }
            )
        return remote
    return []


def _runs_table(runs: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for run in runs[:50]:
        metrics = run.get("metrics") or {}
        rows.append(
            {
                "run_id": run.get("run_id"),
                "策略": run.get("strategy") or metrics.get("strategy", ""),
                "数据集": run.get("dataset_version") or metrics.get("dataset_version", ""),
                "总收益": _pct(metrics.get("total_return")),
                "夏普": _num(metrics.get("sharpe")),
                "最大回撤": _pct(metrics.get("max_drawdown")),
                "交易次数": metrics.get("trade_count", 0),
                "创建时间": run.get("created_at") or "",
            }
        )
    return pd.DataFrame(rows)


def _metric_cards(metrics: dict[str, Any]) -> None:
    first = st.columns(4)
    first[0].metric("总收益", _pct(metrics.get("total_return")))
    first[1].metric("年化收益", _pct(metrics.get("annualized_return")))
    first[2].metric("夏普比率", _num(metrics.get("sharpe")))
    first[3].metric("最大回撤", _pct(metrics.get("max_drawdown")))
    second = st.columns(4)
    second[0].metric("交易次数", _num(metrics.get("trade_count"), 0))
    second[1].metric("胜率", _pct(metrics.get("win_rate")))
    second[2].metric("期末权益", _num(metrics.get("final_equity"), 0))
    second[3].metric("总费用", _num(metrics.get("total_fees"), 2))


def _dataset_range(settings: be.Settings, version: str, dataset: dict[str, Any]) -> tuple[date, date]:
    start, end = dataset.get("start"), dataset.get("end")
    if start and end:
        try:
            return pd.Timestamp(start).date(), pd.Timestamp(end).date()
        except Exception:
            pass
    start_date, end_date = be.dataset_range(settings.data_root, version, dataset.get("frequency", "1m"))
    today = date.today()
    return start_date or today, end_date or today


# --------------------------------------------------------------------------- pages
def render_overview(settings: be.Settings, client: be.ApiClient) -> None:
    st.header("概览")
    runs, source = be.recent_runs(settings, client)
    st.caption(f"数据来源: {source} · 输出目录: {settings.output_root}")
    if not runs:
        st.info("暂无回测记录。请到「新建回测」页创建第一个回测。")
        return

    latest = runs[0]
    metrics = latest.get("metrics") or {}
    st.subheader("最新回测")
    st.caption(
        f"{latest['run_id']} · 策略 {latest.get('strategy') or metrics.get('strategy', '')} · "
        f"数据集 {metrics.get('dataset_version') or latest.get('dataset_version', '')}"
    )
    if metrics:
        _metric_cards(metrics)
    else:
        st.warning("该回测还没有产出指标（可能仍在运行）。")

    detail = be.load_run(settings.output_root, latest["run_id"])
    if not detail["equity"].empty:
        st.plotly_chart(equity_figure(detail["equity"]), key="overview_equity")

    st.subheader("最近回测")
    st.dataframe(_runs_table(runs), hide_index=True)


def render_new_backtest(settings: be.Settings, client: be.ApiClient) -> None:
    st.header("新建回测")
    datasets = _dataset_options(settings, client)
    if not datasets:
        st.warning("未发现数据集。请先把分钟数据写入 data_files/normalized/dataset=<version>/。")
        return

    version = st.selectbox("数据集版本", [item["version"] for item in datasets], key="nb_dataset")
    dataset = next(item for item in datasets if item["version"] == version)
    symbols = dataset.get("symbols") or be.dataset_symbols(
        settings.data_root, version, dataset.get("frequency", "1m")
    )
    selected = st.multiselect("标的", symbols, default=symbols[:1], key="nb_symbols")

    default_start, default_end = _dataset_range(settings, version, dataset)
    col_start, col_end, col_freq = st.columns(3)
    start = col_start.date_input("开始日期", value=default_start, key="nb_start")
    end = col_end.date_input("结束日期", value=default_end, key="nb_end")
    frequency = col_freq.selectbox("频率", dataset.get("frequencies") or ["1m"], key="nb_frequency")

    strategies = {item["name"]: item for item in list_strategies()}
    strategy = st.selectbox("策略", list(strategies), key="nb_strategy")
    st.caption(strategies[strategy].get("description", ""))
    params = _param_inputs(strategy, strategies[strategy])

    with st.expander("资金与费用", expanded=True):
        cash_col, commission_col, stamp_col, slippage_col = st.columns(4)
        initial_cash = cash_col.number_input(
            "初始资金", min_value=10_000.0, max_value=1e9, value=1_000_000.0, step=100_000.0, key="nb_cash"
        )
        commission = commission_col.number_input(
            "佣金费率", min_value=0.0, max_value=0.01, value=0.0003, format="%.5f", key="nb_commission"
        )
        stamp = stamp_col.number_input(
            "印花税率(卖出)", min_value=0.0, max_value=0.01, value=0.0005, format="%.5f", key="nb_stamp"
        )
        slippage = slippage_col.number_input(
            "滑点(bps)", min_value=0.0, max_value=100.0, value=1.0, step=0.5, key="nb_slippage"
        )

    with st.expander("撮合规则", expanded=False):
        match_col, part_col, bar_col = st.columns(3)
        t_plus_1 = match_col.checkbox("T+1", value=True, key="nb_t1")
        participation = part_col.slider("成交量参与率", 0.0, 1.0, 0.1, 0.01, key="nb_participation")
        fill_same_bar = bar_col.checkbox("当根K线成交", value=False, key="nb_same_bar")

    with st.expander("风控", expanded=False):
        # The core defaults (30% per position, 200k per order) reject most
        # strategy weights, so the UI exposes them and starts permissive.
        weight_col, value_col = st.columns(2)
        max_position_weight = weight_col.slider(
            "单标的最大权重", 0.05, 1.0, 0.95, 0.05, key="nb_max_weight"
        )
        max_order_value = value_col.number_input(
            "单笔最大金额",
            min_value=10_000.0,
            max_value=1e9,
            value=1_000_000.0,
            step=100_000.0,
            key="nb_max_order_value",
        )

    if st.button("运行回测", type="primary", key="nb_run"):
        if not selected:
            st.error("请至少选择一个标的。")
            return
        payload = {
            "strategy": strategy,
            "strategy_params": params,
            "symbols": list(selected),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "initial_cash": float(initial_cash),
            "frequency": frequency,
            "dataset_version": version,
            "fees": {
                "commission_rate": float(commission),
                "stamp_duty_rate": float(stamp),
            },
            "matching": {
                "slippage_bps": float(slippage),
                "t_plus_1": bool(t_plus_1),
                "participation_rate": float(participation),
                "fill_on_same_bar": bool(fill_same_bar),
            },
            "risk": {
                "max_position_weight": float(max_position_weight),
                "max_order_value": float(max_order_value),
            },
        }
        with st.spinner("回测运行中，请稍候..."):
            try:
                outcome = be.submit_backtest(settings, payload, client)
            except Exception as exc:  # surface the error instead of crashing the page
                st.error(f"回测失败: {exc}")
                return
        st.session_state["last_backtest"] = outcome

    outcome = st.session_state.get("last_backtest")
    if outcome:
        if outcome.get("source") == "api":
            st.success(f"任务已提交: {outcome.get('run_id')} (任务 {outcome.get('task_id')})")
        else:
            st.success(f"回测完成: {outcome.get('run_id')} (本地执行)")
        metrics = outcome.get("metrics") or {}
        if metrics:
            _metric_cards(metrics)
            with st.expander("完整指标"):
                st.json(metrics)
        else:
            st.info("任务已提交，稍后可在「概览」或「结果分析」页查看结果。")


def render_analysis(settings: be.Settings, client: be.ApiClient) -> None:
    st.header("结果分析")
    runs = [run for run in be.list_runs(settings.output_root) if Path(run.get("path", "")).exists()]
    if not runs:
        st.info("暂无本地回测结果，请先运行一次回测。")
        return

    run_id = st.selectbox("回测记录", [run["run_id"] for run in runs], key="an_run")
    detail = be.load_run(settings.output_root, run_id)
    metrics = detail["metrics"]
    if not metrics:
        st.warning("该目录缺少 metrics.json。")
        return

    _metric_cards(metrics)
    st.caption(
        f"策略 {metrics.get('strategy', '')} · 参数 {metrics.get('strategy_params', {})} · "
        f"区间 {metrics.get('start')} ~ {metrics.get('end')} · "
        f"标的 {', '.join(metrics.get('symbols') or []) or '-'}"
    )

    rejections = be.load_risk_rejections(settings.output_root, run_id)
    if not rejections.empty:
        counts = rejections["rule"].value_counts().to_dict()
        detail_text = "、".join(f"{rule} {count} 笔" for rule, count in counts.items())
        st.warning(
            f"风控拒绝了 {len(rejections)} 笔订单（{detail_text}）。"
            "若预期应有成交，请到「新建回测」页放宽风控限制后重跑。"
        )
        with st.expander("风控拒绝明细"):
            st.dataframe(rejections, hide_index=True)

    if not detail["equity"].empty:
        st.plotly_chart(equity_figure(detail["equity"]), key="analysis_equity")
        st.plotly_chart(exposure_figure(detail["equity"]), key="analysis_exposure")

    table_col, trade_col = st.columns([1, 2])
    with table_col:
        st.subheader("指标")
        frame = pd.DataFrame(
            [
                {"指标": key, "值": str(value)}
                for key, value in metrics.items()
                if not isinstance(value, (dict, list))
            ]
        )
        st.dataframe(frame, hide_index=True, height=420)
    with trade_col:
        st.subheader("成交明细")
        trades = detail["trades"]
        if trades.empty:
            st.info("本次回测没有成交。")
        else:
            st.dataframe(trades.tail(300), hide_index=True)
            st.download_button(
                "下载成交明细 CSV",
                trades.to_csv(index=False).encode("utf-8"),
                file_name=f"{run_id}_trades.csv",
                mime="text/csv",
                key="an_download_trades",
            )

    with st.expander("运行清单 manifest"):
        st.json(detail["manifest"])
    with st.expander("错误与警告"):
        errors = metrics.get("errors") or []
        if errors:
            for error in errors:
                st.warning(error)
        else:
            st.caption("没有记录到错误。")


def render_replay(settings: be.Settings, client: be.ApiClient) -> None:
    st.header("行情回放")
    datasets = _dataset_options(settings, client)
    if not datasets:
        st.warning("未发现数据集，无法启动回放。")
        return

    version = st.selectbox("数据集版本", [item["version"] for item in datasets], key="rp_dataset")
    dataset = next(item for item in datasets if item["version"] == version)
    symbols = dataset.get("symbols") or be.dataset_symbols(
        settings.data_root, version, dataset.get("frequency", "1m")
    )
    selected = st.multiselect("标的", symbols, default=symbols[:1], key="rp_symbols")
    strategies = {item["name"]: item for item in list_strategies()}
    strategy = st.selectbox("策略", list(strategies), key="rp_strategy")

    if st.button("创建回放会话", key="rp_build"):
        if not selected:
            st.error("请至少选择一个标的。")
        else:
            with st.spinner("正在加载行情并构建引擎..."):
                try:
                    session, events = be.build_replay_session(
                        settings,
                        dataset_version=version,
                        symbols=selected,
                        strategy=strategy,
                        params=dict(strategies[strategy].get("default_params") or {}),
                        frequency=dataset.get("frequency", "1m"),
                    )
                except Exception as exc:
                    st.error(f"创建回放会话失败: {exc}")
                    return
            st.session_state["replay"] = {"session": session, "events": events, "version": version}
            st.session_state.pop("replay_message", None)

    state = st.session_state.get("replay")
    if not state:
        st.info("先选择数据集与策略，然后点击「创建回放会话」。")
        return

    session = state["session"]
    events: list[dict[str, Any]] = state["events"]
    message = st.session_state.pop("replay_message", None)
    if message:
        st.success(message)

    controls = st.columns(6)
    if controls[0].button("开始", key="rp_start"):
        session.start()
    if controls[1].button("暂停", key="rp_pause"):
        session.pause()
    if controls[2].button("继续", key="rp_resume"):
        session.resume()
    if controls[3].button("单步", key="rp_step"):
        session.step(1)
    if controls[4].button("单步K线", key="rp_step_bar"):
        session.step_bar()
    if controls[5].button("全速运行", key="rp_run"):
        session.run()

    speed_col, apply_col, seek_col = st.columns([2, 1, 3])
    speed = speed_col.select_slider(
        "倍速",
        options=[0.0, 1.0, 5.0, 10.0, 60.0, 300.0],
        value=float(session.engine.speed) if float(session.engine.speed) in {0.0, 1.0, 5.0, 10.0, 60.0, 300.0} else 1.0,
        key="rp_speed",
        format_func=lambda value: "全速" if value == 0 else f"{value:g}x",
    )
    if apply_col.button("应用倍速", key="rp_apply_speed"):
        session.set_speed(float(speed))
        st.session_state["replay_message"] = f"倍速已设置为 {speed:g}x"

    status = session.status()
    target = seek_col.text_input(
        "跳转到 (YYYY-MM-DD HH:MM:SS)",
        value=status.get("clock") or "",
        key="rp_seek_value",
    )
    if seek_col.button("跳转", key="rp_seek"):
        try:
            session.seek(datetime.fromisoformat(target))
            st.session_state["replay_message"] = f"已跳转到 {target}"
        except Exception as exc:
            st.error(f"跳转失败: {exc}")

    st.divider()
    metrics = st.columns(6)
    metrics[0].metric("状态", status.get("state", ""))
    metrics[1].metric("模拟时钟", (status.get("clock") or "-").replace("T", " ")[:19])
    metrics[2].metric("已处理事件", status.get("processed_events", 0))
    metrics[3].metric("已处理K线", status.get("processed_bars", 0))
    metrics[4].metric("进度", f"{status.get('cursor', 0)}/{status.get('total_groups', 0)}")
    metrics[5].metric("检查点", status.get("checkpoints", 0))

    checkpoint_col, restore_col = st.columns(2)
    if checkpoint_col.button("保存检查点", key="rp_checkpoint"):
        try:
            checkpoint = session.checkpoint(label="manual")
            st.session_state["replay_message"] = f"已保存检查点 {checkpoint.checkpoint_id}"
            st.rerun()
        except Exception as exc:
            st.error(f"保存检查点失败: {exc}")

    checkpoints = session.checkpoints.list()
    if checkpoints:
        restore_id = restore_col.selectbox(
            "恢复检查点", [item.checkpoint_id for item in checkpoints], key="rp_restore_id"
        )
        if restore_col.button("恢复", key="rp_restore"):
            try:
                session.restore(restore_id)
                st.session_state["replay_message"] = f"已恢复 {restore_id}"
                st.rerun()
            except Exception as exc:
                st.error(f"恢复失败: {exc}")
    else:
        restore_col.caption("尚无检查点。")

    st.subheader("事件流")
    if events:
        st.dataframe(pd.DataFrame(events[-100:]), hide_index=True, height=300)
    else:
        st.caption("还没有事件，点击「开始」或「单步」推进回放。")

    if checkpoints:
        st.subheader("检查点")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "checkpoint_id": item.checkpoint_id,
                        "clock": item.clock,
                        "seq": item.seq,
                        "cursor": item.cursor,
                        "label": item.label,
                        "created_at": item.created_at,
                    }
                    for item in checkpoints
                ]
            ),
            hide_index=True,
        )


def render_data(settings: be.Settings, client: be.ApiClient) -> None:
    st.header("数据浏览")
    datasets = _dataset_options(settings, client)
    if not datasets:
        st.warning("未发现数据集目录 data_files/normalized/dataset=*。")
        return

    st.subheader("数据集列表")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "版本": item["version"],
                    "频率": ", ".join(item.get("frequencies") or []),
                    "标的数": len(item.get("symbols") or []),
                    "行数": item.get("rows") or 0,
                    "起始": str(item.get("start") or "-")[:19],
                    "结束": str(item.get("end") or "-")[:19],
                }
                for item in datasets
            ]
        ),
        hide_index=True,
    )

    version = st.selectbox("数据集版本", [item["version"] for item in datasets], key="db_version")
    dataset = next(item for item in datasets if item["version"] == version)
    frequency = st.selectbox("频率", dataset.get("frequencies") or ["1m"], key="db_frequency")

    stats = be.dataset_stats(settings.data_root, version, frequency)
    columns = st.columns(4)
    columns[0].metric("行数", _num(stats.get("rows"), 0))
    columns[1].metric("标的数", _num(len(stats.get("symbols") or []), 0))
    columns[2].metric("起始时间", str(stats.get("start") or "-")[:19])
    columns[3].metric("结束时间", str(stats.get("end") or "-")[:19])

    if stats.get("duplicates"):
        st.warning(f"检测到 {stats['duplicates']} 条重复的 (symbol, ts) 记录。")

    with st.expander("数据集清单 manifest", expanded=False):
        st.json(dataset.get("manifest") or {})

    st.subheader("数据预览")
    frame = be.dataset_frame(settings.data_root, version, frequency, limit=100)
    if frame.empty:
        st.info("该数据集没有数据。")
    else:
        st.dataframe(frame, hide_index=True)

    st.subheader("数据校验")
    if st.button("运行校验", key="db_validate"):
        st.session_state["db_report"] = be.validate_dataset(settings.data_root, version, frequency)
    report = st.session_state.get("db_report")
    if report:
        if report["ok"]:
            st.success(f"校验通过：{report['rows']} 行数据无结构性问题。")
        else:
            st.error("校验未通过：")
            for error in report["errors"]:
                st.write(f"- {error}")
        for warning in report["warnings"]:
            st.warning(warning)


# --------------------------------------------------------------------------- shell
def main() -> None:
    st.set_page_config(page_title="量化回放与回测", layout="wide")
    settings = be.Settings.from_env()
    client = be.ApiClient(settings)

    st.sidebar.title("量化回放系统")
    page = st.sidebar.radio("页面", PAGES, key="page")
    online = client.health()
    st.sidebar.caption(f"API: {'在线' if online else '离线（本地模式）'}")
    st.sidebar.caption(f"API 地址: {settings.api_url}")
    st.sidebar.caption(f"数据目录: {settings.data_root}")
    st.sidebar.caption(f"输出目录: {settings.output_root}")

    if page == "概览":
        render_overview(settings, client)
    elif page == "新建回测":
        render_new_backtest(settings, client)
    elif page == "结果分析":
        render_analysis(settings, client)
    elif page == "行情回放":
        render_replay(settings, client)
    else:
        render_data(settings, client)


main()
