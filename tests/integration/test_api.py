"""End-to-end tests for the FastAPI service using TestClient."""
from __future__ import annotations

import time

import pytest

BACKTEST_PAYLOAD = {
    "strategy": "buy_and_hold",
    "strategy_params": {"weight": 0.5},
    "symbols": ["000001"],
    "start": "2025-01-06",
    "end": "2025-01-09",
    "initial_cash": 1_000_000,
    "dataset_version": "v001",
    "frequency": "1m",
    "risk": {"max_position_weight": 0.95, "max_order_value": 5_000_000},
}

TERMINAL = {"succeeded", "failed", "cancelled"}


def _wait_for_terminal(client, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/backtests/{run_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in TERMINAL:
            return detail
        time.sleep(0.05)
    pytest.fail(f"backtest {run_id} did not finish within {timeout}s: {detail}")


@pytest.fixture(scope="module")
def completed_run(api_env):
    client, _settings, _dataset = api_env
    response = client.post("/api/backtests", json=BACKTEST_PAYLOAD)
    assert response.status_code == 202, response.text
    body = response.json()
    detail = _wait_for_terminal(client, body["run_id"])
    assert detail["status"] == "succeeded", detail
    return detail


# --------------------------------------------------------------------------- ops
def test_health(api_env):
    client, _settings, _dataset = api_env
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["metadata_backend"] in {"pending", "json", "mongo"}


def test_routes_with_and_without_api_prefix(api_env):
    client, _settings, _dataset = api_env
    # Nginx rewrites /api/... -> /..., so both forms must be served.
    assert client.get("/strategies").status_code == 200
    assert client.get("/api/strategies").status_code == 200
    assert client.get("/backtests", params={"limit": 1}).status_code == 200
    assert client.get("/api/backtests", params={"limit": 1}).status_code == 200
    assert client.get("/data/datasets").status_code == 200
    assert client.get("/api/data/datasets").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_settings_quant_env(monkeypatch):
    from apps.api.settings import Settings

    monkeypatch.setenv("QUANT_ENV", "test")
    monkeypatch.setenv("QUANT_DATA_ROOT", "/data")
    monkeypatch.setenv("QUANT_MONGO_URI", "mongodb://mongo:27017")
    monkeypatch.setenv("QUANT_MONGO_DB", "quant")
    monkeypatch.setenv("QUANT_DATASET_VERSION", "v777")
    monkeypatch.setenv("QUANT_LOG_JSON", "true")
    monkeypatch.setenv("QUANT_PROMETHEUS_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.env == "test"
    assert settings.data_root == "/data"
    assert settings.mongo_uri == "mongodb://mongo:27017"
    assert settings.mongo_db == "quant"
    assert settings.dataset_version == "v777"
    assert settings.log_json is True
    assert settings.prometheus_enabled is True
    assert settings.mongo_url == settings.mongo_uri


def test_settings_qr_env(monkeypatch):
    from apps.api.settings import Settings

    monkeypatch.setenv("QR_DATA_ROOT", "/qrdata")
    monkeypatch.setenv("QR_OUTPUT_ROOT", "/qrdata/backtests")
    monkeypatch.setenv("QR_MONGO_URL", "mongodb://qr-mongo:27017")
    monkeypatch.setenv("QR_MONGO_DB", "qr")
    monkeypatch.setenv("QR_CELERY_BROKER_URL", "amqp://guest:guest@rabbit:5672//")
    monkeypatch.setenv("QR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("QR_MAX_WORKERS", "4")
    settings = Settings(_env_file=None)
    assert settings.data_root == "/qrdata"
    assert settings.output_root == "/qrdata/backtests"
    assert settings.mongo_uri == "mongodb://qr-mongo:27017"
    assert settings.mongo_db == "qr"
    assert settings.celery_broker_url == "amqp://guest:guest@rabbit:5672//"
    assert settings.log_level == "DEBUG"
    assert settings.max_workers == 4


def test_metrics_endpoint(api_env, completed_run):
    client, _settings, _dataset = api_env
    response = client.get("/metrics")
    assert response.status_code == 200
    text = response.text
    for name in (
        "quant_backtest_runs_total",
        "quant_backtest_duration_seconds",
        "quant_backtest_queue_depth",
        "quant_bars_processed_total",
        "quant_data_lag_seconds",
        "quant_data_missing_minutes",
        "quant_data_duplicate_bars",
        "quant_strategy_total_return_pct",
        "quant_strategy_max_drawdown_pct",
        "quant_strategy_win_rate",
        "quant_strategy_turnover",
    ):
        assert name in text, name
    assert 'quant_strategy_total_return_pct{strategy_id="buy_and_hold"}' in text

    completed = next(
        line
        for line in text.splitlines()
        if line.startswith('quant_backtest_runs_total{status="success"}')
    )
    assert float(completed.split()[-1]) >= 1.0
    bars = next(line for line in text.splitlines() if line.startswith("quant_bars_processed_total "))
    assert float(bars.split()[-1]) > 0


# --------------------------------------------------------------------------- strategies
def test_list_strategies(api_env):
    client, _settings, _dataset = api_env
    response = client.get("/api/strategies")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    names = {item["name"] for item in body["items"]}
    assert "sma_cross" in names
    sma = next(item for item in body["items"] if item["name"] == "sma_cross")
    assert "fast" in sma["param_schema"]
    assert sma["default_params"]["slow"] >= 2


# --------------------------------------------------------------------------- backtests
def test_backtest_detail(api_env, completed_run):
    client, _settings, _dataset = api_env
    detail = completed_run
    assert detail["run_id"]
    assert detail["task_id"]
    assert detail["strategy"] == "buy_and_hold"
    assert detail["metrics"]["trade_count"] >= 1
    assert detail["metrics"]["final_equity"] > 0
    assert detail["manifest"] is not None
    assert detail["manifest"]["strategy"] == "buy_and_hold"
    assert detail["artifacts"]
    assert detail["events_recorded"] > 0


def test_backtest_artifacts(api_env, completed_run):
    client, _settings, _dataset = api_env
    run_id = completed_run["run_id"]

    equity = client.get(f"/api/backtests/{run_id}/equity")
    assert equity.status_code == 200, equity.text
    body = equity.json()
    assert body["kind"] == "equity"
    assert body["count"] == body["total"] > 1
    assert "equity" in body["items"][0]

    limited = client.get(f"/api/backtests/{run_id}/equity", params={"limit": 5})
    assert limited.status_code == 200
    assert limited.json()["count"] == 5
    assert limited.json()["total"] == body["total"]

    trades = client.get(f"/api/backtests/{run_id}/trades")
    assert trades.status_code == 200
    assert trades.json()["total"] >= 1

    orders = client.get(f"/api/backtests/{run_id}/orders")
    assert orders.status_code == 200
    assert orders.json()["total"] >= 1

    positions = client.get(f"/api/backtests/{run_id}/positions")
    assert positions.status_code == 200
    assert positions.json()["total"] >= 1

    events = client.get(f"/api/backtests/{run_id}/events", params={"limit": 20})
    assert events.status_code == 200
    assert events.json()["count"] == 20
    assert events.json()["items"][0]["event_type"]

    invalid = client.get(f"/api/backtests/{run_id}/unknown-kind")
    assert invalid.status_code == 422


def test_backtest_list_filters(api_env, completed_run):
    client, _settings, _dataset = api_env
    response = client.get("/api/backtests", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert any(item["run_id"] == completed_run["run_id"] for item in body["items"])

    succeeded = client.get("/api/backtests", params={"status": "succeeded"})
    assert succeeded.status_code == 200
    assert succeeded.json()["total"] >= 1

    empty = client.get("/api/backtests", params={"status": "cancelled"})
    assert empty.status_code == 200
    assert all(item["status"] == "cancelled" for item in empty.json()["items"])

    bad = client.get("/api/backtests", params={"status": "nope"})
    assert bad.status_code == 400
    assert "未知状态" in bad.json()["detail"]


def test_tasks(api_env, completed_run):
    client, _settings, _dataset = api_env
    response = client.get("/api/tasks", params={"status": "succeeded"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    task_id = completed_run["task_id"]
    single = client.get(f"/api/tasks/{task_id}")
    assert single.status_code == 200
    assert single.json()["status"] == "succeeded"
    assert single.json()["run_id"] == completed_run["run_id"]


def test_accounts(api_env, completed_run):
    client, _settings, _dataset = api_env
    response = client.get("/api/accounts")
    assert response.status_code == 200
    body = response.json()
    snapshot = next(item for item in body["items"] if item["run_id"] == completed_run["run_id"])
    assert snapshot["status"] == "succeeded"
    assert snapshot["initial_cash"] == 1_000_000
    assert snapshot["final_equity"] > 0
    assert snapshot["cash"] is not None

    positions = client.get(f"/api/accounts/{completed_run['run_id']}/positions")
    assert positions.status_code == 200
    assert positions.json()["count"] >= 1
    assert positions.json()["as_of"]


def test_failed_backtest_is_recorded(api_env):
    client, _settings, _dataset = api_env
    payload = dict(BACKTEST_PAYLOAD)
    payload["symbols"] = ["999999"]
    created = client.post("/api/backtests", json=payload)
    assert created.status_code == 202, created.text
    body = created.json()
    detail = _wait_for_terminal(client, body["run_id"])
    assert detail["status"] == "failed"
    assert detail["error"]
    task = client.get(f"/api/tasks/{body['task_id']}")
    assert task.status_code == 200
    assert task.json()["status"] == "failed"


def test_cancel_and_delete(api_env):
    client, _settings, _dataset = api_env
    created = client.post("/api/backtests", json=BACKTEST_PAYLOAD)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]

    cancelled = client.post(f"/api/backtests/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    detail = _wait_for_terminal(client, run_id)
    assert detail["status"] in {"cancelled", "succeeded"}

    removed = client.delete(f"/api/backtests/{run_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True
    assert client.get(f"/api/backtests/{run_id}").status_code == 404


# --------------------------------------------------------------------------- data
def test_data_datasets(api_env):
    client, _settings, dataset = api_env
    response = client.get("/api/data/datasets")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    item = next(entry for entry in body["items"] if entry["version"] == "v001")
    assert item["frequency"] == "1m"
    assert item["rows"] == dataset["rows"]
    assert item["symbol_count"] == 2


def test_data_stats(api_env, completed_run):
    client, _settings, dataset = api_env
    response = client.get("/api/data/datasets/v001/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["rows"] == dataset["rows"]
    assert body["symbol_count"] == 2
    assert body["duplicate_bars"] == 0
    assert body["start"] and body["end"]

    missing = client.get("/api/data/datasets/v404/stats")
    assert missing.status_code == 404


def test_data_validate(api_env):
    client, _settings, dataset = api_env
    response = client.post("/api/data/validate", json={"version": "v001", "frequency": "1m"})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["checked_rows"] == dataset["rows"]
    assert body["symbols"] == 2
    assert body["issues"] == []

    filtered = client.post(
        "/api/data/validate", json={"version": "v001", "symbols": ["000001"], "start": "2025-01-07"}
    )
    assert filtered.status_code == 200
    assert filtered.json()["checked_rows"] < dataset["rows"]

    missing = client.post("/api/data/validate", json={"version": "v404"})
    assert missing.status_code == 404


def test_data_build_validation(api_env):
    client, _settings, _dataset = api_env
    bad = client.post("/api/data/build", json={"frequency": "2m"})
    assert bad.status_code == 422


def test_data_build_synthetic(api_env):
    client, _settings, _dataset = api_env
    response = client.post(
        "/api/data/build",
        json={
            "version": "vbuilt",
            "source": "synthetic",
            "symbols": ["600000.SH"],
            "start": "2025-01-06",
            "end": "2025-01-08",
            "seed": 3,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["rows"] > 0
    assert result["raw_file_count"] >= 1
    stats = client.get("/api/data/datasets/vbuilt/stats")
    assert stats.status_code == 200
    assert stats.json()["rows"] == result["rows"]


def test_data_build_without_builder(api_env, monkeypatch):
    client, _settings, _dataset = api_env
    monkeypatch.setattr("apps.api.routers.data._BUILDER_CANDIDATES", ())
    response = client.post("/api/data/build", json={"version": "v001"})
    assert response.status_code == 501
    assert "build_dataset" in response.json()["detail"]


# --------------------------------------------------------------------------- metadata store
def test_metadata_store_json_fallback(tmp_path):
    from apps.api.store import MetadataStore

    meta_dir = tmp_path / "meta"
    store = MetadataStore(mongo_url="mongodb://127.0.0.1:9", meta_dir=meta_dir, timeout_ms=100)
    store.create_run({"run_id": "run-1", "task_id": "task-1", "status": "pending"})
    store.create_task({"task_id": "task-1", "run_id": "run-1", "status": "pending"})
    assert store.backend == "json"
    assert (meta_dir / "runs.json").exists()
    assert (meta_dir / "tasks.json").exists()

    reloaded = MetadataStore(mongo_url="mongodb://127.0.0.1:9", meta_dir=meta_dir, timeout_ms=100)
    assert reloaded.get_run("run-1")["status"] == "pending"
    assert reloaded.get_task("task-1")["run_id"] == "run-1"


# --------------------------------------------------------------------------- replay
def test_replay_session_controls(api_env):
    client, _settings, _dataset = api_env
    created = client.post(
        "/api/replay/sessions",
        json={
            "strategy": "buy_and_hold",
            "strategy_params": {"weight": 0.3},
            "symbols": ["000001"],
            "start": "2025-01-06",
            "end": "2025-01-09",
            "dataset_version": "v001",
            "initial_cash": 1_000_000,
            "speed": 0,
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    assert created.json()["status"]["state"] == "idle"

    status = client.get(f"/api/replay/sessions/{session_id}")
    assert status.status_code == 200
    assert status.json()["run_id"] == session_id

    started = client.post(f"/api/replay/sessions/{session_id}/start")
    assert started.status_code == 200, started.text
    assert started.json()["status"]["state"] == "running"

    stepped = client.post(f"/api/replay/sessions/{session_id}/step", json={"events": 25})
    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["status"]["processed_events"] >= 25
    assert stepped.json()["status"]["clock"]

    paused = client.post(f"/api/replay/sessions/{session_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"]["state"] == "paused"

    resumed = client.post(f"/api/replay/sessions/{session_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"]["state"] == "running"

    speed = client.post(f"/api/replay/sessions/{session_id}/speed", json={"speed": 5})
    assert speed.status_code == 200
    assert speed.json()["status"]["speed"] == 5

    seeked = client.post(
        f"/api/replay/sessions/{session_id}/seek", json={"target": "2025-01-07T10:00:00"}
    )
    assert seeked.status_code == 200, seeked.text
    assert seeked.json()["status"]["clock"] >= "2025-01-07T10:00:00"
    assert seeked.json()["status"]["state"] == "paused"

    checkpoint = client.post(
        f"/api/replay/sessions/{session_id}/checkpoint", json={"label": "manual"}
    )
    assert checkpoint.status_code == 200, checkpoint.text
    checkpoint_body = checkpoint.json()
    checkpoint_id = checkpoint_body["checkpoint"]["checkpoint_id"]
    checkpoint_clock = checkpoint_body["checkpoint"]["clock"]
    assert checkpoint_body["checkpoint"]["label"] == "manual"

    advanced = client.post(f"/api/replay/sessions/{session_id}/step", json={"events": 15})
    assert advanced.status_code == 200
    assert advanced.json()["status"]["clock"] >= checkpoint_clock

    restored = client.post(
        f"/api/replay/sessions/{session_id}/restore", json={"checkpoint_id": checkpoint_id}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"]["clock"] == checkpoint_clock
    assert restored.json()["status"]["state"] == "paused"

    events = client.get(f"/api/replay/sessions/{session_id}/events", params={"limit": 10})
    assert events.status_code == 200
    assert events.json()["count"] > 0
    assert events.json()["items"][-1]["simulated_time"]

    bad_speed = client.post(f"/api/replay/sessions/{session_id}/speed", json={"speed": -1})
    assert bad_speed.status_code == 422

    unknown = client.get("/api/replay/sessions/does-not-exist")
    assert unknown.status_code == 404
    assert "不存在" in unknown.json()["detail"]


# --------------------------------------------------------------------------- errors
def test_not_found(api_env):
    client, _settings, _dataset = api_env
    response = client.get("/api/backtests/does-not-exist")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]

    task = client.get("/api/tasks/does-not-exist")
    assert task.status_code == 404

    positions = client.get("/api/accounts/does-not-exist/positions")
    assert positions.status_code == 404


def test_validation_errors(api_env):
    client, _settings, _dataset = api_env

    empty_strategy = client.post("/api/backtests", json={"strategy": ""})
    assert empty_strategy.status_code == 422
    assert "请求参数校验失败" in empty_strategy.json()["detail"]

    unknown_strategy = client.post("/api/backtests", json={"strategy": "no_such_strategy"})
    assert unknown_strategy.status_code == 400
    assert "未知策略" in unknown_strategy.json()["detail"]

    bad_frequency = client.post(
        "/api/backtests", json={"strategy": "buy_and_hold", "frequency": "7m"}
    )
    assert bad_frequency.status_code == 422

    unknown_field = client.post(
        "/api/backtests", json={"strategy": "buy_and_hold", "not_a_field": 1}
    )
    assert unknown_field.status_code == 422

    missing_dataset = client.post(
        "/api/backtests", json={"strategy": "buy_and_hold", "dataset_version": "v404"}
    )
    assert missing_dataset.status_code == 400
    assert "不存在" in missing_dataset.json()["detail"]

    negative_cash = client.post(
        "/api/backtests", json={"strategy": "buy_and_hold", "initial_cash": -1}
    )
    assert negative_cash.status_code == 422
