"""Shared FastAPI dependencies backed by app.state."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from apps.api.executor import BacktestJobManager
from apps.api.replay_manager import ReplaySessionManager
from apps.api.settings import Settings
from apps.api.store import MetadataStore


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> MetadataStore:
    return request.app.state.store


def get_executor(request: Request) -> BacktestJobManager:
    return request.app.state.executor


def get_replay_manager(request: Request) -> ReplaySessionManager:
    return request.app.state.replay_manager


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
StoreDep = Annotated[MetadataStore, Depends(get_store)]
ExecutorDep = Annotated[BacktestJobManager, Depends(get_executor)]
ReplayDep = Annotated[ReplaySessionManager, Depends(get_replay_manager)]
