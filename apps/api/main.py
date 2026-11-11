"""FastAPI application factory for the quant replay API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api import metrics as api_metrics
from apps.api.executor import BacktestJobManager
from apps.api.replay_manager import ReplaySessionManager
from apps.api.routers import accounts, backtests, data, replay, strategies, tasks
from apps.api.settings import Settings, get_settings
from apps.api.store import MetadataStore, utcnow
from core.logging import configure_logging

logger = logging.getLogger("apps.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API application.

    Nothing here touches the network: the metadata store connects lazily on the
    first request that needs it, and the executor only connects to a broker when
    CELERY_BROKER_URL is configured.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "starting %s v%s | data_root=%s output_root=%s max_workers=%s",
            settings.app_name,
            settings.app_version,
            settings.data_root,
            settings.output_root,
            settings.max_workers,
        )
        yield
        app.state.executor.shutdown()
        logger.info("API shutdown complete")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="单机版分钟级量化回放与回测系统 API",
        lifespan=lifespan,
    )

    origins = list(settings.cors_origins or ["*"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    store = MetadataStore(
        mongo_url=settings.mongo_uri,
        mongo_db=settings.mongo_db,
        meta_dir=settings.meta_dir,
        timeout_ms=settings.mongo_timeout_ms,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.executor = BacktestJobManager(settings, store)
    app.state.replay_manager = ReplaySessionManager(settings)
    app.state.started_at = utcnow()

    # Canonical routes have no /api prefix (Nginx rewrites /api/... -> /...).
    # The /api-prefixed aliases keep direct clients (web UI, curl) working.
    for router in (
        strategies.router,
        backtests.router,
        replay.router,
        data.router,
        accounts.router,
        tasks.router,
    ):
        app.include_router(router)
        app.include_router(router, prefix="/api", include_in_schema=False)

    # ------------------------------------------------------------------ handlers
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts: list[str] = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            message = str(error.get("msg", "invalid value"))
            parts.append(f"{location}: {message}" if location else message)
        return JSONResponse(status_code=422, content={"detail": "请求参数校验失败：" + "；".join(parts)})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看服务日志"})

    # ------------------------------------------------------------------ ops
    @app.get("/health", tags=["ops"], summary="健康检查")
    def health() -> dict:
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "env": settings.env,
            "time": utcnow(),
            "started_at": app.state.started_at,
            "metadata_backend": store.backend,
            "replay_sessions": app.state.replay_manager.count(),
        }

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标")
    def prometheus_metrics() -> Response:
        payload, content_type = api_metrics.render()
        return Response(content=payload, media_type=content_type)

    @app.get("/", tags=["ops"], summary="服务信息")
    def root() -> dict:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/health",
            "metrics": "/metrics",
        }

    return app


app = create_app()
