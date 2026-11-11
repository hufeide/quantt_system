"""API settings loaded from QR_-prefixed environment variables.

The QR_ names are the deployment contract; QUANT_ names are accepted as a
fallback so both the K8s configmap (QR_) and the local .env (QUANT_) work.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _aliases(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    """Runtime configuration for the API process.

    Every field can be set with QR_<FIELD>; the equivalent QUANT_<FIELD> is
    accepted as a fallback. Init kwargs use the plain field names.
    """

    model_config = SettingsConfigDict(
        env_prefix="QR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: str = Field("local", validation_alias=_aliases("QR_ENV", "QUANT_ENV"))
    app_name: str = "Quant Replay API"
    app_version: str = "1.0.0"
    log_level: str = Field(
        "INFO", validation_alias=_aliases("QR_LOG_LEVEL", "QUANT_LOG_LEVEL", "LOG_LEVEL")
    )
    log_json: bool = Field(True, validation_alias=_aliases("QR_LOG_JSON", "QUANT_LOG_JSON"))

    data_root: str = Field(
        "data_files", validation_alias=_aliases("QR_DATA_ROOT", "QUANT_DATA_ROOT")
    )
    output_root: str = Field(
        "data_files/backtests", validation_alias=_aliases("QR_OUTPUT_ROOT", "QUANT_OUTPUT_ROOT")
    )
    dataset_version: str = Field(
        "v001", validation_alias=_aliases("QR_DATASET_VERSION", "QUANT_DATASET_VERSION")
    )

    mongo_uri: str = Field(
        "mongodb://localhost:27017",
        validation_alias=_aliases(
            "QR_MONGO_URL", "QR_MONGO_URI", "QUANT_MONGO_URI", "QUANT_MONGO_URL", "MONGO_URL"
        ),
    )
    mongo_db: str = Field("quant", validation_alias=_aliases("QR_MONGO_DB", "QUANT_MONGO_DB"))
    mongo_timeout_ms: int = Field(
        500, validation_alias=_aliases("QR_MONGO_TIMEOUT_MS", "QUANT_MONGO_TIMEOUT_MS")
    )

    rabbitmq_url: str | None = Field(None, validation_alias=_aliases("QR_RABBITMQ_URL", "QUANT_RABBITMQ_URL"))
    celery_broker_url: str | None = Field(
        None,
        validation_alias=_aliases(
            "QR_CELERY_BROKER_URL", "CELERY_BROKER_URL", "QUANT_CELERY_BROKER_URL"
        ),
    )
    celery_result_backend: str | None = Field(
        None,
        validation_alias=_aliases(
            "QR_CELERY_RESULT_BACKEND", "CELERY_RESULT_BACKEND", "QUANT_CELERY_RESULT_BACKEND"
        ),
    )
    redis_url: str | None = Field(None, validation_alias=_aliases("QR_REDIS_URL", "QUANT_REDIS_URL"))

    prometheus_enabled: bool = Field(
        True, validation_alias=_aliases("QR_PROMETHEUS_ENABLED", "QUANT_PROMETHEUS_ENABLED")
    )
    metrics_port: int = Field(
        8000, validation_alias=_aliases("QR_METRICS_PORT", "QUANT_METRICS_PORT")
    )
    max_workers: int = Field(2, validation_alias=_aliases("QR_MAX_WORKERS", "QUANT_MAX_WORKERS"))
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"], validation_alias=_aliases("QR_CORS_ORIGINS", "QUANT_CORS_ORIGINS")
    )
    replay_event_buffer: int = Field(
        5000, validation_alias=_aliases("QR_REPLAY_EVENT_BUFFER", "QUANT_REPLAY_EVENT_BUFFER")
    )

    @property
    def mongo_url(self) -> str:
        """Backward-compatible alias for mongo_uri."""
        return self.mongo_uri

    @property
    def replay_root(self) -> Path:
        return Path(self.output_root) / "replay"

    @property
    def meta_dir(self) -> Path:
        return Path(self.output_root) / "_meta"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
