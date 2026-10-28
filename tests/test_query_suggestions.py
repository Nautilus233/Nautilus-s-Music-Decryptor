"""Offline hint tests: weak retrieval evidence never becomes acceptance."""
from __future__ import annotations

import copy
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic.model import Track
from mmusic.suggestions import query_suggestions
from mmusic.config import Settings
from mmusic.errors import SearchNoResultError
from mmusic.pipeline import Pipeline
from mmusic.search.base import SearchSource


def song(title="だから僕は音楽を辞めた", artist="ヨルシカ", **kwargs):
    return Track("fixture", title, artist, **kwargs)


class QuerySuggestionTests(unittest.TestCase):
    def test_failed_pipeline_includes_hint_without_resolving_or_downloading(self):
        class MetadataOnlySource(SearchSource):
            name = label = "suggestion_fixture"
            queries = 0

            def search(self, query, **kwargs):
                self.queries += 1
                return [song("Unfamiliar Long Melodi")]

            def resolve(self, *args, **kwargs):
                raise AssertionError("suggestions may not resolve or download")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = MetadataOnlySource(None)
            pipe = Pipeline(Settings(remember_hashes=False), sources=[source],
                            cache_path=root / "c.json", index_path=root / "i.db")
            try:
                with self.assertRaises(SearchNoResultError) as error:
                    pipe.search_candidates_ex("Unfamiliar Long Melody", artist="yorushika")
                self.assertIn("你可能想搜：Unfamiliar Long Melodi（ヨルシカ）", error.exception.message)
                self.assertLessEqual(source.queries, 4)
                # Different input must not inherit the previous request's hints.
                with self.assertRaises(SearchNoResultError) as other:
                    pipe.search_candidates_ex("全然違う曲", artist="yorushika")
                self.assertNotIn("你可能想搜", other.exception.message)
            finally:
                pipe.close()

    def test_long_title_missing_or_wrong_character_with_same_artist(self):
        for title in ("だから僕は音楽辞めた", "だから僕は音楽を止めた"):
            with self.subTest(title=title):
                self.assertEqual(query_suggestions([(title, "yorushika")], [song()]),
                                 ["だから僕は音楽を辞めた（ヨルシカ）"])

    def test_explicit_artist_unknown_and_wrong_are_not_evidence(self):
        for artist in ("", "unknown", "群星", "未知艺术家", "tayori"):
            with self.subTest(artist=artist):
                self.assertEqual(query_suggestions(
                    [("だから僕は音楽を止めた", "yorushika")], [song(artist=artist)]), [])
        self.assertEqual(query_suggestions([("atarayo", "atarayo")],
                                            [song("atarayo", "tayori")]), [])

    def test_same_artist_alone_cannot_suggest_another_song(self):
        self.assertEqual(query_suggestions([("卖春", "yorushika")],
                         [song("夜行"), song("花に亡霊"), song("春泥棒")]), [])

    def test_real_api_alias_can_bridge_short_translation(self):
        candidate = song("春ひさぎ", raw={"alias": ["卖春"]})
        self.assertEqual(query_suggestions([("卖春", "yorushika")], [candidate]),
                         ["春ひさぎ（ヨルシカ）"])
        self.assertEqual(query_suggestions([("买春", "yorushika")], [candidate]), [])

    def test_long_translated_alias_near_match_still_needs_artist_identity(self):
        candidate = song(raw={"tns": ["所以我放弃了音乐"]})
        self.assertEqual(query_suggestions([("所以我放弃音乐", "yorushika")], [candidate]),
                         ["だから僕は音楽を辞めた（ヨルシカ）"])
        self.assertEqual(query_suggestions([("所以我放弃音乐", "tayori")], [candidate]), [])

    def test_written_forms_fold_without_changing_the_original_label(self):
        self.assertEqual(query_suggestions([("鱼", "atarayo")], [song("魚", "あたらよ")]),
                         ["魚（あたらよ）"])

    def test_short_titles_must_not_use_edit_distance_or_substrings(self):
        for query, title in (("晴天", "晴空"), ("春", "春ひさぎ"),
                             ("花亡霊", "花に亡霊"), ("夜行", "夜行者")):
            with self.subTest(query=query, title=title):
                self.assertEqual(query_suggestions([(query, "yorushika")], [song(title)]), [])

    def test_lyrics_and_query_echo_cannot_be_alias_evidence(self):
        for raw in ({"query": "卖春"}, {"lyrics": {"alias": ["卖春"]}},
                    {"highlight": "卖春"}, {"song": {"title": "无关歌曲", "alias": ["卖春"]}}):
            with self.subTest(raw=raw):
                self.assertEqual(query_suggestions([("卖春", "yorushika")],
                                                    [song("春ひさぎ", raw=raw)]), [])
        for raw in ({"query_echo": True}, {"type": "lyrics"}, {"queryEcho": "true"}):
            self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")],
                                                [song("春ひさぎ", raw=raw)]), [])

    def test_default_rejects_live_and_cover_in_title_album_and_nested_fields(self):
        tracks = [song("春ひさぎ (Live)"), song("春ひさぎ (cover)"),
                  song("春ひさぎ", album="ヨルシカ LIVE「月光」"),
                  song("春ひさぎ", raw={"song": {"title": "春ひさぎ (ライブ)"}})]
        self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")], tracks), [])
        self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")], [tracks[2]],
                                          requested={"live"}), ["春ひさぎ（ヨルシカ）"])

    def test_title_and_artist_must_come_from_same_reading(self):
        readings = [("夜行", "tayori"), ("晴天", "yorushika")]
        self.assertEqual(query_suggestions(readings, [song("夜行")]), [])
        self.assertEqual(query_suggestions([("夜行", "tayori"), ("夜行", "")],
                                            [song("夜行")]), [])

    def test_required_artist_remains_binding(self):
        class Readings(list):
            required_artist = "atarayo"
        self.assertEqual(query_suggestions(Readings([("春ひさぎ", "yorushika")]),
                                            [song("春ひさぎ")]), [])

    def test_collaboration_identity_and_identity_first_order(self):
        tracks = [song("春ひさぎ", "ヨルシカ/tayori"), song("春ひさぎ")]
        self.assertEqual(query_suggestions([("春ひさぎ", "ヨルシカ")], tracks),
                         ["春ひさぎ（ヨルシカ）", "春ひさぎ（ヨルシカ/tayori）"])

    def test_junk_html_long_output_and_lyrics_are_skipped(self):
        for title in ("!!!", "<b>春ひさぎ</b>", "&lt;b&gt;春ひさぎ", "x" * 121,
                      "歌词：春ひさぎ", "春ひさぎ\n歌词", "https://example.test/春ひさぎ"):
            with self.subTest(title=title):
                self.assertEqual(query_suggestions([(title, "yorushika")], [song(title)]), [])
        huge = song("山" * 110, "川" * 70)
        self.assertEqual(query_suggestions([(huge.title, huge.artist)], [huge]), [])

    def test_maximum_three_dedup_and_limit(self):
        readings = [(name, "ヨルシカ") for name in ("夜行", "春ひさぎ", "花に亡霊", "春泥棒")]
        tracks = [song(name) for name, _ in readings]
        tracks.insert(1, Track("other", "夜行", "ヨルシカ"))
        self.assertEqual(query_suggestions(readings, tracks, limit=99),
                         ["夜行（ヨルシカ）", "春ひさぎ（ヨルシカ）", "花に亡霊（ヨルシカ）"])
        self.assertEqual(len(query_suggestions(readings, tracks, limit=1)), 1)
        self.assertEqual(query_suggestions(readings, tracks, limit=0), [])

    def test_near_title_is_suggestion_only_no_mutation_or_network(self):
        readings = [("だから僕は音楽を止めた", "yorushika")]
        candidate = song()
        before = copy.deepcopy((readings, candidate))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             patch("socket.create_connection", side_effect=AssertionError("network")), \
             patch("mmusic.jp.dictionary_spellings", side_effect=AssertionError("expansion")):
            self.assertEqual(len(query_suggestions(readings, [candidate])), 1)
        self.assertEqual((readings, candidate), before)

    def test_invalid_empty_inputs_and_bounded_candidate_iterator(self):
        self.assertEqual(query_suggestions([], [song()]), [])
        self.assertEqual(query_suggestions([("", "yorushika")], [song()]), [])
        self.assertEqual(query_suggestions([("!!!", "yorushika")], [song()]), [])

        def candidates():
            for _ in range(256):
                yield song("无关歌曲")
            raise AssertionError("unbounded candidate iteration")
        self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")], candidates()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
