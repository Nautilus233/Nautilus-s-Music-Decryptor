"""Declarative source pack (声明式源清单).

A *source is data, not code*: the pack only describes **which** sources
exist, in what order, with what limits and licence notes.  The actual
request/parse logic stays in built-in adapters during the P0 transition
(``kind: "adapter"``); from P2 onwards ``json_api`` / ``html_scrape`` /
``template_url`` specs will be driven by the generic engine.

Consequences that matter for safety:

* adding or fixing a source usually means editing one JSON entry;
* a remote pack can **never execute code** - the schema is a strict
  whitelist, so unknown keys or kinds are rejected outright.

No third-party imports: this module only uses ``re`` and ``dataclasses``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ALLOWED_KINDS", "KNOWN_IMPLS", "DEFAULT_ORDER", "PackError", "SourceSpec",
    "BUILTIN_PACK", "builtin_specs", "load_pack", "load_user_pack",
    "validate_spec", "validate_pack", "pack_summary",
]

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
ALLOWED_KINDS = ("adapter", "json_api", "html_scrape", "template_url", "local")
# P0 只存在这些内置实现；P2 起 json_api/html_scrape/template_url 由通用引擎驱动
KNOWN_IMPLS = ("kuwo", "kugou", "aggregator", "local")
SPEC_FIELDS = frozenset({
    "id", "label", "kind", "impl", "enabled_by_default", "offline",
    "home", "license_note", "params", "rate_limit", "min_app_version",
})
#: sources are tried in this order unless the user reorders them
DEFAULT_ORDER = ("local", "kuwo", "kugou", "aggregator")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_HTTPS_RE = re.compile(r"^https://[^\s]+$")


class PackError(ValueError):
    """The pack (or one spec inside it) violates the schema."""


@dataclass(frozen=True)
class SourceSpec:
    id: str
    label: str
    kind: str
    impl: str = ""
    enabled_by_default: bool = False
    offline: bool = False
    home: str = ""
    license_note: str = ""
    params: dict = field(default_factory=dict)
    rate_limit: dict = field(default_factory=dict)
    min_app_version: str = ""

    @property
    def network(self) -> bool:
        return not self.offline


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _need_str(raw: dict, key: str, *, where: str, required: bool = True) -> str:
    value = raw.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PackError(f"{where}: 字段 {key!r} 必须是字符串")
    if required and not value.strip():
        raise PackError(f"{where}: 缺少必填字段 {key!r}")
    return value.strip()


def validate_spec(raw: object, *, where: str = "spec") -> SourceSpec:
    """Validate one raw dict and return a frozen :class:`SourceSpec`."""
    if not isinstance(raw, dict):
        raise PackError(f"{where}: 条目必须是对象")

    unknown = set(raw) - SPEC_FIELDS
    if unknown:
        raise PackError(f"{where}: 未知字段 {sorted(unknown)}（schema 为严格白名单）")

    sid = _need_str(raw, "id", where=where)
    if not _ID_RE.match(sid):
        raise PackError(f"{where}: id {sid!r} 非法（小写字母开头，[a-z0-9_]，2-32 位）")

    label = _need_str(raw, "label", where=where)
    kind = _need_str(raw, "kind", where=where)
    if kind not in ALLOWED_KINDS:
        raise PackError(f"{where}: 不支持的 kind {kind!r}（白名单：{list(ALLOWED_KINDS)}）")

    impl = _need_str(raw, "impl", where=where, required=False)
    offline = bool(raw.get("offline", kind == "local"))

    if kind == "adapter":
        if not impl:
            raise PackError(f"{where}: kind=adapter 必须给出 impl")
        if impl not in KNOWN_IMPLS:
            raise PackError(f"{where}: 未知实现 impl {impl!r}（可用：{list(KNOWN_IMPLS)}）")
    elif impl and impl not in KNOWN_IMPLS:
        raise PackError(f"{where}: 未知实现 impl {impl!r}")

    # 数据驱动的源必须给出 https 模板；P0 的 adapter 不受此限
    if kind in ("json_api", "html_scrape", "template_url"):
        search = raw.get("params", {}).get("search_url", "")
        if not isinstance(search, str) or not _HTTPS_RE.match(search):
            raise PackError(f"{where}: kind={kind} 需要 params.search_url 且必须是 https://")

    home = _need_str(raw, "home", where=where, required=False)
    if home and not _HTTPS_RE.match(home):
        raise PackError(f"{where}: home 必须是 https:// 链接")

    rate = raw.get("rate_limit") or {}
    if not isinstance(rate, dict):
        raise PackError(f"{where}: rate_limit 必须是对象")
    rps = rate.get("per_host_rps", 1.0)
    if not isinstance(rps, (int, float)) or rps <= 0:
        raise PackError(f"{where}: rate_limit.per_host_rps 必须是正数")

    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise PackError(f"{where}: params 必须是对象")

    return SourceSpec(
        id=sid, label=label, kind=kind, impl=impl or sid,
        enabled_by_default=bool(raw.get("enabled_by_default", False)),
        offline=offline, home=home,
        license_note=_need_str(raw, "license_note", where=where, required=False),
        params=dict(params), rate_limit=dict(rate),
        min_app_version=_need_str(raw, "min_app_version", where=where, required=False),
    )


def validate_pack(data: object) -> list[SourceSpec]:
    """Validate a whole pack document (``{"schema":1,"sources":[...]}``)."""
    if not isinstance(data, dict):
        raise PackError("pack: 顶层必须是对象")
    unknown = set(data) - {"schema", "pack_version", "min_app_version", "sources"}
    if unknown:
        raise PackError(f"pack: 未知字段 {sorted(unknown)}")
    if data.get("schema") != 1:
        raise PackError(f"pack: 不支持的 schema {data.get('schema')!r}（当前支持 1）")
    sources = data.get("sources")
    if not isinstance(sources, list):
        raise PackError("pack: sources 必须是数组")
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(sources):
        spec = validate_spec(raw, where=f"sources[{i}]")
        if spec.id in seen:
            raise PackError(f"sources[{i}]: id {spec.id!r} 重复")
        seen.add(spec.id)
        specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# built-in pack (equivalent to the four hand-written adapters)
# ---------------------------------------------------------------------------
BUILTIN_PACK: tuple[SourceSpec, ...] = (
    SourceSpec(id="local", label="本地曲库(CC0/自建)", kind="local", impl="local",
               enabled_by_default=True, offline=True, license_note="用户自有 / CC0 资源"),
    SourceSpec(id="kuwo", label="酷我音乐", kind="adapter", impl="kuwo",
               enabled_by_default=True, home="https://www.kuwo.cn/",
               license_note="公开检索接口，仅按歌名定向查询",
               rate_limit={"per_host_rps": 1.0, "burst": 3}),
    SourceSpec(id="kugou", label="酷狗音乐", kind="adapter", impl="kugou",
               enabled_by_default=True, home="https://www.kugou.com/",
               license_note="公开检索接口，仅按歌名定向查询",
               rate_limit={"per_host_rps": 1.0, "burst": 3}),
    SourceSpec(id="aggregator", label="全网聚合(MyFreeMP3)", kind="adapter", impl="aggregator",
               enabled_by_default=True, home="https://music-api.gdstudio.xyz/",
               license_note="公共聚合网关，地址可由用户改为自建",
               rate_limit={"per_host_rps": 1.0, "burst": 2}),
)


def builtin_specs() -> list[SourceSpec]:
    return list(BUILTIN_PACK)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_user_pack(path: "str | Path | None") -> "tuple[list[SourceSpec], list[str]]":
    """Load a user-supplied pack file.  Returns ``(specs, problems)`` and never
    raises: a broken pack must not stop the app from running."""
    if not path:
        return [], []
    p = Path(path)
    if not p.is_file():
        return [], [f"源清单不存在：{p}"]
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [], [f"源清单解析失败：{exc}"]
    try:
        return validate_pack(data), []
    except PackError as exc:
        return [], [str(exc)]


def load_pack(settings: "dict | None" = None) -> list[SourceSpec]:
    """Resolve the effective, ordered source list.

    Order/selection rules (identical to the previous hard-coded registry):
    an explicit ``settings["sources"]`` wins; otherwise the default order,
    plus any *opt-in* user-pack source that declares itself enabled.
    """
    settings = settings or {}
    specs = builtin_specs()
    user, _problems = load_user_pack(settings.get("source_pack_path"))
    by_id = {s.id: s for s in specs}
    for spec in user:                      # user pack may override a built-in
        by_id[spec.id] = spec

    requested = settings.get("sources")
    if requested:
        order = [str(n) for n in requested]
    else:
        order = list(DEFAULT_ORDER)
        order += [s.id for s in user if s.enabled_by_default and s.id not in order]
    return [by_id[n] for n in order if n in by_id]


def pack_summary(specs: "list[SourceSpec]") -> str:
    rows = [f"  {s.id:11} {s.kind:12} {'离线' if s.offline else '联网':4} {s.label}"
            for s in specs]
    return "\n".join(rows)