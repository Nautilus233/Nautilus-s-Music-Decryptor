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
from mmusic.suggestions import query_suggestions, query_suggestion_choices
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
                self.assertIn("你可能想找的歌是：Unfamiliar Long Melodi（ヨルシカ）？", error.exception.message)
                self.assertLessEqual(source.queries, 4)
                # Different input must not inherit the previous request's hints.
                with self.assertRaises(SearchNoResultError) as other:
                    pipe.search_candidates_ex("全然違う曲", artist="yorushika")
                self.assertNotIn("你可能想找的歌是", other.exception.message)
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

    def test_short_titles_reject_weak_substitution_and_prefix_extension(self):
        for query, title in (("晴天", "晴空"), ("春", "春ひさぎ"),
                             ("夜行", "夜行者"), ("里香", "七里香")):
            with self.subTest(query=query, title=title):
                self.assertEqual(query_suggestions([(query, "yorushika")], [song(title)]), [])

    def test_short_interior_omission_with_artist(self):
        self.assertEqual(query_suggestion_choices([("花亡霊", "yorushika")],
                                                  [song("花に亡霊")]),
                         [("花に亡霊", "ヨルシカ")])

    def test_short_query_extra_character_can_use_explicit_artist(self):
        self.assertEqual(query_suggestion_choices([("夜曲子", "周杰伦")],
                                                    [song("夜曲", "周杰伦")]),
                         [("夜曲", "周杰伦")])
        self.assertEqual(query_suggestions([("夜曲子", "周杰伦")],
                                           [song("夜曲", "周杰伦")]),
                         ["夜曲（周杰伦）"])

    def test_artistless_short_query_requires_two_sources(self):
        tracks = [Track("source-a", "夜曲", "周杰伦"),
                  Track("source-b", "夜曲", "周杰伦")]
        self.assertEqual(query_suggestion_choices([("夜曲子", "")], tracks),
                         [("夜曲", "周杰伦")])
        self.assertEqual(query_suggestions([("夜曲子", "")], [tracks[0]]), [])

    def test_two_character_substitution_requires_artist_and_two_sources(self):
        one = [Track("source-a", "夜曲", "周杰伦")]
        two = one + [Track("source-b", "夜曲", "周杰伦")]
        self.assertEqual(query_suggestions([("夜困", "周杰伦")], one), [])
        self.assertEqual(query_suggestion_choices([("夜困", "周杰伦")], two),
                         [("夜曲", "周杰伦")])

    def test_choices_are_structured_and_version_identity_is_preserved(self):
        candidate = song("夜曲 (伴奏)", "周杰伦")
        self.assertEqual(query_suggestion_choices([("夜曲", "周杰伦")], [candidate]),
                         [("夜曲 (伴奏)", "周杰伦")])
        self.assertEqual(query_suggestions([("夜曲", "周杰伦")], [candidate]),
                         ["夜曲 (伴奏)（周杰伦）"])

    def test_short_edit_matrix_is_not_song_specific(self):
        for title, artist in (("晴天", "周杰伦"), ("海風", "甲歌手"),
                              ("星空", "乙歌手"), ("北の街", "丙歌手"),
                              ("Melon", "Singer")):
            for query in (title + "子", title[0] + "子" + title[1:]):
                tracks = [Track("a", title, artist), Track("b", title, artist)]
                with self.subTest(query=query):
                    self.assertEqual(query_suggestion_choices([(query, "")], tracks),
                                     [(title, artist)])
                    self.assertEqual(query_suggestion_choices([(query, artist)], tracks[:1]),
                                     [(title, artist)])
        for artist in ("", "群星", "unknown", "未知艺术家"):
            tracks = [Track("a", "夜曲", artist), Track("b", "夜曲", artist)]
            self.assertEqual(query_suggestion_choices([("夜曲子", "")], tracks), [])

    def test_sources_cannot_pool_duplicates_artists_or_readings(self):
        bad_pools = [
            [Track("a", "夜曲", "周杰伦")] * 5,
            [Track("a", "夜曲", "周杰伦"), Track("A", "夜曲", "周杰伦")],
            [Track("", "夜曲", "周杰伦"), Track("b", "夜曲", "周杰伦")],
            [Track("a", "夜曲", "周杰伦"), Track("b", "夜曲", "别的歌手")],
            [Track("a", "夜曲", "周杰伦"), Track("b", "夜曲", "周杰伦", raw={"query_echo": True})],
        ]
        for tracks in bad_pools:
            self.assertEqual(query_suggestion_choices([("夜曲子", "")], tracks), [])
        tracks = [Track("a", "夜曲", "周杰伦"), Track("b", "夜曲", "周杰伦")]
        self.assertEqual(query_suggestion_choices([("夜曲子", "错歌手"), ("夜曲子", "")], tracks), [])
        self.assertEqual(query_suggestion_choices([("夜曲子", "unknown"), ("夜曲子", "")], tracks), [])
        self.assertEqual(query_suggestion_choices([("夜曲子", "别的歌手"), ("无关", "周杰伦")], tracks), [])
        self.assertEqual(query_suggestion_choices([("夜困", "")], tracks), [])

    def test_equally_close_works_suppress_short_guess_and_exact_wins(self):
        tracks = [Track(source, title, "歌手") for source in ("a", "b")
                  for title in ("星海", "星河")]
        self.assertEqual(query_suggestion_choices([("星海河", "")], tracks), [])
        exact = Track("c", "星海河", "歌手")
        self.assertEqual(query_suggestion_choices([("星海河", "")], tracks + [exact]),
                         [("星海河", "歌手")])

    def test_metadata_versions_append_without_fabricating_studio_identity(self):
        for kwargs, suffix in (({"album": "Live at Somewhere"}, "Live"),
                               ({"raw": {"song": {"title": "夜曲 (伴奏)"}}}, "伴奏"),
                               ({"raw": {"subtitle": "cover"}}, "翻唱"),
                               ({"raw": {"album": {"name": "instrumental"}}}, "伴奏")):
            with self.subTest(kwargs=kwargs):
                track = song("夜曲", "周杰伦", **kwargs)
                before = copy.deepcopy(track)
                self.assertEqual(query_suggestion_choices([("夜曲", "")], [track]),
                                 [(f"夜曲 ({suffix})", "周杰伦")])
                self.assertEqual(track, before)
        self.assertEqual(query_suggestion_choices([("夜曲", "")],
                         [song("夜曲 (伴奏)", "周杰伦")], requested={"live"}), [])

    def test_original_and_instrumental_evidence_preserve_real_title(self):
        for title in ("夜曲 (原版)", "夜曲（伴奏）", "夜曲 - Instrumental"):
            tracks = [Track("a", title, "周杰伦"), Track("b", title, "周杰伦")]
            self.assertEqual(query_suggestion_choices([("夜曲子", "")], tracks),
                             [(title, "周杰伦")])
        self.assertEqual(query_suggestion_choices([("夜曲子", "")],
                         [Track("a", "夜曲 (未知说明)", "周杰伦"),
                          Track("b", "夜曲 (未知说明)", "周杰伦")]), [])

    def test_choices_and_labels_share_order_dedup_limit_and_full_identity(self):
        readings = [("夜曲", "周杰伦"), ("晴天", "周杰伦")]
        tracks = [Track("a", "夜曲 (原版)", "周杰伦"),
                  Track("b", "夜曲 (原版)", "周杰伦"),
                  Track("a", "晴天", "周杰伦")]
        for limit in (1, 2, 99):
            choices = query_suggestion_choices(readings, iter(tracks), limit=limit)
            self.assertEqual(query_suggestions(readings, iter(tracks), limit=limit),
                             [f"{title}（{artist}）" for title, artist in choices])
            self.assertEqual(len(choices), min(limit, 2))

    def test_nested_injection_echo_and_conflicting_name_rejected(self):
        for raw in ({"song": {"title": "无关歌曲 (Live)"}},
                    {"song": {"query_echo": True, "title": "夜曲"}},
                    {"alias": ["<script>alert(1)</script>"]},
                    {"subtitle": "歌词：夜曲"},
                    {"title": "夜曲" + "x" * 200}):
            self.assertEqual(query_suggestion_choices([("夜曲", "")],
                             [song("夜曲", "周杰伦", raw=raw)]), [])

    def test_budgeted_metadata_readings_and_comparisons(self):
        from mmusic import suggestions
        # Current Kugou records contain >100 fields, mostly irrelevant. A
        # bounded whitelist must not discard their valid identity wholesale.
        realistic = {"SongName": "夜曲", **{f"numeric{i}": i for i in range(104)}}
        self.assertEqual(query_suggestion_choices([("夜曲子", "")], [
            Track("migu", "夜曲", "周杰伦"),
            Track("kugou", "夜曲", "周杰伦", raw=realistic)]), [("夜曲", "周杰伦")])
        cycle = {}
        cycle["song"] = cycle
        for raw in (cycle, {"alias": ["夜曲"] * 10000},
                    {str(i): "x" for i in range(1000)}):
            self.assertEqual(query_suggestion_choices([("夜曲", "")],
                             [song("夜曲", "周杰伦", raw=raw)]), [])

        def readings():
            for _ in range(8):
                yield ("夜曲", "")
            raise AssertionError("unbounded readings")
        self.assertEqual(query_suggestion_choices(readings(), [song("夜曲", "周杰伦")]),
                         [("夜曲", "周杰伦")])
        aliases = ["Alias" + str(i) for i in range(24)]
        tracks = [song("Unrelated title", raw={"alias": aliases}) for _ in range(256)]
        with patch.object(suggestions, "_title_similarity", wraps=suggestions._title_similarity) as similarity:
            self.assertEqual(query_suggestion_choices([("Unfamiliar value", "")] * 8, tracks), [])
            self.assertEqual(similarity.call_count, suggestions._MAX_COMPARISONS)

    def test_lyrics_and_query_echo_cannot_be_alias_evidence(self):
        for raw in ({"query": "卖春"}, {"lyrics": {"alias": ["卖春"]}},
                    {"highlight": "卖春"}, {"song": {"title": "无关歌曲", "alias": ["卖春"]}}):
            with self.subTest(raw=raw):
                self.assertEqual(query_suggestions([("卖春", "yorushika")],
                                                    [song("春ひさぎ", raw=raw)]), [])
        for raw in ({"query_echo": True}, {"type": "lyrics"}, {"queryEcho": "true"}):
            self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")],
                                                [song("春ひさぎ", raw=raw)]), [])

    def test_suggestions_retain_live_and_cover_evidence(self):
        tracks = [song("春ひさぎ (Live)"), song("春ひさぎ (cover)"),
                  song("春ひさぎ", album="ヨルシカ LIVE「月光」"),
                  song("春ひさぎ", raw={"song": {"title": "春ひさぎ (ライブ)"}})]
        self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")], tracks),
                         ["春ひさぎ (Live)（ヨルシカ）", "春ひさぎ (cover)（ヨルシカ）"])
        self.assertEqual(query_suggestions([("春ひさぎ", "yorushika")], [tracks[2]],
                                          requested={"live"}), ["春ひさぎ (Live)（ヨルシカ）"])

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
