"""Build the fallback source chain from the declarative pack.

Behaviour is identical to the previous hard-coded registry - the only change
is that *which* sources exist, in what order, now comes from
:mod:`mmusic.search.pack` (data) instead of a literal dict (code).
"""
from __future__ import annotations

from typing import Optional

from .aggregator import AggregatorSource
from .base import SearchSource
from .html_source import HtmlScrapeSource
from .internet_archive import InternetArchiveSource
from .kugou import KugouSource
from .kuwo import KuwoSource
from .local import LocalSource
from .manual import ManualLinkSource
from .migu import MiguSource
from .pack import DEFAULT_ORDER, load_pack, SourceSpec  # noqa: F401
from .wikimedia import WikimediaCommonsSource

#: built-in implementations addressable from a pack entry
_ADAPTERS: "dict[str, type[SearchSource]]" = {
    KuwoSource.name: KuwoSource,
    KugouSource.name: KugouSource,
    MiguSource.name: MiguSource,
    AggregatorSource.name: AggregatorSource,
    LocalSource.name: LocalSource,
    WikimediaCommonsSource.name: WikimediaCommonsSource,
    InternetArchiveSource.name: InternetArchiveSource,
    ManualLinkSource.name: ManualLinkSource,
}


def available_sources() -> list[str]:
    """Ids of the built-in implementations (unchanged public behaviour)."""
    return list(_ADAPTERS)


def specs_for(settings: Optional[dict] = None):
    """The ordered :class:`SourceSpec` list for these settings."""
    return load_pack(settings or {})


def make_source(spec: SourceSpec, http, settings: Optional[dict] = None
                ) -> "SearchSource | None":
    """Instantiate one source from its spec, by *kind*.

    ``adapter``  -> one of the built-in hand-written adapters (P0 transition)
    ``html_scrape`` -> the generic, data-driven page extractor (P2)
    ``json_api`` / ``template_url`` -> not implemented yet; skipped, not failed.
    """
    settings = settings or {}
    try:
        if spec.kind == "html_scrape":
            return HtmlScrapeSource(http, settings, spec)
        if spec.kind in ("json_api", "template_url"):
            return None
        cls = _ADAPTERS.get(spec.impl)
        # 内置适配器都接受第三个参数 spec（默认 None），这样同一份实现
        # 可以被清单里的多条条目复用（例如聚合网关的不同平台通道）。
        source = cls(http, settings, spec) if cls is not None else None
        if source is not None:
            # 关键：name/label 必须取自**条目**而不是实现类 —— 否则同一实现
            # 的两条条目（aggregator / aggregator_joox）会同名，解析时张冠李戴。
            source.name = spec.id
            source.label = spec.label
        return source
    except Exception:  # noqa: BLE001 - a bad config must not kill the chain
        return None


def build_sources(http, settings: Optional[dict] = None) -> list[SearchSource]:
    settings = settings or {}
    built: list[SearchSource] = []
    for spec in specs_for(settings):
        source = make_source(spec, http, settings)
        if source is not None:
            built.append(source)
    return built