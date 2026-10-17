from __future__ import annotations

from typing import Optional

from .aggregator import AggregatorSource
from .base import SearchSource
from .kugou import KugouSource
from .kuwo import KuwoSource
from .local import LocalSource

# ordered default fallback chain: local first (free), then the online ones
DEFAULT_ORDER = ["local", "kuwo", "kugou", "aggregator"]

_REGISTRY: "dict[str, type[SearchSource]]" = {
    KuwoSource.name: KuwoSource,
    KugouSource.name: KugouSource,
    AggregatorSource.name: AggregatorSource,
    LocalSource.name: LocalSource,
}


def available_sources() -> list[str]:
    return list(_REGISTRY)


def build_sources(http, settings: Optional[dict] = None) -> list[SearchSource]:
    settings = settings or {}
    order = settings.get("sources") or DEFAULT_ORDER
    built: list[SearchSource] = []
    for name in order:
        cls = _REGISTRY.get(name)
        if cls is None:
            continue
        try:
            built.append(cls(http, settings))
        except Exception:  # noqa: BLE001 - a bad config must not kill the chain
            continue
    return built