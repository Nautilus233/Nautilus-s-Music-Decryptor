"""Song-name normalisation and best-match ranking."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Sequence

from .model import Track

__all__ = ["ARTIST_UNKNOWN", "artist_match_score", "best_reading_match",
           "title_gate_ok",
           "split_filename_raw",
           "clean_text",
           "filename_parts", "filename_query", "has_extra_qualifier",
           "is_original_version", "normalize_for_version", "original_markers",
           "parse_song_filename", "parse_song_filename_candidates",
           "rank_tracks", "rank_tracks_multi", "reading_scores",
           "title_match_score",
           "similarity", "split_originals", "tokenize",
           "track_version_markers", "version_markers"]

# noise that streaming platforms sprinkle into filenames
_BRACKET_NOISE = re.compile(r"[\[(（【][^\[\]()（）【】]*[\]）】)]")
_NOISE_WORDS = re.compile(
    r"\b(mqms\d*|flac|mp3|320k|hires|hifi|sq|hq|live|remaster(ed)?|version|"
    r"explicit|clean|official|mv|lyric)\b", re.IGNORECASE)
_SEP = re.compile(r"\s*[-–—_|/]\s*")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _BRACKET_NOISE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    text = _NOISE_WORDS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return [t for t in _PUNCT.split(clean_text(text).lower()) if t]


# --------------------------------------------------------------------- #
# 版本过滤：原曲 vs 伴奏 / 改编版（用户要求的"歌名字段强匹配"）
# --------------------------------------------------------------------- #
# 中文/日文标记按「子串」匹配（它们不会出现在别的常用词内部）；
# 拉丁标记按「词元」匹配，避免 Alive 命中 live、Instant 命中 inst 这类误伤。
# 注意：这里检查的是**原始字符串**，括号内容不会被去掉 —— 与 clean_text 不同。
# 子串类：这些组合几乎不可能出现在"别的正常歌名内部"，所以按子串匹配。
# 注意带上「版」后缀 —— 直接匹配「电台」会误伤《电台情歌》这类正常歌名。
_BLOCK_SUBSTRINGS: "tuple[str, ...]" = (
    "伴奏", "伴唱", "纯音乐", "无人声", "无伴奏", "卡拉ok", "カラオケ",
    "翻唱", "现场", "试听", "不插电", "剪辑",
    "演唱会", "巡演", "串烧", "联唱", "演出版",      # 现场/串烧类，同样不是录音室原曲
    # 缩短版 / 电台版 / 电视版 —— 这类"看着像原曲但被剪短"的最难防
    "电台版", "电视版", "铃声版", "彩铃版", "片段版", "试唱",
    # 器乐 / 演奏 / 改编
    "演奏版", "钢琴版", "吉他版", "古筝版", "二胡版", "口琴版", "八音盒",
    "尤克里里", "小提琴", "萨克斯", "手风琴", "弦乐版", "交响版", "管弦",
    "民乐版", "纯伴奏", "伴奏曲", "清唱版", "和声版", "领唱版", "合唱版",
    # 人声 / 调性 / 速度类改编
    "童声版", "女声版", "男声版", "升调版", "降调版", "变速版",
    "加快版", "放慢版", "慢摇", "蹦迪", "电音版", "抖音版", "鬼畜", "恶搞",
    "rnb", "爵士版", "摇滚版", "抒情版", "民谣版", "古风版", "加速版", "减速版",
    "英文版", "中文版", "日语版", "粤语版", "韩语版", "电视剧", "电影版",
    # 教学 / 混剪
    "教学", "教程", "弹唱", "混剪", "混搭", "串歌",
    # 拉丁标记（多词，避免单字误伤）
    "off vocal", "offvocal", "minus one", "minusone",
    "radio edit", "radio version", "radio cut", "tv version", "tv size", "tv版", "hd版",
    "short version", "ringtone", "mashup", "acapella", "a cappella",
)
# 词元类：这些词本身也可能是正常歌名的一部分（《吉他手》《电台情歌》），
# 所以只在"独立词元"（被空格/括号/分隔符隔开）时才判定为版本标记。
_BLOCK_WORDS = frozenset({
    "instrumental", "inst", "karaoke", "remix", "cover",
    "live", "demo", "acoustic", "edit", "mix", "concert", "medley", "dj",
    "radio", "ringtone", "cut", "clip", "version",
    "钢琴", "吉他", "古筝", "二胡", "口琴", "八音盒", "尤克里里", "小提琴",
    "萨克斯", "手风琴", "交响", "弦乐", "民乐", "演奏", "纯音乐2",
    "童声", "女声", "男声", "合唱", "领唱", "和声", "清唱",
    "升调", "降调", "变速", "加快", "放慢", "慢摇", "蹦迪", "电音",
    "抖音", "鬼畜", "恶搞", "教学", "教程", "弹唱", "混剪", "混搭",
    "伴奏", "片段", "电台", "电视", "铃声", "彩铃",
})
# 明确的原曲标记 —— 同曲多版本时用于"录音室版优先"
_ORIGINAL_WORDS = frozenset({
    "studio", "original", "official", "原版", "原曲", "录音室", "正式版", "完整版",
})
_SEP_NORM = re.compile(r"[._\-+/\\|,:;!?\"'\u201c\u201d\u2018\u2019()\[\]{}<>\u00b7~@#$%^&*]+")
_BRACKET = re.compile(r"[\[(\uff08\u3010]")
_RAW_NAME_KEYS = frozenset({
    "name", "title", "songname", "song_name", "filename", "file",
    "filehashname", "tags", "tag", "text",
})


def normalize_for_version(text: str) -> str:
    """小写 + NFKC 归一化，并把所有分隔符/标点折叠成空格。"""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _SEP_NORM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def version_markers(text: str) -> "set[str]":
    """返回 `text` 中命中的伴奏/改编标记（作用于原始串，不剥离括号内容）。"""
    norm = normalize_for_version(text)
    if not norm:
        return set()
    tokens = set(norm.split())
    hits = {w for w in _BLOCK_WORDS if w in tokens}
    hits |= {p for p in _BLOCK_SUBSTRINGS if p in norm}
    return hits


def original_markers(text: str) -> "set[str]":
    """明确的原曲/录音室标记。"""
    norm = normalize_for_version(text)
    if not norm:
        return set()
    tokens = set(norm.split())
    return {w for w in _ORIGINAL_WORDS if w in tokens or w in norm}


def has_extra_qualifier(wanted: str, candidate: str) -> bool:
    """候选标题是否比"想要的标题"多带了修饰（括号内容或多余词元）。

    这是"同曲多版本"里最关键的一条偏好：`相似度` 会把括号内容整段剥掉，
    于是「夜曲 (电台版)」和「夜曲」的相似度都是 1.0 —— 只看相似度根本无法区分。
    因此这里显式判定"多出来的部分"，让干净标题永远排在前面。
    """
    if not candidate:
        return False
    if _BRACKET.search(candidate):
        return True
    want_tokens = set(normalize_for_version(wanted).split())
    cand_tokens = set(normalize_for_version(candidate).split())
    if not want_tokens or not cand_tokens:
        return False
    return bool(cand_tokens - want_tokens)


def track_version_markers(track: Track) -> "set[str]":
    """歌曲名/文件名/标签一类字段里命中的版本标记合集。"""
    fields = [track.title]
    for key, value in (track.raw or {}).items():
        if isinstance(value, str) and str(key).casefold() in _RAW_NAME_KEYS:
            fields.append(value)
    hits: "set[str]" = set()
    for field in fields:
        hits |= version_markers(field)
    return hits


def is_original_version(track: Track) -> bool:
    """该候选是否"干净"（不含任何伴奏/改编标记）。"""
    return not track_version_markers(track)


def split_originals(tracks: "Sequence[Track]") -> "tuple[list[Track], list[tuple[Track, set[str]]]]":
    """拆成 (原曲, [(被排除的改编版, 命中的标记), ...])。"""
    originals: list[Track] = []
    dropped: list[tuple[Track, set[str]]] = []
    for t in tracks:
        markers = track_version_markers(t)
        if markers:
            dropped.append((t, markers))
        else:
            originals.append(t)
    return originals, dropped


def filename_parts(stem: str) -> list[str]:
    """Split a filename stem into its non-empty parts (order preserved)."""
    stem = clean_text(stem)
    if not stem:
        return []
    return [p.strip() for p in _SEP.split(stem) if p.strip()]


def split_filename_raw(stem: str) -> "tuple[str, str]":
    """把文件名拆成 ``(歌名, 歌手)``，**保留括号等修饰**。

    与 :func:`parse_song_filename` 的关键区别：不做 ``clean_text`` 的去噪，
    所以「晴天 - 周杰伦 (KTV版伴奏)」会得到歌名「周杰伦 (KTV版伴奏)」——
    版本过滤（伴奏/电台版…）依赖这些字样，剥掉它们等于把过滤能力关掉。
    取「第一段=歌手、最后一段=歌名」，这正是本地曲库最常见的命名习惯；
    只有歌名时返回 ``(stem, "")``，即不提供歌手信息。
    """
    parts = [p.strip() for p in _SEP.split(stem or "") if p.strip()]
    if len(parts) >= 2:
        return parts[-1], parts[0]
    return (stem or "").strip(), ""


def parse_song_filename_candidates(stem: str) -> list[tuple[str, str]]:
    """Every plausible ``(title, artist)`` reading of a filename stem.

    Two orders are equally common in the wild: ``Artist - Title`` (what music
    apps tend to write) and ``Title - Artist`` (how people often name files by
    hand).  A bare filename cannot tell them apart, so we return *both*
    readings and let the ranking pick whichever fits the search results
    better.  That is a deterministic choice between two stated readings - it
    never guesses at the content itself.
    """
    parts = filename_parts(stem)
    if not parts:
        return []
    if len(parts) == 1:
        return [(parts[0], "")]
    first, last = parts[0], parts[-1]
    if first == last:
        return [(first, "")]
    return [(last, first),      # "Artist - Title"
            (first, last)]      # "Title - Artist"


def parse_song_filename(stem: str) -> tuple[str, str]:
    """The single most likely ``(title, artist)`` reading (best guess first)."""
    candidates = parse_song_filename_candidates(stem)
    return candidates[0] if candidates else ("", "")


def filename_query(stem: str) -> str:
    """Keyword query for a filename: its parts joined with spaces.

    Order is preserved so the phrasing stays natural under either convention
    ("周杰伦 稻香" / "晴天 周杰伦").  Search back-ends treat the terms as a
    keyword bag, so a single query covers every reading - no extra requests.
    """
    parts = filename_parts(stem)
    return " ".join(parts) if parts else clean_text(stem)



def similarity(a: str, b: str) -> float:
    a, b = clean_text(a).lower(), clean_text(b).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # token-overlap bonus so "song (live)" still matches "song"
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
        ratio = max(ratio, 0.5 * ratio + 0.5 * overlap)
    return ratio


def _score_one(title: str, artist: str, track: Track, duration: int, prefer_ext: str) -> float:
    score = 0.72 * similarity(title, track.title) + 0.28 * similarity(artist, track.artist)
    if duration and track.duration:
        delta = abs(duration - track.duration)
        score += 0.12 * max(0.0, 1.0 - delta / 15.0)          # <=15s counts as a match
    if prefer_ext and track.ext:
        if track.ext.lower() == prefer_ext.lower():
            score += 0.06
        elif prefer_ext == "wav" and track.ext.lower() == "flac":
            score += 0.04
    # 同曲多版本时的偏好：明确的原曲/录音室标记加分；
    # 带多余修饰（括号/多词）的候选**扣分**，确保干净标题永远赢。
    if original_markers(track.title):
        score += 0.03
    if has_extra_qualifier(title, track.title):
        score -= 0.06
    return score


def title_match_score(readings: "Sequence[tuple[str, str]]", track: Track) -> float:
    """How well a candidate's song title matches, under the best filename reading.

    Used as a **hard field gate**: a candidate whose title does not match is
    rejected however well the artist matches - otherwise "same artist,
    different song" results (which score fine on the artist alone) slip into
    the download pool.

    Scoring: 1.0 for an identical title, 0.85 when the wanted title appears
    verbatim inside a longer one (e.g. "晴天" inside "晴天 - 周杰伦", or a
    medley that really does contain it), otherwise the difflib similarity.
    """
    norm_title = normalize_for_version(track.title)
    best = 0.0
    for want, _artist in readings:
        wanted = normalize_for_version(want)
        if not wanted:
            continue
        if wanted == norm_title:
            best = max(best, 1.0)
        elif wanted in norm_title:
            best = max(best, 0.85)
        else:
            best = max(best, similarity(want, track.title))
    return best


# 平台常见"没有歌手信息"的写法 —— 这种情况不能拿来淘汰候选
ARTIST_UNKNOWN = frozenset({
    "", "未知", "未知艺术家", "未知歌手", "群星", "va", "various artists",
    "unknown", "unknown artist", "n/a", "na", "无", "不详", "佚名",
})
_ARTIST_SEP = re.compile(r"[、,;，；/\\&＋+|]|\sfeat\.?\s|\sft\.?\s|\swith\s")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _script_class(text: str) -> str:
    cjk, latin = bool(_CJK_RE.search(text)), bool(_LATIN_RE.search(text))
    if cjk and latin:
        return "mixed"
    return "cjk" if cjk else ("latin" if latin else "")


def _artist_tokens(text: str) -> set:
    norm = normalize_for_version(text)
    return {p.strip() for p in _ARTIST_SEP.split(norm) if p and p.strip()}


def artist_match_score(readings: "Sequence[tuple[str, str]]", track: Track) -> float:
    """歌手字段匹配分（与歌名门槛同级使用）。

    返回 1.0 表示"通过或不作判断"，越低越可疑：

    * 候选没有歌手信息（空 / 未知 / 群星）-> 1.0（无法判定，不作为淘汰理由）
    * 文件名/标签没给出歌手 -> 1.0
    * 一边只有中文、一边只有拉丁（如「周杰伦」vs「Jay Chou」）-> **不匹配**
      （强约束：确认不了就不下载；想让英文写法通过，请在文件名里补上该写法）
    * 多歌手串里有同一个人（「周杰伦/费玉清」对「周杰伦」）-> 0.85
    * 其余按相似度打分；**不同演唱者的同名版本（翻唱）会得到接近 0 的分数**
    """
    cand = normalize_for_version(track.artist)
    if cand in ARTIST_UNKNOWN:
        return 1.0
    want_artists = [a for _t, a in readings if a and a.strip()]
    if not want_artists:
        return 1.0

    cand_norm = cand
    cand_tokens = _artist_tokens(track.artist)
    cand_script = _script_class(cand_norm)
    best = 0.0
    for artist in want_artists:
        want = normalize_for_version(artist)
        if not want or want in ARTIST_UNKNOWN:
            continue
        if want == cand_norm:
            return 1.0
        # 强约束：文字系统不同也**必须**给出一致的名字才算匹配。
        # 这里曾经是「无法比对就不淘汰」，结果一个演唱者叫 Haohao Music 的
        # 同名翻唱（歌名还带修饰）就被放行了。现在：确认不了，就不下载。
        if _artist_tokens(artist) & cand_tokens:
            best = max(best, 0.85)         # 多歌手串中的同一个人
        elif want in cand_norm or cand_norm in want:
            best = max(best, 0.85)
        else:
            best = max(best, similarity(artist, track.artist))
    return best


def title_gate_ok(want_title: str, want_artist: str, track: Track) -> bool:
    """歌名是否**就是这首歌**（强约束）。

    * 完全一致 -> 通过；
    * **包含**：只允许多出来的词元都能用歌手名解释（平台常把标题写成
      「晴天 - 周杰伦」）；多出来的是别的修饰（例如「黑色毛衣(R&B)」里的
      r b）就判为**另一个版本**，直接淘汰；
    * 其余（只是模糊相似）-> 不通过。

    以前包含关系一律按 0.85 通过，于是一个歌名带 (R&B) 的改编版靠「包含」就过关。
    """
    norm_want = normalize_for_version(want_title)
    norm_cand = normalize_for_version(track.title)
    if not norm_want or not norm_cand:
        return False
    if norm_want == norm_cand:
        return True
    if norm_want not in norm_cand:
        return False
    leftover = [tok for tok in norm_cand.replace(norm_want, " ").split() if tok]
    if not leftover:
        return True
    artist_tokens = _artist_tokens(want_artist) if (want_artist or "").strip() else set()
    if not artist_tokens:
        return False
    return all(any(tok in a or a in tok for a in artist_tokens) for tok in leftover)


def best_reading_match(readings: "Sequence[tuple[str, str]]", track: Track, *,
                       min_title: float, min_artist: float, strict_title: bool = True
                       ) -> "tuple[bool, tuple[str, str], float, float]":
    """歌名与歌手**必须由同一个读法**同时达标。

    返回 ``(是否通过, 采用/最接近的读法, 歌名分, 歌手分)``。

    为什么不能用"两种读法各自取最高分"：文件名「周杰伦 - 黑色毛衣」有两种读法
    （歌名=黑色毛衣/歌手=周杰伦，或歌名=周杰伦/歌手=黑色毛衣）。若把两种读法的
    歌名最高分与歌手最高分拼在一起，一个演唱者字段里恰好含"黑色毛衣"字样的
    翻唱版就会同时"命中"两边的分数而混进来。要求同一读法同时成立，
    这种候选在两种读法下都会失败（一种错在歌手、另一种错在歌名）。
    """
    candidates = [(t, a) for t, a in (readings or []) if (t or "").strip() or (a or "").strip()]
    if not candidates:
        candidates = [("", "")]
    best_ok = None
    best_any = None
    best_ok_sum = best_any_sum = -1.0
    for want_t, want_a in candidates:
        ts = title_match_score([(want_t, want_a)], track)
        as_ = artist_match_score([(want_t, want_a)], track) if (want_a or "").strip() else 1.0
        pair = ((want_t, want_a), ts, as_)
        title_ok = (title_gate_ok(want_t, want_a, track) if strict_title
                    else ts >= min_title)      # 用户主动点名版本时放宽歌名规则
        if title_ok and as_ >= min_artist:
            if ts + as_ > best_ok_sum:
                best_ok, best_ok_sum = pair, ts + as_
        if ts + as_ > best_any_sum:
            best_any, best_any_sum = pair, ts + as_
    if best_ok is not None:
        reading, ts, as_ = best_ok
        return True, reading, ts, as_
    reading, ts, as_ = best_any
    return False, reading, ts, as_


def reading_scores(readings: Sequence[tuple[str, str]], track: Track,
                   *, duration: int = 0, prefer_ext: str = "") -> list[tuple[float, tuple[str, str]]]:
    """Score one track under every filename reading, best reading first."""
    scored = [(_score_one(t, a, track, duration, prefer_ext), (t, a)) for t, a in readings]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def rank_tracks_multi(readings: Sequence[tuple[str, str]], tracks: Sequence[Track],
                      *, duration: int = 0, prefer_ext: str = "") -> list[Track]:
    """Rank `tracks` by each track's *best* score across all filename readings."""
    readings = [r for r in readings if r[0] or r[1]] or [("", "")]
    for t in tracks:
        t.score = round(max(_score_one(rt, ra, t, duration, prefer_ext) for rt, ra in readings), 4)
    return sorted(tracks, key=lambda t: t.score, reverse=True)


def rank_tracks(query_title: str, query_artist: str, tracks: Sequence[Track],
                *, duration: int = 0, prefer_ext: str = "") -> list[Track]:
    """Rank against a single ``(title, artist)`` reading (kept for simple callers)."""
    return rank_tracks_multi([(query_title, query_artist)], tracks,
                             duration=duration, prefer_ext=prefer_ext)
