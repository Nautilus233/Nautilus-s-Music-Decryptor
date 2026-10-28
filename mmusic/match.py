"""Song-name normalisation and best-match ranking."""
from __future__ import annotations

import functools
import math
import re
import unicodedata
from difflib import SequenceMatcher
from itertools import islice
from typing import Sequence

from . import jp
from .model import Track

__all__ = ["ARTIST_UNKNOWN", "artist_match_score", "best_reading_match",
           "cross_similarity",
           "sanitize_query", "sanitize_query_text", "looks_like_song_query",
           "space_split_readings",
           "title_gate_ok",
           "split_filename_raw",
           "clean_text",
           "filename_parts", "filename_query", "has_extra_qualifier",
           "is_original_version", "normalize_for_version", "original_markers",
           "parse_song_filename", "parse_song_filename_candidates",
           "rank_tracks", "rank_tracks_multi", "reading_scores",
           "title_match_score",
           "similarity", "split_originals", "tokenize",
           "track_version_markers", "version_markers", "album_version_markers",
           "recording_version_ok", "title_alias_evidence", "filter_title_alias_ambiguity"]

# noise that streaming platforms sprinkle into filenames
_BRACKET_NOISE = re.compile(r"[\[(（【][^\[\]()（）【】]*[\]）】)]")
_NOISE_WORDS = re.compile(
    r"\b(mqms\d*|flac|mp3|320k|hires|hifi|sq|hq|live|remaster(ed)?|version|"
    r"explicit|clean|official|mv|lyric)\b", re.IGNORECASE)
_SEP = re.compile(r"\s*[-–—_|/]\s*")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")

# Only a complete soundtrack credit is descriptive metadata, not a version.
# Never strip arbitrary brackets here: (R&B), (Live), etc. remain hard gates.
_SOUNDTRACK_CREDIT = re.compile(
    r'(?:电影|电视剧|影视剧|网剧|动画片|动画|动漫|纪录片|游戏)?\s*'
    r'《[^《》\r\n]+》\s*(?:主题曲|插曲|片头曲|片尾曲|推广曲|宣传曲)')


def _title_without_soundtrack_credit(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    result = _BRACKET_NOISE.sub(
        lambda m: " " if _SOUNDTRACK_CREDIT.fullmatch(m.group()[1:-1].strip())
        else m.group(), text)
    return result.strip() if result.strip() else text


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
    # 2026-10-19 三十首批量实测里"漏网"的准正版（它们都真的能拿到直链）：
    # 「天空の城…(网友改编)」「…3D环绕版」「稻香 (2018 CCTV-15…)」「遇见 (黑胶版)」
    # 「…重生版」「…热爱版」「…(2010音乐万万岁)现场」「晚会现场」
    "改编", "环绕", "黑胶", "央视", "晚会", "节目", "重生版", "热爱版", "重制",
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
    "cctv",                      # 「稻香 (2018 CCTV-15 …)」这类电视节目版
    "伴奏", "片段", "电台", "电视", "铃声", "彩铃",
    # Japanese qualifiers are whole tokens, never arbitrary substrings of a
    # legitimate song title (e.g. ライブのあとで remains a normal title).
    "ライブ", "ライヴ", "カバー", "インスト", "インストゥルメンタル",
    "アコースティック", "リミックス", "オフボーカル", "オフヴォーカル", "ピアノ",
    "ライブ版", "ライヴ版", "カバー版", "ライブバージョン", "ライヴバージョン",
})
# 明确的原曲标记 —— 同曲多版本时用于"录音室版优先"
_ORIGINAL_WORDS = frozenset({
    "studio", "original", "official", "原版", "原曲", "录音室", "正式版", "完整版",
})
_SEP_NORM = re.compile(r"[._\-+/\\|,:;!?\"'\u201c\u201d\u2018\u2019()\[\]{}<>\u00b7~@#$%^&*]+")
_BRACKET = re.compile(r"[\[(\uff08\u3010]")
_RAW_NAME_KEYS = frozenset({
    "name", "title", "songname", "song_name", "filename", "file",
    "filehashname", "tags", "tag", "text", "suffix", "songnamesuffix", "subtitle",
})
_RAW_ALBUM_KEYS = frozenset({"album", "albums", "albumname", "albumtitle", "albuminfo",
                            "al", "专辑", "唱片集"})
_LIVE_CONTEXT = re.compile(
    r"(?<![a-z])live(?:\s*(?:版本|版|录音|錄音|专辑|專輯)"
    r"|\s+(?:at|in|from|on|album|recording|version|tour|concert|ver\b)\b"
    r"|\s+\d{4}(?:\b|年)|\s+vol\.?\s*\d|\s*[「『【(\[]|\s*$)"
    r"|(?:^|[\[(【])\s*live\s*[)\]】]"
    r"|演唱会|演唱會|现场(?:录音|錄音|版|专辑|專輯)|現場(?:錄音|版|專輯)"
    r"|ライブ(?:盤|版|アルバム|音源|録音)|ライヴ(?:盤|版|アルバム|音源|録音)"
    r"|(?:^|[\s「『【(\[])ライ[ブヴ](?=$|[\s「『」』】)\]\d])"
    r"|(?:^|[\s「『【(\[])(?:现场|現場)(?=$|[\s「『」』】)\]])", re.IGNORECASE)


def album_version_markers(text: str) -> set[str]:
    """Album names need recording context, not ordinary title words like Alive.

    In particular Live Through This and Live and Let Die are album titles,
    whereas LIVE「月光」, Live at Wembley and (Live) identify a recording.
    """
    norm = unicodedata.normalize("NFKC", text or "").casefold()
    hits = {"live"} if _LIVE_CONTEXT.search(norm) else set()
    for word in ("伴奏", "カラオケ", "instrumental", "karaoke", "remix album",
                 "翻唱专辑", "翻唱專輯", "カバーアルバム"):
        if (word in norm if not word.isascii() else
                re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", norm)):
            hits.add(word)
    return hits


def _recording_fields(raw):
    """Walk only named metadata containers; never scan URLs, artists or lyrics."""
    remaining = 128
    stack = [(raw, 0, False)]
    while stack and remaining:
        obj, depth, in_album = stack.pop()
        remaining -= 1
        if depth > 4:
            continue
        if isinstance(obj, dict):
            for key, value in list(obj.items())[:64]:
                key = re.sub(r"[_\s-]", "", str(key).casefold())
                album = key in _RAW_ALBUM_KEYS or (in_album and key in {"name", "title"})
                if isinstance(value, str):
                    if album:
                        yield True, value
                    elif not in_album and key in _RAW_NAME_KEYS:
                        yield False, value
                elif isinstance(value, (dict, list, tuple)) and (album or key in {
                        "metadata", "meta", "tags", "tag", "song", "track", "data",
                        "songinfo", "trackinfo", "recordingtags", "transparam"}):
                    stack.append((value, depth + 1, album))
        elif isinstance(obj, (list, tuple)):
            for value in obj[:32]:
                if isinstance(value, str) and in_album:
                    yield True, value
                elif isinstance(value, dict):
                    stack.append((value, depth + 1, in_album))


def _recording_identity(markers):
    live = {"live", "concert", "现场", "現場", "演唱会", "演唱會", "巡演",
            "ライブ", "ライヴ", "ライブ版", "ライヴ版", "ライブバージョン", "ライヴバージョン"}
    return {"live" if m in live else m for m in markers}


def recording_version_ok(track: Track, requested=()) -> bool:
    """Do not mix requested recordings, even if their durations are close."""
    actual = _recording_identity(track_version_markers(track))
    wanted = _recording_identity(requested)
    return actual == wanted


def normalize_for_version(text: str) -> str:
    """小写 + NFKC 归一化，并把所有分隔符/标点折叠成空格。"""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _SEP_NORM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def version_markers(text: str) -> "set[str]":
    """返回伴奏/改编标记；只忽略完整影视出处说明，其余括号保留。"""
    norm = normalize_for_version(_title_without_soundtrack_credit(text))
    if not norm:
        return set()
    tokens = set(norm.split())
    hits = {w for w in _BLOCK_WORDS if w in tokens}
    hits |= {p for p in _BLOCK_SUBSTRINGS if p in norm and
             (not p[0].isascii() or re.search(r"(?<![a-z0-9])" + re.escape(p), norm))}
    return hits


@functools.lru_cache(maxsize=4096)
def original_markers(text: str) -> "frozenset[str]":
    """明确的原曲/录音室标记（结果只读，记忆化以省下重复归一化开销）。"""
    norm = normalize_for_version(text)
    if not norm:
        return frozenset()
    tokens = set(norm.split())
    return frozenset(w for w in _ORIGINAL_WORDS if w in tokens or w in norm)


def has_extra_qualifier(wanted: str, candidate: str) -> bool:
    """候选标题是否比"想要的标题"多带了修饰（括号内容或多余词元）。

    这是"同曲多版本"里最关键的一条偏好：`相似度` 会把括号内容整段剥掉，
    于是「夜曲 (电台版)」和「夜曲」的相似度都是 1.0 —— 只看相似度根本无法区分。
    因此这里显式判定"多出来的部分"，让干净标题永远排在前面。
    """
    wanted = _title_without_soundtrack_credit(wanted)
    candidate = _title_without_soundtrack_credit(candidate)
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
    """Recording identity across title, album and bounded source metadata."""
    hits = version_markers(track.title) | album_version_markers(track.album)
    for album, value in _recording_fields(track.raw or {}):
        hits |= album_version_markers(value) if album else version_markers(value)
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


# --------------------------------------------------------------------- #
# 手动搜索 / 点播清单：清洗序号与符号
# --------------------------------------------------------------------- #
_NUM_TOKEN = r"(?:\d{1,3}|[一二三四五六七八九十百零〇两]{1,3})"
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\u00ad]")
# 圈号/带圈数字（①-⑳、❶-❿、㉑ 等）—— 当列表序号处理，且必须在 NFKC **之前**下手
_ENCLOSED_LEAD = re.compile(r"^[\s\u3000]*[\u2460-\u24ff\u2776-\u2793\u3220-\u3239"
                            r"\u3248-\u324f\u3251-\u325f]+[\s\u3000]*")
_CJK_ONLY = re.compile(r"^[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+$")


def merge_lone_cjk(text: str) -> str:
    """把 `夜 曲` / `周 杰伦` 这类"被空格切散的汉字"合回去。

    只合并**左侧是单个汉字**的相邻片（以及两侧都只有一个汉字的情形）——
    这样 `夜曲 周杰伦`（歌名 + 歌手两个词）的边界不会被误并成 `夜曲周杰伦`，
    拉丁串里的空格（`Hotel California`）也照样保留。
    """
    if " " not in text:
        return text
    merged: "list[str]" = []
    for token in text.split(" "):
        if (merged and token and len(merged[-1]) == 1
                and _CJK_ONLY.match(merged[-1]) and _CJK_ONLY.match(token)):
            merged[-1] += token
        else:
            merged.append(token)
    return " ".join(merged)
# 整串被书名号/引号包起来：「《夜曲》」「"夜曲"」
_WRAPPED = re.compile(r"^[\s\u3000]*[《「『“\"']\s*(.+?)\s*[》」』”\"']\s*$")
# 前缀：括号序号 [1] （一） 【3】；裸序号 1. 2、 三. ；第 3 首；零填充 01␣；装饰符号
_LEAD_BRACKET_NUM = re.compile(r"^[\s\u3000]*[（(\[{【]\s*" + _NUM_TOKEN
                               + r"\s*[)）\]}】]\s*[.．、,，:：\-–—]?\s*")
_LEAD_NUM = re.compile(r"^[\s\u3000]*" + _NUM_TOKEN + r"\s*[.．、,，)），:：；;\-–—。]\s*")
_LEAD_ROMAN = re.compile(r"^[\s\u3000]*(?:[IVXLCDM]{1,5}|[iIlL])\s*[.．、,，)）:：\-–—]+\s*")
_LEAD_DIGIT_SPACE = re.compile(r"^[\s\u3000]*\d[\s\u3000]+(?=[\u3400-\u9fff\uf900-\ufaff"
                               r"\u3040-\u30ff\uac00-\ud7af])")
_LEAD_TRACK = re.compile(r"^[\s\u3000]*第\s*" + _NUM_TOKEN
                         + r"\s*[首曲个集章]\s*[.．、:：\-–—]?\s*")
_LEAD_ZERO_PADDED = re.compile(r"^[\s\u3000]*0\d{1,2}[\s\u3000]+")
_LEAD_BULLET = re.compile(r"^[\s\u3000]*[•·※☆★◇◆○●■□▲▼►▶▸▹→⇒➜・、,，\-–—~～＋+*#<>）》】\]]+\s*")
# 行首「说明前缀」（点播：/歌名：/推荐 - …）。**白名单**制，只认明确是"标签"的词。
# 「带标点」就能认的标签（含单字，如 `求：`；也含 `歌手：`/`原唱：` 这种"标签后跟内容"的）
_LABEL_LIST = ("点播", "点歌", "求歌", "求播", "点一首", "来一首", "歌名", "曲名", "歌曲",
               "歌曲名", "曲目", "曲目名", "歌单", "专辑", "专辑名", "album", "收听", "播放",
               "播送", "送上", "推荐",
               "分享", "听歌", "试听", "试唱", "原唱", "原唱歌手", "演唱", "演唱者", "歌手",
               "求", "听", "播", "送", "选")
_LABEL_SET = frozenset(_LABEL_LIST)
_LABEL_WORDS = "(?:" + "|".join(_LABEL_LIST) + ")"
# 「只带空格」也认的标签：只放**多字且不会跟歌名混**的词。
# 刻意不含 原唱/演唱/歌手 —— `原唱 周杰伦` 里那串是歌手，不能当标签删掉。
_LABEL_SPACE_WORDS = ("(?:" + "|".join(("点播", "点歌", "求歌", "求播", "点一首", "来一首",
                                       "歌名", "曲名", "歌曲", "歌曲名", "曲目", "曲目名",
                                       "歌单", "专辑", "专辑名", "收听", "播放", "播送", "送上", "推荐",
                                       "分享", "听歌", "试听",
                                       # 单字标签只在"后面还有内容"时生效（`求 夜曲`）；
                                       # `求` 单独出现时由"绝不擦成空串"兜底成原文
                                       "求", "听", "播", "送", "选")) + ")")
# ① 带标点：连单字标签也认（`求：夜曲`）—— 标点本身就是"这是标签"的信号
_LEAD_LABEL_PUNCT = re.compile(r"^[\s\u3000]*" + _LABEL_WORDS
                               + r"\s*[：:、,，;；\-–—.。!！]+\s*")
# ② 只带空格：只认多字标签，且后面必须真的有内容（`点播 夜曲`）
_LEAD_LABEL_SPACE = re.compile(r"^[\s\u3000]*" + _LABEL_SPACE_WORDS + r"\s+")
# ③ 方括号里恰好是标签词：`【点播】夜曲`
_LEAD_BRACKET_LABEL = re.compile(r"^[\s\u3000]*[（(\[{【]\s*(?:点播|点歌|求歌|歌名|曲名|歌曲"
                                 r"|歌单|专辑|收听|播放|推荐|分享)\s*[)）\]}】]\s*[：:、,，\-–—]?\s*")
# 后缀：括号序号 夜曲（2）；分隔符+序号 夜曲-2；装饰符号；悬空分隔符
_TRAIL_BRACKET_NUM = re.compile(r"[\s\u3000]*[（(\[{【]\s*" + _NUM_TOKEN
                                + r"\s*[)）\]}】]\s*$")
_TRAIL_DASH_NUM = re.compile(r"[\s\u3000]*[\-–—·]\s*" + _NUM_TOKEN
                             + r"\s*[.．、]?\s*$")
_TRAIL_BULLET = re.compile(r"[\s\u3000]*[•·※☆★◇◆○●■□▲▼►▶▸▹*#]+\s*$")
# 直接粘贴文件名时带的扩展名（夜曲.mp3）也算噪声 —— 它不是歌名的一部分
_TRAIL_EXT = re.compile(r"[\s\u3000]*\.(?:mp3|flac|m4a|aac|wav|ogg|opus|ape|wma|dff|mgg|mflac"
                        r"|mmp4|ncm|qmc\d*|kwm|kwd|mp4)\s*$", re.IGNORECASE)
_TRAIL_SEP = re.compile(r"[\s\u3000]*[\-–—·、,，:：;；.。]+\s*$")

_LEAD_HASH_NUM = re.compile(r"^[\s\u3000]*[#＃]\s*" + _NUM_TOKEN + r"\s*[.．、,，)）:：\-–—]?\s*")
_LEAD_NO_NUM = re.compile(r"^[\s\u3000]*[Nn][oO]\.?\s*" + _NUM_TOKEN + r"\s*[.．、,，)）:：\-–—]?\s*")
# 英文标签必须带标点（否则《Title》这种真的歌名会被啃）
_LEAD_EN_LABEL = re.compile(r"^[\s\u3000]*(?:song|title|track|name|music)\s*[:：=\-–—]+\s*",
                            re.IGNORECASE)
# 点歌清单里的「原唱：周杰伦」「歌手: 周杰伦」—— 把歌手抠出来当提示，而不是丢掉
# 只认两种**无歧义**的歌手提示，其余（如「歌手：周杰伦 夜曲」）刻意不猜：
#   ① 括号里整段就是歌手：`夜曲（原唱：周杰伦）`
#   ② 行尾整段就是歌手：  `夜曲 原唱：周杰伦`
_ARTIST_HINT = re.compile(
    r"[（(\[{【]\s*(?:原唱|原唱歌手|演唱|演唱者|歌手|artist|singer)\s*[:：=]\s*"
    r"([^)\]}）】,，;；/|（(\[{【]+)\s*[)\]}）】]"
    r"|(?:^|[\s\u3000])(?:原唱|原唱歌手|演唱|演唱者|歌手|artist|singer)\s*[:：=]\s*"
    r"([^)\]}）】,，;；/|（(\[{【\s]+)\s*$")
# 书名号/引号出现在中间时当分隔符（整串被包住的情况由 _WRAPPED 处理）
_BOOK_SPLIT = re.compile(r"[《「『]\s*(.+?)\s*[》」』]")
# 间隔号/中点也常当"歌手·歌名"的分隔符
_MIDDOT_SPLIT = re.compile(r"\s*[·・•‧]\s*")

_LEAD_ALL = (_LEAD_BRACKET_NUM, _LEAD_BRACKET_LABEL, _LEAD_LABEL_PUNCT,
             _LEAD_LABEL_SPACE, _LEAD_HASH_NUM, _LEAD_NO_NUM, _LEAD_EN_LABEL,
             _LEAD_ROMAN, _LEAD_NUM, _LEAD_TRACK, _LEAD_ZERO_PADDED,
             _LEAD_DIGIT_SPACE, _LEAD_BULLET)
_TRAIL_ALL = (_TRAIL_BRACKET_NUM, _TRAIL_DASH_NUM, _TRAIL_BULLET, _TRAIL_SEP, _TRAIL_EXT)


def _clean_chunk(chunk: str) -> str:
    """对一段文本反复清洗序号/符号/标签（「1.（一）夜曲」这种叠加形态要迭代）。"""
    for _ in range(4):
        before = chunk
        chunk = _WRAPPED.sub(r"\1", chunk.strip()) or chunk
        for pattern in _LEAD_ALL:
            chunk = pattern.sub("", chunk)
        for pattern in _TRAIL_ALL:
            chunk = pattern.sub("", chunk)
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if chunk == before.strip():
            break
    return chunk


def sanitize_query(text: str) -> "tuple[str, str]":
    """清洗用户输入 → ``(歌名文本, 提示歌手)``。

    做四类事（全部只针对"输入噪声"，不动歌名本身）：

    1. **归一化**：NFKC（全角→半角）、去零宽字符、全角空格→空格；
    2. **抠出歌手提示**：``夜曲 原唱：周杰伦`` / ``歌手: 周杰伦`` → 歌手=周杰伦、
       文本变 ``夜曲``（这类写法在点歌清单里很常见，直接丢掉等于白丢一个歌手参照）；
    3. **分隔符归一**：``《夜曲》`` 在句子中间当分隔符（``周杰伦《夜曲》`` → ``周杰伦 - 夜曲``）、
       ``周杰伦·夜曲`` 的间隔号同理；
    4. **清掉序号/符号/说明前缀**（`_LEAD_ALL` / `_TRAIL_ALL`），并**逐段**处理
       （``周杰伦 - 1. 夜曲``、``夜曲 - 点播``），整段就是说明标签的直接丢掉。

    精度护栏（宁可不洗，也不洗错）：``（Live）``/``(伴奏)`` 这类版本标记原样保留；
    ``7 Years``/``30 岁的女人``/``2002年的第一场雪`` 不动；``音乐会`` 不被当成标签。

    返回的文本可能为空（例如整行只有 ``1.``）—— 由 :func:`looks_like_song_query` 判断
    是否值得拿去搜索；需要"绝不返回空串"的场景请用 :func:`sanitize_query_text`。
    """
    original = (text or "")
    out = _INVISIBLE.sub("", original)
    out = _ENCLOSED_LEAD.sub("", out)          # ① 要在 NFKC 之前认
    out = unicodedata.normalize("NFKC", out)
    out = out.replace("\u3000", " ")

    out = _TRAIL_EXT.sub("", out)      # 先剥掉粘贴文件名带的扩展名，再解析歌手提示

    hint = ""
    found = _ARTIST_HINT.search(out)
    if found:
        hint = _clean_chunk(found.group(1) or found.group(2) or "")
        out = f"{out[:found.start()]} {out[found.end():]}"

    out = _BOOK_SPLIT.sub(r" - \1", out)      # 周杰伦《夜曲》 → 周杰伦 - 夜曲
    out = _MIDDOT_SPLIT.sub(" - ", out)        # 周杰伦·夜曲   → 周杰伦 - 夜曲
    out = _clean_chunk(out)

    # 空格分隔的标签词也丢掉（`夜曲 点播` / `求 夜曲`）—— 前提是还剩别的内容
    tokens = [t for t in out.split(" ") if t]
    kept_tokens = [t for t in tokens if t not in _LABEL_SET]
    if kept_tokens and len(kept_tokens) < len(tokens):
        out = " ".join(kept_tokens)
    out = merge_lone_cjk(out)                   # `夜 曲` → `夜曲`（不动 `夜曲 周杰伦` 的边界）

    # 逐段再清一遍：序号可能落在分隔符之后（周杰伦 - 1. 夜曲），
    # 整段就是说明标签的（夜曲 - 点播）直接丢掉。
    parts = [p for p in re.split(r"\s*[-–—]\s*", out) if p.strip()]
    if len(parts) > 1:
        kept: "list[str]" = []
        for part in parts:
            piece = _clean_chunk(part)
            if piece and piece not in _LABEL_SET:
                kept.append(piece)
        out = " - ".join(kept) if kept else ""

    # 收尾再清一次：丢掉整段标签后可能留下悬空分隔符（`夜曲 - 点播` → `夜曲 -`）
    out = _clean_chunk(out)
    return out.strip(), hint


def sanitize_query_text(text: str) -> str:
    """同 :func:`sanitize_query`，但**保证不返回空串**（清洗结果为空时回退原文）。"""
    cleaned, _hint = sanitize_query(text)
    return cleaned.strip() or (text or "").strip()


def looks_like_song_query(text: str) -> "tuple[bool, str]":
    """这一行值不值得拿去搜？返回 ``(可用, 清洗后的歌名)``。

    用于批量点播清单：``1.``、``求：``、``点播`` 这种清洗完什么都没有、或**只剩说明标签**的行，
    直接跳过（免得白跑一整轮音源检索，还污染日志）—— 这是"筛选"那一半。
    """
    cleaned, _hint = sanitize_query(text)
    return (bool(cleaned) and cleaned not in _LABEL_SET), cleaned


def space_split_readings(text: str) -> "list[tuple[str, str]]":
    """把"空格分隔的两段"当成可能的「歌名 歌手」（两种顺序都试）。

    手动搜索框里写 ``夜曲 周杰伦`` 是很自然的写法，但空格不是文件名里的分隔符约定
    （那里用的是 ``-``），所以这里**只补读法、不替换**原有的读法 —— 排名取各读法的
    最大值，多一种读法只会更容易命中，不会让原本能过的匹配失败。
    """
    text = (text or "").strip()
    if re.search(r"[-–—]", text):
        return []       # 已经有明确分隔符 → 读法交给文件名解析，别在这里重复拆
    parts = [p for p in re.split(r"\s+", text) if p.strip(" -–—·.,;:()[]{}（）【】")]
    if len(parts) < 2:
        return []
    out: "list[tuple[str, str]]" = []
    for title, artist in ((parts[0], " ".join(parts[1:])),
                          (" ".join(parts[:-1]), parts[-1])):
        if title and artist and title != artist and (title, artist) not in out:
            out.append((title, artist))
    return out


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


def _ascii_ish(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and all(ord(ch) < 128 for ch in t) and any(ch.isalpha() for ch in t)


@functools.lru_cache(maxsize=8192)
def cross_similarity(a: str, b: str) -> float:
    """原文相似度的加强版：任一侧呈日语形态时，追加**确定性读音形态**再取最大。

    只做确定性转换（假名/罗马音/内置表读音）：查不到读音的一侧不参与，
    因此不会凭空放大相似度。中文对中文等场景与 :func:`similarity` 完全一致。
    """
    base = max(similarity(a, b), similarity(jp.fold_written(a), jp.fold_written(b)))
    if not (jp.has_kana(a) or jp.has_kana(b) or _ascii_ish(a) or _ascii_ish(b)):
        return base
    try:
        fa, fb = jp.match_forms(a), jp.match_forms(b)
    except Exception:                                    # noqa: BLE001 - 比对绝不能炸
        return base
    if not fa or not fb:
        return base
    best = base
    for x in fa:
        for y in fb:
            value = similarity(x, y)
            if value > best:
                best = value
    return best


def _score_one(title: str, artist: str, track: Track, duration: int, prefer_ext: str) -> float:
    verified_alias = _verified_title_alias(title, artist, track)
    title_score = (1.0 if verified_alias
                   else cross_similarity(title, track.title))
    score = 0.72 * title_score + 0.28 * cross_similarity(artist, track.artist)
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
    if not verified_alias and has_extra_qualifier(title, track.title):
        score -= 0.06
    if verified_alias and cross_similarity(title, track.title) < _JP_STRICT_SIMILARITY:
        # Alias identity is retrieval evidence, not an independent proof of
        # recording length. In particular it must never create a >=.95 strong
        # duration sample / early stop just by changing the title score.
        score = min(score, .94)
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
    norm_title = normalize_for_version(_title_without_soundtrack_credit(track.title))
    best = 0.0
    for want, _artist in readings:
        if _verified_title_alias(want, _artist, track):
            best = max(best, 1.0)
            continue
        wanted = normalize_for_version(_title_without_soundtrack_credit(want))
        if not wanted:
            continue
        if wanted == norm_title:
            best = max(best, 1.0)
        elif wanted in norm_title:
            best = max(best, 0.85)
        else:
            best = max(best, cross_similarity(want, track.title))
    return best


# 平台常见"没有歌手信息"的写法 —— 这种情况不能拿来淘汰候选
ARTIST_UNKNOWN = frozenset({
    "", "未知", "未知艺术家", "未知歌手", "群星", "va", "various artists",
    "unknown", "unknown artist", "n/a", "na", "无", "不详", "佚名",
})
_ARTIST_SEP = re.compile(r"[、,;，；/\\&＋+|]|\sfeat\.?\s|\sft\.?\s|\swith\s")
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


_WORD_CHARS = "0-9a-z\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"


def _artist_contains(hay: str, needle: str) -> bool:
    """``needle`` 是否作为**独立的歌手名**出现在 ``hay`` 里。

    为什么需要边界：原来的 ``needle in hay`` 会把单字母歌手名当子串命中 ——
    实测查询「R - Roselia」时，候选「arkinz - roselia」里的 ``arkinz`` **含字母 r**，
    于是歌手门槛按 0.85 放行，最后下到了一条与该曲无关的 CC 上传物。

    规则：**中文名按子串**（「周杰伦」出现在「周杰伦精选」里是合意的）；
    **拉丁名必须词级出现**（前后是非字母数字，或串首串尾），
    这样 `r` ⊄ `arkinz`、`va` ⊄ `various`，而 `jj` ⊂ `jj lin`、`jay chou` ⊂ `周杰伦 jay chou` 照常命中。
    """
    if not hay or not needle or needle not in hay:
        return False
    if _CJK_RE.search(needle):
        return True
    pattern = (r"(?:^|[^" + _WORD_CHARS + r"])" + re.escape(needle)
               + r"(?:$|[^" + _WORD_CHARS + r"])")
    return re.search(pattern, hay) is not None


def _script_class(text: str) -> str:
    cjk, latin = bool(_CJK_RE.search(text)), bool(_LATIN_RE.search(text))
    if cjk and latin:
        return "mixed"
    return "cjk" if cjk else ("latin" if latin else "")


def _artist_text(text: str) -> str:
    """Latin accents are writing variants; Japanese voicing marks remain intact."""
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = "".join(
        "".join(c for c in unicodedata.normalize("NFD", ch)
                if unicodedata.category(c) != "Mn")
        if "LATIN" in unicodedata.name(ch, "") else ch
        for ch in normalized)
    return normalize_for_version(normalized)


def _artist_tokens(text: str) -> set:
    norm = _artist_text(text)
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
    cand = _artist_text(track.artist)
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
        want = _artist_text(artist)
        if not want or want in ARTIST_UNKNOWN:
            continue
        if want == cand_norm:
            return 1.0
        # 强约束：文字系统不同也**必须**给出一致的名字才算匹配。
        # 这里曾经是「无法比对就不淘汰」，结果一个演唱者叫 Haohao Music 的
        # 同名翻唱（歌名还带修饰）就被放行了。现在：确认不了，就不下载。
        if _artist_tokens(artist) & cand_tokens:
            best = max(best, 0.85)         # 多歌手串中的同一个人
        elif _artist_contains(cand_norm, want) or _artist_contains(want, cand_norm):
            best = max(best, 0.85)
        else:
            fa = jp.match_forms(artist, kind="artist")
            fb = jp.match_forms(track.artist, kind="artist")
            if fa or fb:
                # Reading identity has a much higher bar than general fuzzy
                # text: atarayo/tayori share letters, but are different artists.
                score = max((similarity(a, b) for a in fa for b in fb), default=0.0)
                best = max(best, score if score >= _JP_STRICT_SIMILARITY else min(score, 0.59))
            else:
                best = max(best, max(similarity(artist, track.artist),
                                     similarity(jp.fold_written(artist), jp.fold_written(track.artist))))
    return best


def title_gate_ok(want_title: str, want_artist: str, track: Track) -> bool:
    """歌名是否**就是这首歌**（强约束）。

    * 完全一致 -> 通过；
    * 完整的影视出处括号（如「电影《头文字D》主题曲」）不视为版本；
    * **包含**：只允许多出来的词元都能用歌手名解释（平台常把标题写成
      「晴天 - 周杰伦」）；多出来的是别的修饰（例如「黑色毛衣(R&B)」里的
      r b）就判为**另一个版本**，直接淘汰；
    * 其余（只是模糊相似）-> 不通过。

    以前包含关系一律按 0.85 通过，于是一个歌名带 (R&B) 的改编版靠「包含」就过关。
    """
    norm_want = normalize_for_version(jp.fold_written(_title_without_soundtrack_credit(want_title)))
    norm_cand = normalize_for_version(jp.fold_written(_title_without_soundtrack_credit(track.title)))
    if not norm_want or not norm_cand:
        return False
    if norm_want == norm_cand:
        return True
    if _verified_title_alias(want_title, want_artist, track):
        return True
    if jp.title_aliases(want_title):
        return False
    if norm_want not in norm_cand:
        return _jp_title_gate_ok(want_title, track.title, want_artist, track.artist)
    leftover = [tok for tok in norm_cand.replace(norm_want, " ").split() if tok]
    if not leftover:
        return True
    artist_tokens = _artist_tokens(want_artist) if (want_artist or "").strip() else set()
    if artist_tokens and all(any(tok in a or a in tok for a in artist_tokens) for tok in leftover):
        return True
    return _jp_title_gate_ok(want_title, track.title, want_artist, track.artist)


_ALIAS_KEYS = frozenset({"alias", "aliases", "alia", "tns", "transnames",
                         "transname", "translatedname", "translationname",
                         "subtitle", "subtitles"})
_TITLE_KEYS = frozenset({"name", "title", "songname", "orisongname", "originalname"})
_ALIAS_BRACKET = re.compile(r"^(.*?)\s*[（(]([^()（）]{1,256})[)）]\s*$")


def _metadata_key(value):
    return re.sub(r"[_\s-]", "", str(value).casefold())


def _alias_values(value):
    if isinstance(value, str):
        value = unicodedata.normalize("NFKC", value).strip()
        if value.startswith("(") and value.endswith(")"):
            value = value[1:-1].strip()
        if value and len(value) <= 256:
            yield value
    elif isinstance(value, (list, tuple)):
        for entry in value[:12]:
            if isinstance(entry, str):
                yield from _alias_values(entry)


def _title_parts(text):
    text = unicodedata.normalize("NFKC", text or "").strip()
    found = _ALIAS_BRACKET.fullmatch(text)
    return (found[1].strip(), found[2].strip()) if found else (text, "")


def _bound_title(left, right):
    if jp.title_alias_key(left) == jp.title_alias_key(right):
        return bool(jp.title_alias_key(left))
    # Binding may cross kana/romaji, but may not erase version qualifiers.
    return (not _BRACKET.search(left) and not _BRACKET.search(right)
            and not version_markers(left) and not version_markers(right)
            and bool(set(jp.match_forms(left)) & set(jp.match_forms(right))))


def title_alias_evidence(track: Track) -> tuple[str, ...]:
    """Bounded whitelist of aliases attached to THIS API song record.

    No recursive text mining: lyrics/tags, highlights, search-query echoes,
    album/artist aliases and unrelated result objects cannot prove song identity.
    Kugou suffixes/filename annotations additionally require a bound song name.
    """
    core, annotation = _title_parts(track.title)
    out = []
    stack = [(track.raw, 0, True)]
    visited = 0
    while stack and visited < 12 and len(out) < 24:
        obj, depth, root = stack.pop()
        visited += 1
        if not isinstance(obj, dict) or depth > 2:
            continue
        fields = {_metadata_key(k): v for k, v in islice(obj.items(), 64)}
        names = [v for k, v in fields.items() if k in _TITLE_KEYS and isinstance(v, str)]
        bound = any(_bound_title(name, track.title) or _bound_title(name, core) for name in names)
        if names and not bound:
            continue
        if root or bound:
            for key in _ALIAS_KEYS:
                for value in _alias_values(fields.get(key)):
                    if value not in out:
                        out.append(value)
        if bound:
            suffixes = [fields.get("suffix")]
            trans = fields.get("transparam")
            if isinstance(trans, dict):
                suffixes.extend(v for k, v in islice(trans.items(), 16)
                                if _metadata_key(k) == "songnamesuffix")
            for item in suffixes:
                for value in _alias_values(item):
                    if value not in out:
                        out.append(value)
            filename = fields.get("filename")
            if isinstance(filename, str) and len(filename) <= 512:
                filename = re.sub(r"\.(?:mp3|flac|ogg|wav|m4a)$", "", filename, flags=re.I)
                file_title, sep, possible_title = filename.rpartition(" - ")
                if sep:
                    if (normalize_for_version(track.artist) in ARTIST_UNKNOWN
                            or artist_match_score([("", file_title)], track) < .999):
                        possible_title = ""
                else:
                    possible_title = filename
                file_core, file_alias = _title_parts(possible_title)
                if file_alias and _bound_title(file_core, core) and file_alias not in out:
                    out.append(file_alias)
        for key in ("song", "songinfo", "track", "trackinfo", "metadata"):
            child = fields.get(key)
            if isinstance(child, dict):
                stack.append((child, depth + 1, False))
    return tuple(out[:24])


def _local_title_member(track, original, performer):
    if artist_match_score([("", performer)], track) < .999:
        return False
    names = [original] + [a for a, o, p, _ in jp.title_alias_records()
                          if o == original and p == performer]
    core, annotation = _title_parts(track.title)
    def member(value):
        # Only the original spelling has a verified reading. Reading an
        # abbreviation/translation as Japanese could turn 音辞 into ネジ.
        return (_bound_title(value, original)
                or any(jp.title_alias_key(value) == jp.title_alias_key(n) for n in names))
    if not member(track.title):
        if not annotation or not member(core):
            return False
        if not any(jp.title_alias_similarity(annotation, n) == 1 for n in names):
            return False
    return True


def _title_alias_strength(title: str, artist: str, track: Track) -> float:
    if (normalize_for_version(track.artist) in ARTIST_UNKNOWN
            or (artist and artist_match_score([("", artist)], track) < .999)
            or not recording_version_ok(track, version_markers(title))):
        return 0.0
    local = jp.title_aliases(title)
    if local:
        for original, performer in local:
            if _local_title_member(track, original, performer):
                names = [original] + [a for a, o, p, _ in jp.title_alias_records()
                                      if o == original and p == performer]
                return max(jp.title_alias_similarity(title, name) for name in names)
        # A known short name must not be reinterpreted by an unrelated API hit.
        return 0.0
    # Bidirectional API evidence: canonical title and explicit translated name
    # form one identity. Never use an API's whole result list as title evidence.
    aliases = title_alias_evidence(track)
    core, annotation = _title_parts(track.title)
    if annotation and not (
            _bound_title(core, annotation)
            or any(jp.title_alias_similarity(annotation, alias) == 1 for alias in aliases)):
        return 0.0
    strength = max((jp.title_alias_similarity(title, alias) for alias in aliases), default=0.0)
    return strength if strength == 1 or artist else 0.0


def _verified_title_alias(title: str, artist: str, track: Track) -> bool:
    return bool(_title_alias_strength(title, artist, track))


def filter_title_alias_ambiguity(readings, tracks):
    """Reject approximate aliases pointing at different works; no blind top hit.

    Exact matches remain available. Applied before per-source truncation and
    after merging sources; the helper never changes Track.score or metadata.
    """
    ambiguous = set()
    for title, artist in list(readings)[:4]:
        works = {}
        fuzzy_ids = set()
        for track in tracks:
            strength = _title_alias_strength(title, artist, track)
            if not strength:
                continue
            local = jp.title_aliases(track.title)
            identity = ((local[0][0], local[0][1]) if len(local) == 1 else
                        (_title_parts(track.title)[0], track.artist))
            key = tuple(jp.title_alias_key(v) for v in identity)
            works.setdefault(key, []).append(id(track))
            if strength < 1:
                fuzzy_ids.add(id(track))
        if len(works) > 1:
            ambiguous.update(fuzzy_ids)
    return [track for track in tracks if id(track) not in ambiguous]


#: 跨书写系统「就是这首歌」的高相似兜底阈值（读音形态、空格/长音折叠后比对）。
#: 定在 0.92：足以容忍连浊「千本桜=せんぼんざくら vs senbonsakura」这类单点差异，
#: 又拦得住「同歌手另一首歌」——而且它只是**追加**通道，原硬门槛一字未放松。
_JP_STRICT_SIMILARITY = 0.92


def _jp_title_gate_ok(want_title: str, cand_title: str, want_artist: str,
                      cand_artist: str) -> bool:
    """日语跨书写系统的歌名强判定（读音形态上的「相等/包含」，再加高相似兜底）。

    适用前提：至少一侧含假名、或至少一侧是纯拉丁（罗马音输入）。
    一侧读不出读音（表里没有的汉字）时不参与 —— 不猜。
    """
    if version_markers(cand_title) - version_markers(want_title):
        return False
    if not (jp.has_kana(want_title) or jp.has_kana(cand_title)
            or _ascii_ish(want_title) or _ascii_ish(cand_title)):
        return False
    # Reading conversion drops brackets; never let that erase an unrequested
    # version/qualifier. Only attested reading annotations may be ignored here.
    annotated = _title_without_soundtrack_credit(cand_title)
    brackets = list(_BRACKET_NOISE.finditer(annotated))
    if brackets:
        core = _BRACKET_NOISE.sub(" ", annotated).strip()
        core_forms = set(jp.match_forms(core))
        if any(not core_forms.intersection(jp.match_forms(m.group()[1:-1].strip()))
               for m in brackets):
            return False
    try:
        fa, fb = jp.match_forms(want_title), jp.match_forms(cand_title)
    except Exception:                                    # noqa: BLE001
        return False
    if not fa or not fb:
        return False
    arts: "set[str]" = set()
    for value in (want_artist, cand_artist):
        if (value or "").strip():
            try:
                arts.update(jp.match_forms(value))
            except Exception:                            # noqa: BLE001
                pass
            arts.add(value.strip().casefold())
    best = 0.0
    for x in fa:
        for y in fb:
            if x == y:
                return True
            if x and x in y:
                leftover = [tok for tok in y.replace(x, " ").split() if tok]
                if not leftover:
                    return True
                if arts and all(any(tok in art for art in arts) for tok in leftover):
                    return True
            value = similarity(x, y)
            if value > best:
                best = value
    return best >= _JP_STRICT_SIMILARITY


def best_reading_match(readings: "Sequence[tuple[str, str]]", track: Track, *,
                       min_title: float, min_artist: float, strict_title: bool = True,
                       expected_album: str = "", reference_duration: float = 0
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
        if not title_ok and strict_title and expected_album:
            title_ok = _album_supported_title(want_t, want_a, track,
                                             expected_album, reference_duration)
        known_artist = (not getattr(readings, "required_artist", "")
                        or normalize_for_version(track.artist) not in ARTIST_UNKNOWN)
        if title_ok and as_ >= min_artist and known_artist:
            if ts + as_ > best_ok_sum:
                best_ok, best_ok_sum = pair, ts + as_
        if ts + as_ > best_any_sum:
            best_any, best_any_sum = pair, ts + as_
    if best_ok is not None:
        reading, ts, as_ = best_ok
        return True, reading, ts, as_
    reading, ts, as_ = best_any
    return False, reading, ts, as_


def _album_supported_title(title: str, artist: str, track: Track,
                           album: str, duration: float) -> bool:
    """Bounded typo tolerance with independent album, artist and duration evidence.

    Candidate metadata is inspected before any output-tag rewrite. Missing
    evidence does not qualify; ordinary exact matching remains available.
    """
    norm = normalize_for_version
    if (not artist or not album or not track.artist or not track.album
            or norm(artist) in ARTIST_UNKNOWN or norm(track.artist) in ARTIST_UNKNOWN
            or norm(artist) != norm(track.artist) or norm(album) != norm(track.album)):
        return False
    if (duration <= 0 or track.duration <= 0
            or not math.isfinite(duration) or not math.isfinite(track.duration)
            or abs(duration - track.duration) > 2
            or track_version_markers(track) or version_markers(title)):
        return False
    left = _title_without_soundtrack_credit(title)
    right = _title_without_soundtrack_credit(track.title)
    # Do not hide unknown qualifiers such as (R&B), featured artists, or editions.
    if _BRACKET.search(left) or _BRACKET.search(right):
        return False
    left, right = norm(left), norm(right)
    if min(len(left), len(right)) < 8:
        return False
    if re.findall(r"\d+", left) != re.findall(r"\d+", right):
        return False
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    edits = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes()
                if op != "equal")
    return edits == 1 and matcher.ratio() >= .94


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
    written = {normalize_for_version(value)
               for title, _artist in readings if not jp.has_kanji(title)
               for value in (jp.known_titles(title, kind="song") or jp.dictionary_spellings(title))}
    for t in tracks:
        t.score = round(max(_score_one(rt, ra, t, duration, prefer_ext) for rt, ra in readings), 4)
    # Retrieval preference is independent of confidence: Track.score also
    # decides reference-duration sampling and must not receive spelling bonuses.
    return sorted(tracks, key=lambda t: (
        normalize_for_version(_title_without_soundtrack_credit(t.title)) in written,
        t.score), reverse=True)


def rank_tracks(query_title: str, query_artist: str, tracks: Sequence[Track],
                *, duration: int = 0, prefer_ext: str = "") -> list[Track]:
    """Rank against a single ``(title, artist)`` reading (kept for simple callers)."""
    return rank_tracks_multi([(query_title, query_artist)], tracks,
                             duration=duration, prefer_ext=prefer_ext)
