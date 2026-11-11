"""Strategy registry: name -> class, plus parameter metadata for the UI."""
from __future__ import annotations

from typing import Any, Callable

from core.strategy.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def decorator(cls: type[Strategy]) -> type[Strategy]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy '{name}'. available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](params)


def list_strategies() -> list[dict[str, Any]]:
    out = []
    for name, cls in sorted(_REGISTRY.items()):
        out.append(
            {
                "name": name,
                "description": cls.description,
                "default_params": dict(cls.default_params),
                "param_schema": dict(cls.param_schema),
            }
        )
    return out


def strategy_class(name: str) -> type[Strategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy '{name}'")
    return _REGISTRY[name]


def registry_names() -> list[str]:
    return sorted(_REGISTRY)
