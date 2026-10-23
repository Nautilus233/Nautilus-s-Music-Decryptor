"""Verified Chinese album aliases, local first with a bounded Wikidata fallback."""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache

from ..match import cross_similarity
from ..paths import resource_path

_API = "https://www.wikidata.org/w/api.php"
_HEADERS = {"User-Agent": "MusicDecryptor/1.0 (local album title lookup)"}
_ALBUM_TYPES = {"Q482994", "Q169930"}  # album, extended play
_ID = re.compile(r"^Q[1-9][0-9]*$")
_TIMEOUT = 4.0


@dataclass(frozen=True)
class AlbumAlias:
    entity_id: str
    titles: tuple[str, ...]
    artists: tuple[str, ...]


def _key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\s\-‐–—·:：,，.。'‘’\"“”]+", "", text)


def _artist_ok(requested: str, names: tuple[str, ...]) -> bool:
    if not requested:
        return True
    want = _key(requested)
    for name in names:
        candidate = _key(name)
        if want == candidate or (len(want) >= 3 and want in candidate):
            return True
        if cross_similarity(requested, name) >= 0.75:
            return True
    return False


@lru_cache(maxsize=1)
def _local() -> tuple[dict, ...]:
    try:
        data = json.loads(resource_path("mmusic", "data", "album_aliases.json").read_text("utf-8"))
        return tuple(row for row in data if isinstance(row, dict))
    except (OSError, ValueError, TypeError):
        return ()


def local_album_alias(query: str, artist: str = "") -> AlbumAlias | None:
    """Resolve a curated exact alias without issuing a network request."""
    wanted = _key(query)
    for row in _local():
        if wanted not in {_key(a) for a in row.get("aliases") or []}:
            continue
        names = tuple(row.get("artists") or ())
        return (AlbumAlias(str(row["id"]), tuple(row["titles"]), names)
                if _artist_ok(artist, names) else None)
    return None


def _api_json(http, **params):
    url = _API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    return http.get_json(url, headers=_HEADERS, timeout=_TIMEOUT, retries=0)


def _claim_ids(entity: dict, prop: str) -> set[str]:
    ids = set()
    for claim in (entity.get("claims") or {}).get(prop) or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if isinstance(value, dict) and _ID.fullmatch(str(value.get("id") or "")):
            ids.add(value["id"])
    return ids


def _labels(entity: dict) -> tuple[str, ...]:
    out = []
    for language in ("ja", "ko", "en", "zh-hans", "zh", "zh-hant"):
        value = ((entity.get("labels") or {}).get(language) or {}).get("value")
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value.strip())
    return tuple(out)


def resolve_album_alias(http, query: str, artist: str = "") -> AlbumAlias | None:
    """Use exact local aliases, then verify an exact Chinese Wikidata label/alias.

    The remote path runs only after all enabled music sources fail to provide a
    matching album. It makes at most three short requests and never guesses from
    a partial title, a song entity, or an unverified performer.
    """
    local = local_album_alias(query, artist)
    if local is not None:
        return local
    wanted = _key(query)
    if any(wanted in {_key(a) for a in row.get("aliases") or []} for row in _local()):
        return None  # A curated title with a conflicting artist must not retry remotely.

    if not re.search(r"[\u3400-\u9fff]", query or "") or len(wanted) < 2:
        return None
    try:
        found = _api_json(http, action="wbsearchentities", search=query,
                          language="zh", uselang="zh", limit="8")
        candidates = list(dict.fromkeys(item["id"] for item in (found.get("search") or [])
                      if _ID.fullmatch(str(item.get("id") or ""))
                      and _key(str((item.get("match") or {}).get("text") or "")) == wanted))
        if not candidates:
            return None
        entities = (_api_json(http, action="wbgetentities", ids="|".join(candidates[:3]),
                              props="labels|claims", languages="zh|zh-hans|zh-hant|en|ja|ko")
                    .get("entities") or {})
        albums = [(qid, entities[qid]) for qid in candidates[:3]
                  if isinstance(entities.get(qid), dict)
                  and _claim_ids(entities[qid], "P31") & _ALBUM_TYPES]
        if not albums:
            return None
        performers = {qid: _claim_ids(entity, "P175") for qid, entity in albums}
        ids = sorted(set().union(*performers.values()))[:8]
        if not ids:
            return None
        artist_entities = (_api_json(http, action="wbgetentities", ids="|".join(ids),
                                     props="labels", languages="zh|zh-hans|zh-hant|en|ja|ko")
                           .get("entities") or {})
        verified = []
        for qid, entity in albums:
            names = tuple(n for pid in performers[qid]
                          for n in _labels(artist_entities.get(pid) or {}))
            if not names or (artist and not _artist_ok(artist, names)):
                continue
            titles = tuple(t for language in ("en", "ja", "ko")
                           if (t := ((entity.get("labels") or {}).get(language) or {}).get("value"))
                           and _key(t) != wanted)
            if titles:
                verified.append(AlbumAlias(qid, titles[:3], names))
        return verified[0] if len(verified) == 1 else None
    except Exception:  # noqa: BLE001 - optional metadata lookup must not block album search
        return None
