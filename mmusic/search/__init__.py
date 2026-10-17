"""Fallback music-source adapters (only used when offline decode fails)."""
from .base import SearchSource  # noqa: F401
from .registry import build_sources  # noqa: F401