"""Streamlit UI smoke test: every page must render without exceptions.

The UI is driven through streamlit.testing.v1.AppTest with a synthetic dataset
and no API process (QR_API_URL points at a closed port), which exercises the
local-core fallback path.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from core.storage.parquet import BAR_SCHEMA, write_dataframe  # noqa: E402

APP_PATH = Path(__file__).resolve().parents[2] / "apps" / "web" / "app.py"
PAGES = ("概览", "新建回测", "结果分析", "行情回放", "数据浏览")
VERSION = "vtest"


def write_synthetic_dataset(
    root: Path,
    version: str = VERSION,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
    days: int = 2,
    bars_per_day: int = 120,
    seed: int = 11,
) -> Path:
    """Minimal hive-partitioned dataset so the UI has something to render."""
    rng = np.random.default_rng(seed)
    rows = []
    day = date(2024, 1, 2)
    for _ in range(days):
        start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
        for i in range(bars_per_day):
            ts = start + timedelta(minutes=i)
            for symbol in symbols:
                base = 10.0 if symbol == "AAA" else 20.0
                drift = 0.0005 * np.sin(i / 5.0) + rng.normal(0, 0.0012)
                close = round(base * (1.0 + drift * (i + 1) / 8.0), 4)
                open_ = round(close * (1.0 + rng.normal(0, 0.0004)), 4)
                high = round(max(open_, close) * 1.0008, 4)
                low = round(min(open_, close) * 0.9992, 4)
                volume = float(rng.integers(80_000, 250_000))
                rows.append(
                    {
                        "symbol": symbol,
                        "ts": ts,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "amount": close * volume,
                        "frequency": "1m",
                        "dataset_version": version,
                    }
                )
        day = day + timedelta(days=1)

    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"]).astype("datetime64[ms]")
    path = root / "normalized" / f"dataset={version}" / "frequency=1m" / "year=2024" / "part-0.parquet"
    write_dataframe(frame, path, schema=BAR_SCHEMA)

    manifest = {
        "dataset_version": version,
        "frequency": "1m",
        "source": "synthetic",
        "symbols": list(symbols),
        "start": frame["ts"].min().isoformat(),
        "end": frame["ts"].max().isoformat(),
        "row_count": int(len(frame)),
        "timezone": "Asia/Shanghai",
    }
    (root / "normalized" / f"dataset={version}" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return path


@pytest.fixture()
def web_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_synthetic_dataset(tmp_path)
    monkeypatch.setenv("QR_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("QR_OUTPUT_ROOT", str(tmp_path / "backtests"))
    # Closed port: the UI must fall back to local core execution.
    monkeypatch.setenv("QR_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("QR_API_TIMEOUT", "0.2")
    return tmp_path


def test_settings_follow_deployment_contract(monkeypatch: pytest.MonkeyPatch):
    from apps.web.backend import Settings

    monkeypatch.delenv("QR_API_URL", raising=False)
    monkeypatch.delenv("QR_OUTPUT_ROOT", raising=False)
    monkeypatch.setenv("QUANT_API_BASE_URL", "http://api.quant-app.svc.cluster.local:8000/")
    monkeypatch.setenv("QUANT_DATA_ROOT", "/data")

    settings = Settings.from_env()
    assert settings.api_url == "http://api.quant-app.svc.cluster.local:8000"
    assert settings.data_root == "/data"
    # artifacts stay next to the mounted dataset inside the container
    assert settings.output_root == "/data/backtests"


def test_api_client_candidates_cover_empty_and_prefixed(monkeypatch: pytest.MonkeyPatch):
    from apps.web.backend import ApiClient, Settings

    monkeypatch.setenv("QR_API_URL", "http://api.quant-app.svc.cluster.local:8000")
    monkeypatch.setenv("QR_API_PREFIX", "")

    client = ApiClient(Settings.from_env())
    # /health is unprefixed on the API, routers live under /api
    assert client._candidates("/health") == [
        "http://api.quant-app.svc.cluster.local:8000/api/health",
        "http://api.quant-app.svc.cluster.local:8000/health",
    ]
    assert client._candidates("/backtests")[0].endswith("/api/backtests")

    monkeypatch.setenv("QR_API_PREFIX", "/api")
    prefixed = ApiClient(Settings.from_env())
    assert prefixed._candidates("/backtests")[0] == "http://api.quant-app.svc.cluster.local:8000/api/backtests"


def make_app() -> AppTest:
    return AppTest.from_file(str(APP_PATH), default_timeout=180)


def test_app_renders_all_pages(web_env: Path):
    at = make_app()
    at.run()
    assert not at.exception, [str(err.value) for err in at.exception]
    assert at.sidebar.radio[0].options == list(PAGES)
    captions = [caption.value for caption in at.sidebar.caption]
    assert any("离线" in text for text in captions)

    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page} raised {[str(err.value) for err in at.exception]}"


def test_app_runs_backtest_locally_and_analyses_result(web_env: Path):
    at = make_app()
    at.run()
    at.sidebar.radio[0].set_value("新建回测").run()
    assert not at.exception

    at.button(key="nb_run").click().run()
    assert not at.exception, [str(err.value) for err in at.exception]
    assert any("回测完成" in message.value for message in at.success)

    run_dirs = sorted((web_env / "backtests").glob("run_id=*"))
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "metrics.json").exists()
    assert (run_dirs[0] / "equity.parquet").exists()

    # Default form values must produce fills: the page ships permissive risk
    # limits, otherwise every order would be rejected silently.
    metrics = json.loads((run_dirs[0] / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["trade_count"] >= 1, "default UI parameters produced no trades"

    at.sidebar.radio[0].set_value("结果分析").run()
    assert not at.exception, [str(err.value) for err in at.exception]
    assert len(at.get("plotly_chart")) >= 1
    assert at.selectbox[0].options == [run_dirs[0].name.split("=", 1)[-1]]


def test_risk_rejections_are_surfaced(web_env: Path):
    """A zero-trade run must explain itself: rejections live in events.parquet."""
    from apps.web import backend as be

    settings = be.Settings.from_env()
    outcome = be.run_backtest_local(
        {
            "strategy": "sma_cross",
            "strategy_params": {"fast": 3, "slow": 8, "weight": 0.9},
            "symbols": ["AAA"],
            "start": "2024-01-02",
            "end": "2024-01-03",
            "initial_cash": 1_000_000.0,
            "frequency": "1m",
            "dataset_version": VERSION,
            # core defaults: a 90% target weight is rejected on both rules
            "risk": {"max_position_weight": 0.30, "max_order_value": 200_000.0},
        },
        settings,
    )

    rejections = be.load_risk_rejections(settings.output_root, outcome["run_id"])
    assert not rejections.empty
    assert set(rejections["rule"]) <= {"max_position_weight", "max_order_value"}
    assert (rejections["reason"] != "").any()

    at = make_app()
    at.run()
    at.sidebar.radio[0].set_value("结果分析").run()
    assert not at.exception, [str(err.value) for err in at.exception]
    assert any("风控拒绝" in warning.value for warning in at.warning)


def test_app_replay_controls(web_env: Path):
    at = make_app()
    at.run()
    at.sidebar.radio[0].set_value("行情回放").run()
    assert not at.exception

    at.button(key="rp_build").click().run()
    assert not at.exception, [str(err.value) for err in at.exception]

    at.button(key="rp_step_bar").click().run()
    assert not at.exception
    assert any(metric.label == "已处理K线" for metric in at.metric)

    at.button(key="rp_checkpoint").click().run()
    assert not at.exception
    assert any("检查点" in message.value for message in at.success)

    restore_options = [option for box in at.selectbox for option in box.options]
    assert any(str(option).startswith("ckpt_") for option in restore_options)


def test_app_data_browser_validates_dataset(web_env: Path):
    at = make_app()
    at.run()
    at.sidebar.radio[0].set_value("数据浏览").run()
    assert not at.exception

    at.button(key="db_validate").click().run()
    assert not at.exception
    assert any("校验通过" in message.value for message in at.success)
    assert not at.error
