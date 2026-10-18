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
    "BUILTIN_PACK", "all_specs", "builtin_specs", "load_pack", "load_user_pack",
    "validate_spec", "validate_pack", "pack_summary",
]

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
ALLOWED_KINDS = ("adapter", "json_api", "html_scrape", "template_url", "local")
# P0 只存在这些内置实现；P2 起 json_api/html_scrape/template_url 由通用引擎驱动
KNOWN_IMPLS = ("kuwo", "kugou", "migu", "aggregator", "local",
               "wikimedia", "internet_archive", "manual")
SPEC_FIELDS = frozenset({
    "id", "label", "kind", "impl", "enabled_by_default", "offline",
    "home", "license_note", "params", "rate_limit", "min_app_version",
})
#: sources are tried in this order unless the user reorders them
# 顺序 = 命中概率 × 稳定性：先本地、再中文主流平台、再聚合通道，
# 最后才是 CC/PD 开放库（它们覆盖"冷门/古典/现场"，但通常不带流行曲）
DEFAULT_ORDER = ("local", "kuwo", "kugou", "migu", "aggregator",
                 "wikimedia", "internet_archive")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
#: 旧版本写死的默认音源名单（用于识别"这份配置其实没被用户改过"）
_LEGACY_DEFAULT_ORDER = ["local", "kuwo", "kugou", "aggregator"]
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
    if params.get("link_regex"):
        try:
            re.compile(str(params["link_regex"]))
        except re.error as exc:
            raise PackError(f"{where}: params.link_regex 不是合法正则（{exc}）") from exc

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
# built-in pack (equivalent to the hand-written adapters)
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
    SourceSpec(id="migu", label="咪咕音乐", kind="adapter", impl="migu",
               enabled_by_default=True, home="https://music.migu.cn/",
               license_note="公开 H5 接口（免登录 PQ），仅供个人试听/备份用途",
               rate_limit={"per_host_rps": 1.0, "burst": 2}),
    SourceSpec(id="aggregator", label="全网聚合(MyFreeMP3)", kind="adapter", impl="aggregator",
               enabled_by_default=True, home="https://music-api.gdstudio.xyz/",
               license_note="公共聚合网关，地址可由用户改为自建",
               rate_limit={"per_host_rps": 1.0, "burst": 2}),
    # 注：html_scrape（纯数据、零代码）的**示范**放在本文件顶部的 docstring 与
    # docs/备用资源获取模块-改进方案.md 里，不放进内置清单 —— 演示条目一旦被
    # 配置"显式列出"就会被当成真源，每次兜底白请求一次。需要时用用户清单加回：
    #   {"id": "example_open_audio", "label": "示例站点", "kind": "html_scrape",
    #    "params": {"search_url": "https://example.org/search?q={query}",
    #               "link_regex": "/track/(?P<id>[A-Za-z0-9_-]+)"}}
    # 同一个聚合网关的另一个通道（实测可用）：多一条通道就多一片曲库
    # 注：GDStudio 网关的 JOOX 通道（provider=joox）实测**能检索但取不到直链**
    # （url 恒为空）。它取不到文件却会把候选塞进池子、影响跨平台时长参考值，
    # 因此不放进内置清单。若该通道以后恢复，用用户清单一行即可加回：
    #   {"id": "aggregator_joox", "label": "聚合(JOOX)", "kind": "adapter",
    #    "impl": "aggregator", "params": {"provider": "joox"}}
    # 开放、免 key、允许自动化访问的两个 CC/PD 库：补"冷门/古典/现场"的覆盖
    SourceSpec(id="wikimedia", label="维基共享(CC/PD)", kind="adapter", impl="wikimedia",
               enabled_by_default=True, home="https://commons.wikimedia.org/",
               license_note="Wikimedia Commons：CC / 公有领域，开放 MediaWiki API",
               rate_limit={"per_host_rps": 0.5, "burst": 1}),
    SourceSpec(id="internet_archive", label="互联网档案馆(PD/CC)", kind="adapter",
               impl="internet_archive", enabled_by_default=True,
               home="https://archive.org/",
               license_note="Internet Archive：公有领域 / CC，开放检索 API，附官方时长",
               rate_limit={"per_host_rps": 0.5, "burst": 1}),
    # 人工补链：只有在用户填了链接（settings.manual_links）时才会被启用
    SourceSpec(id="manual", label="手动补链", kind="adapter", impl="manual",
               enabled_by_default=False, offline=True,
               license_note="用户自行提供的直链或本地文件，仅用于本人合法持有的资源"),
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
    # 迁移：v1.0.13 及更早版本会把一份写死的四源名单存进 settings.json。
    # 那**不是**用户的选择，而是旧默认值 —— 若原样当真，新加入的音源永远进不来。
    # 只有"恰好等于旧默认值"的名单才被忽略；其它任何名单都视为用户显式指定。
    if requested and [str(n) for n in requested] == _LEGACY_DEFAULT_ORDER:
        requested = None
    if requested:
        order = [str(n) for n in requested]
    else:
        order = list(DEFAULT_ORDER)
        order += [s.id for s in user if s.enabled_by_default and s.id not in order]
    # 用户填了补链就优先用它（显式意图优先于任何自动源）
    if settings.get("manual_links") and "manual" not in order:
        order = ["manual"] + list(order)
    return [by_id[n] for n in order if n in by_id]


def all_specs(settings: "dict | None" = None) -> list[SourceSpec]:
    """**所有可选**的源条目（内置 + 用户清单），不排序、不筛选。

    与 :func:`load_pack` 的区别：load_pack 返回"实际生效的链路"，
    这里返回"可选清单" —— 设置界面需要把**默认关闭**的源也列出来
    （例如只能检索、取不到直链的 JOOX 通道）。
    """
    settings = settings or {}
    by_id = {spec.id: spec for spec in builtin_specs()}
    user, _problems = load_user_pack(settings.get("source_pack_path"))
    for spec in user:
        by_id[spec.id] = spec
    return list(by_id.values())


def pack_summary(specs: "list[SourceSpec]") -> str:
    rows = [f"  {s.id:11} {s.kind:12} {'离线' if s.offline else '联网':4} {s.label}"
            for s in specs]
    return "\n".join(rows)