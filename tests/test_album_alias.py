"""Deterministic translated-album lookup and false-positive guards."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic.search import album
from mmusic.search.album_alias import resolve_album_alias


def hit(name, artist, album_id="1"):
    return album.AlbumHit(source="kugou", album_id=album_id, name=name,
                          artist=artist, track_count=10, raw={})


class AliasHttp:
    def __init__(self, kind="album", fail=False):
        self.calls = []
        self.kind = kind
        self.fail = fail

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        if self.fail:
            raise OSError("offline")
        if "wbsearchentities" in url:
            return {"search": [{"id": "Q999", "match": {"text": "测试中文译名"}}]}
        if "props=labels%7Cclaims" in url:
            return {"entities": {"Q999": {
                "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {
                    "id": "Q482994" if self.kind == "album" else "Q7366"}}}}],
                           "P175": [{"mainsnak": {"datavalue": {"value": {"id": "Q888"}}}}]},
                "labels": {"zh": {"value": "测试中文译名"},
                           "en": {"value": "English Title"},
                           "ja": {"value": "日本語名"}, "ko": {"value": "한국어명"}},
            }}}
        return {"entities": {"Q888": {"labels": {"en": {"value": "Real Artist"}}}}}


class AlbumAliasTests(unittest.TestCase):
    def test_local_language_examples(self):
        for query, artist, title in (
            ("夏草", "", "夏草が邪魔をする"),
            ("幻灯", "Yorushika", "幻燈"),
            ("所以我放弃了音乐", "Yorushika", "だから僕は音楽を辞めた"),
            ("瞬间的第六感", "Aimyon", "瞬間的シックスセンス"),
            ("花样年华 Young Forever", "BTS", "화양연화 Young Forever"),
            ("颤栗", "Michael Jackson", "Thriller"),
            ("艾比路", "The Beatles", "Abbey Road"),
        ):
            with self.subTest(query=query):
                http = AliasHttp(fail=True)
                found = resolve_album_alias(http, query, artist)
                self.assertIsNotNone(found)
                self.assertIn(title, found.titles)
                self.assertEqual(http.calls, [])

    def test_wrong_artist_rejected(self):
        self.assertIsNone(resolve_album_alias(AliasHttp(fail=True), "颤栗", "Not Jackson"))
        from mmusic.search.album_alias import alias_artist_matches
        self.assertFalse(alias_artist_matches("Artist B", ("Artist A",)))
        self.assertFalse(alias_artist_matches("The Michael Jackson Tribute", ("Michael Jackson",)))
        self.assertTrue(alias_artist_matches("BTS (防弹少年团)", ("BTS",)))

    def test_remote_verified_and_bounded(self):
        http = AliasHttp()
        found = resolve_album_alias(http, "测试中文译名", "Real Artist")
        self.assertEqual(found.titles, ("English Title", "日本語名", "한국어명"))
        self.assertLessEqual(len(http.calls), 3)
        self.assertIsNone(resolve_album_alias(AliasHttp(kind="song"), "测试中文译名"))
        self.assertIsNone(resolve_album_alias(AliasHttp(), "测试中文译名", "Other Artist"))
        self.assertIsNone(resolve_album_alias(AliasHttp(fail=True), "测试中文译名"))

    def test_search_uses_translation_only_after_no_strong_direct_match(self):
        calls = []

        def search(_http, text, _limit):
            calls.append(text)
            if text == "颤栗":
                return [hit("颤栗精选", "Other Artist", "weak")]
            if text == "Thriller":
                return [hit("Thriller", "Michael Jackson", "right"),
                        hit("Thriller", "Cover Artist", "wrong")]
            return []

        with patch.object(album, "_kugou_search", search):
            hits = album.search_albums(AliasHttp(fail=True), "颤栗", sources={"kugou"})
            groups = album.group_albums(hits, query="颤栗")
        self.assertIn("Thriller", calls)
        verified = [g for g in groups if any(
            h.raw.get("_verified_alias_id") == "Q44320" for h in g.variants)]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0].artist, "Michael Jackson")
        self.assertFalse(any(h.artist == "Cover Artist" for g in groups for h in g.variants))

        calls.clear()
        def direct(_http, text, _limit):
            calls.append(text)
            return [hit("颤栗", "Michael Jackson")]

        with patch.object(album, "_kugou_search", direct):
            album.search_albums(AliasHttp(fail=True), "颤栗", sources={"kugou"})
        self.assertNotIn("Thriller", calls)

    def test_translated_artist_variants_merge(self):
        a = hit("だから僕は音楽を辞めた", "Yorushika", "a")
        b = hit("That's Why I Gave Up on Music", "ヨルシカ", "b")
        for h in (a, b):
            h.raw["_verified_query_alias"] = "所以我放弃了音乐"
            h.raw["_verified_alias_id"] = "Q65235123"
        groups = album.group_albums([a, b], query="所以我放弃了音乐")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].variants), 2)

    def test_direct_korean_titles_and_artist_alias_merge(self):
        def search(_http, text, _limit):
            if text != "花样年华 Young Forever":
                return []
            return [hit("화양연화 Young Forever", "BTS", "one"),
                    hit("화양연화 Young Forever", "BTS (防弹少年团)", "two")]

        with patch.object(album, "_kugou_search", search):
            hits = album.search_albums(AliasHttp(fail=True), "花样年华 Young Forever",
                                       artist="BTS", sources={"kugou"})
        groups = album.group_albums(hits, query="花样年华 Young Forever", artist="BTS")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].variants), 2)

    def test_simplified_input_keeps_original_and_queries_missing_source(self):
        calls = []
        def kugou(_http, text, _limit):
            calls.append(("kugou", text))
            return [hit("幻燈", "ヨルシカ", "full")]
        def kuwo(_http, text, _limit):
            calls.append(("kuwo", text))
            if text != "幻燈":
                return []
            h = hit("幻燈", "ヨルシカ", "other")
            h.source = "kuwo"
            return [h]
        with patch.object(album, "_kugou_search", kugou), patch.object(album, "_kuwo_search", kuwo):
            hits = album.search_albums(AliasHttp(fail=True), "幻灯", artist="yorushika",
                                       sources={"kugou", "kuwo"})
        groups = album.group_albums(hits, query="幻灯", artist="yorushika")
        self.assertEqual(len(groups), 1)
        self.assertEqual({h.source for h in groups[0].variants}, {"kugou", "kuwo"})
        self.assertIn(("kuwo", "幻燈"), calls)
        self.assertEqual(sum(s == "kugou" for s, _ in calls), 1)

    def test_cover_and_short_catalog_cannot_suppress_original_lookup(self):
        calls = []
        def search(_http, text, _limit):
            calls.append(text)
            if text == "所以我放弃了音乐":
                h = hit(text, "ヨルシカ", "short")
                h.track_count = 1
                return [h, hit(text, "Cover Artist", "cover")]
            h = hit("だから僕は音楽を辞めた", "ヨルシカ", "full")
            h.track_count = 14
            return [h]
        with patch.object(album, "_kugou_search", search):
            hits = album.search_albums(AliasHttp(fail=True), "所以我放弃了音乐", sources={"kugou"})
        groups = album.group_albums(hits, query="所以我放弃了音乐")
        original = next(g for g in groups if g.artist == "ヨルシカ")
        self.assertEqual(original.track_count, 14)
        self.assertEqual(album._source_best(original.variants)["kugou"].album_id, "full")
        self.assertIn("だから僕は音楽を辞めた", calls)

    def test_deluxe_and_piano_cannot_be_certified_as_original(self):
        from mmusic.search.album_alias import local_album_alias
        alias = local_album_alias("幻灯")
        self.assertFalse(album._alias_matches(hit("幻燈 (Deluxe Edition)", "ヨルシカ"), alias))
        self.assertFalse(album._alias_matches(hit("幻燈 (Piano)", "ヨルシカ"), alias))
        groups = album.group_albums([
            hit("Test Album", "Test Artist", "original"),
            hit("Test Album (Deluxe Edition)", "Test Artist", "deluxe"),
        ], query="Test Album")
        self.assertEqual(len(groups), 2)
        rerecordings = album.group_albums([
            hit("1989 (Deluxe)", "Taylor Swift", "old"),
            hit("1989 (Taylor's Version) (Deluxe)", "Taylor Swift", "new"),
        ], query="1989")
        self.assertEqual(len(rerecordings), 2)
        self.assertEqual(rerecordings[0].name, "1989 (Deluxe)")

    def test_other_hanzi_forms_without_curated_album(self):
        from mmusic.search.album_text import alternate
        self.assertEqual(album._sim("盗作", "盜作"), 1.0)
        self.assertEqual(album._norm_key("乐行者"), album._norm_key("樂行者"))
        self.assertEqual(alternate("幻灯"), "幻燈")
        self.assertEqual(alternate("发"), "发")  # No guess between 發/髮.

    def test_declared_count_is_not_a_complete_tracklist(self):
        from mmusic.model import Track
        h = hit("Test Album", "Test Artist")
        h.track_count = 14
        group = album.group_albums([h], query="Test Album")[0]
        notes = []
        rows = [Track(source="kugou", title="Only Track", artist="Test Artist")]
        with patch.object(album, "_fetch_source_lists", return_value=[(h, rows)]):
            actual = album.group_tracks(AliasHttp(), group, log=notes.append)
        self.assertEqual(len(actual), 1)
        self.assertEqual(group.track_count, 1)
        self.assertTrue(any("标称 14 首" in n and "实际获取 1 首" in n for n in notes))

    def test_failed_directories_allow_inferred_fallback_without_mixing(self):
        from mmusic.model import Track
        directory = hit("Test Album", "Test Artist")
        inferred = hit("Test Album", "Test Artist", "inferred")
        inferred.source = "aggregator"
        group = album.group_albums([directory, inferred], query="Test Album")[0]
        calls = []
        def fetch(_http, best, **kwargs):
            calls.append(set(best))
            if set(best) == {"aggregator"}:
                return [(inferred, [Track(source="aggregator", title="Only Track", artist="Test Artist")])]
            return []
        with patch.object(album, "_fetch_source_lists", fetch):
            rows = album.group_tracks(AliasHttp(), group)
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls[-1], {"aggregator"})

    def test_natsukusa_short_input_loads_seven_not_five(self):
        from mmusic.model import Track
        from mmusic.config import Settings
        japanese = "夏草が邪魔をする"
        english = "The Summer Grass Is Getting in My Way"
        titles = ["夏陰、ピアノを弾く", "カトレア", "言って。", "あの夏に咲け",
                  "飛行", "靴の花火", "雲と幽霊"]
        durations = [94, 188, 242, 258, 90, 303, 315]
        calls = []
        def search(source):
            def run(_http, text, _limit):
                calls.append((source, text))
                if source == "migu" or text == "夏草":
                    if source != "migu":
                        return []
                    h = hit(english, "Yorushika", "partial")
                    h.track_count = 5
                else:
                    h = hit(japanese, "ヨルシカ", "full-" + source)
                    h.track_count = 7
                h.source = source
                return [h]
            return run
        def tracks(_http, h, *_args):
            selected = range(7) if h.source != "migu" else [1, 2, 3, 5, 6]
            return [Track(source=h.source, title=titles[i], artist="ヨルシカ",
                          duration=durations[i]) for i in selected]
        with patch.object(album, "_migu_search", search("migu")), \
                patch.object(album, "_kugou_search", search("kugou")), \
                patch.object(album, "_kuwo_search", search("kuwo")), \
                patch.object(album, "_migu_derive", tracks), \
                patch.object(album, "_kugou_tracks", tracks), \
                patch.object(album, "_kuwo_tracks", tracks):
            hits = album.search_albums(AliasHttp(fail=True), "夏草", settings=Settings(),
                                       sources={"migu", "kugou", "kuwo"})
            groups = album.group_albums(hits, query="夏草")
            self.assertEqual(len(groups), 1)
            actual = album.group_tracks(AliasHttp(), groups[0], settings=Settings())
        self.assertEqual([t.title for t in actual], titles)
        self.assertEqual(groups[0].track_count, 7)
        self.assertIn(("kugou", japanese), calls)
        self.assertIn(("kuwo", japanese), calls)

    def test_partial_majority_does_not_overrule_verified_release_count(self):
        from mmusic.model import Track
        lists = []
        for source, count, prefix in [("migu", 5, "translation"),
                                      ("kuwo", 5, "translation"), ("kugou", 7, "original")]:
            h = hit("夏草が邪魔をする", "ヨルシカ", source)
            h.source = source
            h.raw["_verified_track_count"] = 7
            rows = [Track(source=source, title=f"{prefix}{i}", artist="ヨルシカ")
                    for i in range(count)]
            lists.append((h, rows))
        cluster = album._dominant_cluster(lists, None)
        self.assertEqual([h.source for h, _ in cluster], ["kugou"])

    def test_ambiguous_prefix_is_not_auto_expanded(self):
        from mmusic.search import album_alias
        rows = ({"id": "Q1", "titles": ["夏草甲"], "artists": ["Yorushika"]},
                {"id": "Q2", "titles": ["夏草乙"], "artists": ["King Gnu"]})
        with patch.object(album_alias, "_local", return_value=rows):
            self.assertIsNone(album_alias.local_album_alias("夏草"))
            found = album_alias.local_album_alias("夏草", "Yorushika")
            self.assertEqual(found.entity_id, "Q1")
            self.assertIsNone(album_alias.local_album_alias("夏"))

    def test_source_observed_full_title_expands_without_curated_entry(self):
        from mmusic.config import Settings
        full = "星辰与大海的约定"
        calls = []
        partial = hit(full, "Example Artist", "inferred")
        partial.source = "aggregator"
        partial.track_count = 3
        def search(_http, text, _limit):
            calls.append(text)
            return [hit(full, "Example Artist", "directory")] if text == full else []
        with patch.object(album, "_kugou_search", search), \
                patch.object(album, "_aggregator_search", return_value=[partial]):
            hits = album.search_albums(AliasHttp(), "星辰", settings=Settings(),
                                       sources={"kugou", "aggregator"})
        self.assertIn(full, calls)
        groups = album.group_albums(hits, query="星辰")
        self.assertEqual(len(groups), 1)
        self.assertEqual({h.source for h in groups[0].variants}, {"kugou", "aggregator"})
        self.assertFalse(any(h.raw.get("_verified_alias_id") for h in hits))


if __name__ == "__main__":
    unittest.main()
