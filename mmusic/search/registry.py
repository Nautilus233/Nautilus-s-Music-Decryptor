"""Build the fallback source chain from the declarative pack.

Behaviour is identical to the previous hard-coded registry - the only change
is that *which* sources exist, in what order, now comes from
:mod:`mmusic.search.pack` (data) instead of a literal dict (code).
"""
from __future__ import annotations

from typing import Optional

from .aggregator import AggregatorSource
from .base import SearchSource
from .kugou import KugouSource
from .kuwo import KuwoSource
from .local import LocalSource
from .pack import DEFAULT_ORDER, load_pack  # noqa: F401  (DEFAULT_ORDER re-exported)

#: built-in implementations addressable from a pack entry
_ADAPTERS: "dict[str, type[SearchSource]]" = {
    KuwoSource.name: KuwoSource,
    KugouSource.name: KugouSource,
    AggregatorSource.name: AggregatorSource,
    LocalSource.name: LocalSource,
}


def available_sources() -> list[str]:
    """Ids of the built-in implementations (unchanged public behaviour)."""
    return list(_ADAPTERS)


def specs_for(settings: Optional[dict] = None):
    """The ordered :class:`SourceSpec` list for these settings."""
    return load_pack(settings or {})


def build_sources(http, settings: Optional[dict] = None) -> list[SearchSource]:
    settings = settings or {}
    built: list[SearchSource] = []
    for spec in specs_for(settings):
        cls = _ADAPTERS.get(spec.impl)
        if cls is None:
            # a data-only spec (json_api / html_scrape / template_url) has no
            # implementation yet - P2 drives those; skip instead of failing.
            continue
        try:
            built.append(cls(http, settings))
        except Exception:  # noqa: BLE001 - a bad config must not kill the chain
            continue
    return built