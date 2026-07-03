"""UI-facing platform registry and demo data for automation dashboards."""

from .platform_registry import get_platform, list_platforms
from .platform_store import platform_store

__all__ = [
    "get_platform",
    "list_platforms",
    "platform_store",
]
