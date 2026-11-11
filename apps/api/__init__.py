"""FastAPI service layer for the quant replay system."""

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    """Import create_app lazily so importing the package stays side-effect free."""
    if name == "create_app":
        from apps.api.main import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
