"""Japanese input matrix: typed fields, deterministic spellings and bounded queries."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic import jp
from mmusic.config import Settings
from mmusic.errors import SearchNoResultError
from mmusic.match import (artist_match_score, best_reading_match, cross_similarity,
                          rank_tracks_multi, title_gate_ok, version_markers)
from mmusic.model import Track
from mmusic.pipeline import Pipeline
from mmusic.search.album import _artist_matches
from mmusic.search.base import SearchSource
from mmusic.search.query import MAX_PER_SOURCE, QueryVariant, expand


def accepted(readings, title, artist):
    return best_reading_match(readings, Track("test", title, artist),
                              min_title=.6, min_artist=.6)[0]


class FakeSource(SearchSource):
    name = "jp_fixture"
    label = "Japanese metadata fixture"

    def __init__(self, tracks):
        super().__init__(None)
        self.tracks, self.queries = tracks, []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return self.tracks

    def resolve(self, track, **kwargs):
        raise AssertionError("metadata-only tests must not resolve or download")


class JapaneseInputTests(unittest.TestCase):
    def test_every_accepted_syllable_and_both_scripts(self):
        for roman, hira in jp._R2K.items():
            with self.subTest(roman=roman):
                self.assertEqual(jp.romaji_to_kana(roman), hira)
                self.assertEqual(jp.romaji_to_kana(roman.upper()), hira)
                self.assertEqual(jp.hiraganize(jp.katakanaize(hira)), hira)

    def test_moraic_n_sokuon_and_long_vowels(self):
        examples = {
            "konnichiwa": "こんにちわ", "konnichiha": "こんにちは",
            "konnya": "こんにゃ", "shin'you": "しんよう", "shinyou": "しにょう",
            "kitto": "きっと", "matcha": "まっちゃ", "gakkou": "がっこう",
            "shinjuku": "しんじゅく", "ryokou": "りょこう", "TŌKYŌ": "とうきょう",
            "xtsu": "っ", "ltsu": "っ", "san": "さん", "sann": "さん",
        }
        for roman, hira in examples.items():
            with self.subTest(roman=roman):
                self.assertEqual(jp.romaji_to_kana(roman), hira)
        self.assertEqual(jp.hiraganize("ｻｶﾅ"), "さかな")
        self.assertGreater(cross_similarity("tōkyō", "とうきょう"), .99)
        self.assertGreater(cross_similarity("toookyou", "とうきょう"), .99)

    def test_both_kana_are_reachable_within_four_queries(self):
        for roman in ("sakana", "arigatou", "konnichiwa", "atarayo", "kitto", "ryokou"):
            with self.subTest(roman=roman):
                hira = jp.romaji_to_kana(roman)
                first = [v.text for v in expand(roman, [(roman, "")])[:MAX_PER_SOURCE * 2]]
                self.assertIn(hira, first)
                self.assertIn(jp.katakanaize(hira), first)
                self.assertEqual(len(first), len(set(first)))

    def test_dictionary_reverse_is_attested_and_ambiguity_bounded(self):
        for roman, kanji in (("sakana", "魚"), ("yume", "夢")):
            with self.subTest(roman=roman):
                self.assertIn(kanji, jp.dictionary_spellings(roman))
                self.assertIn(jp.romaji_to_kana(roman), jp._words()[kanji])
                self.assertIn(kanji, [v.text for v in expand(roman, [(roman, "")])[:4]])
        self.assertEqual(jp.dictionary_spellings("hana"), ())
        self.assertEqual(jp.dictionary_spellings("ame"), ())
        self.assertEqual(jp.dictionary_spellings("thisisnotjapanese"), ())
        self.assertEqual(jp.dictionary_spellings("mizutamaranaiunknown"), ())
        self.assertNotIn("魚", [v for v, _ in jp.search_variants("sakana", kind="artist")])

    def test_written_form_is_not_skipped_by_a_successful_romaji_homonym(self):
        class HomonymSource(FakeSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "魚":
                    return [Track(self.name, "魚", "あたらよ")]
                if query == "sakana":
                    return [Track(self.name, "Sakana", "Unrelated Latin Performer")]
                return []

        source = HomonymSource([])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipe = Pipeline(Settings(remember_hashes=False), sources=[source],
                            cache_path=root / "cache.json", index_path=root / "index.db")
            try:
                seed, readings, query, requested = pipe._seed_for("sakana")
                pool = pipe._collect_pool(seed, readings, query, requested)
                self.assertEqual(source.queries[0], "魚")
                self.assertTrue(any(t.title == "魚" and t.artist == "あたらよ" for t in pool))
                self.assertLessEqual(len(source.queries), MAX_PER_SOURCE * 2)
            finally:
                pipe.close()

    def test_attested_spelling_survives_per_source_top_three(self):
        tracks = [Track("jp_fixture", "鱼", f"其他歌手{i}") for i in range(6)]
        fish = Track("jp_fixture", "魚", "あたらよ", ext="flac")
        source = FakeSource(tracks + [fish])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipe = Pipeline(Settings(remember_hashes=False), sources=[source],
                            cache_path=root / "cache.json", index_path=root / "index.db")
            try:
                seed, readings, query, requested = pipe._seed_for("sakana")
                pool = pipe._collect_pool(seed, readings, query, requested)
                self.assertIn(fish, pool)
                self.assertEqual(pool[0], fish)
            finally:
                pipe.close()

    def test_spelling_preference_does_not_change_duration_confidence(self):
        fish = Track("jp_fixture", "魚", "あたらよ", ext="flac", duration=245)
        readings = [("sakana", "atarayo")]
        rank_tracks_multi(readings, [fish], prefer_ext="mp3")
        preferred_score = fish.score
        with patch.object(jp, "dictionary_spellings", return_value=()):
            rank_tracks_multi(readings, [fish], prefer_ext="mp3")
        self.assertEqual(preferred_score, fish.score)
        self.assertLess(preferred_score, .95)

    def test_reading_conversion_cannot_erase_unverified_bracket_qualifiers(self):
        for qualifier in ("冰心版", "加速氛围冰心版", "说唱版", "未知说明"):
            for core in ("Sakana", "魚"):
                with self.subTest(core=core, qualifier=qualifier):
                    self.assertFalse(accepted([("sakana", "")], f"{core}({qualifier})", "あたらよ"))
        # A verifiable pronunciation annotation is not an alternative recording.
        self.assertTrue(accepted([("sakana", "")], "魚(sakana)", "あたらよ"))

    def test_orthographic_matching_does_not_change_display_fields(self):
        for query, title in (("鱼", "魚"), ("魚", "鱼"), ("幻灯", "幻燈"),
                             ("红莲华", "紅蓮華"), ("sakana", "魚")):
            with self.subTest(query=query):
                track = Track("test", title, "ヨルシカ")
                self.assertTrue(title_gate_ok(query, "", track))
                self.assertGreaterEqual(cross_similarity(query, title), .99)
                self.assertEqual(track.title, title)
        self.assertEqual(jp.search_variants("夜曲"), [])

    def test_curated_short_names_need_full_title_and_original_performer(self):
        canonical = "だから僕は音楽を辞めた"
        self.assertIn((canonical, "ヨルシカ"), jp.title_aliases("音辞"))
        self.assertTrue(accepted([("音辞", "")], canonical, "ヨルシカ"))
        for title, artist in ((canonical, "Other Artist"), (canonical, ""),
                              (canonical + "(ライブ)", "ヨルシカ"),
                              ("ネジ", "ヨルシカ"), ("音楽を辞めない", "ヨルシカ")):
            with self.subTest(title=title, artist=artist):
                self.assertFalse(accepted([("音辞", "")], title, artist))
        for arbitrary in ("音", "辞", "僕辞", "から音", "音辞です"):
            self.assertEqual(jp.title_aliases(arbitrary), ())
            self.assertFalse(accepted([(arbitrary, "")], canonical, "ヨルシカ"))
        good = Track("test", canonical, "ヨルシカ")
        rank_tracks_multi([("音辞", "")], [good])
        self.assertGreaterEqual(good.score, .72)

    def test_typed_artist_cannot_be_title_or_an_unknown_placeholder(self):
        for title in ("", "atarayo", "tayori", "魚", "sakana", "極夜"):
            readings = Pipeline._seed_for(title, "atarayo")[1]
            self.assertEqual(list(readings), [(title, "atarayo")])
            for bad_title, bad_artist in (("atarayo", "tayori"), (title, ""),
                                          (title, "未知"), (title, "群星"),
                                          (title, "VA"), (title, "tayori")):
                with self.subTest(title=title, bad_artist=bad_artist):
                    self.assertFalse(accepted(readings, bad_title, bad_artist))
        self.assertTrue(accepted(Pipeline._seed_for("極夜", "atarayo")[1], "極夜", "あたらよ"))
        self.assertTrue(accepted(Pipeline._seed_for("極夜", "ATARAYO")[1], "極夜", "アタラヨ"))

    def test_artist_match_uses_artist_tables_and_strict_reading_identity(self):
        for want, cand in (("ROSALIA", "ROSALÍA"), ("Rosali\u0301a", "ROSALIA"),
                           ("Beyonce", "Beyoncé"), ("Sigur Ros", "Sigur Rós")):
            self.assertEqual(artist_match_score([("x", want)], Track("", "x", cand)), 1)
            self.assertTrue(_artist_matches(want, cand))
        self.assertLess(artist_match_score([("x", "がく")], Track("", "x", "かく")), .6)
        for want, cand in (("atarayo", "tayori"), ("tayori", "atarayo"),
                            ("sakana", "魚"), ("gurenge", "紅蓮華")):
            self.assertLess(artist_match_score([("x", want)], Track("", "x", cand)), .6)
            self.assertFalse(_artist_matches(want, cand))
        for want, cand in (("atarayo", "あたらよ"), ("yorushika", "ヨルシカ"),
                            ("kenshi yonezu", "米津玄師"), ("YOASOBI", "ヨアソビ")):
            self.assertTrue(_artist_matches(want, cand), (want, cand))
        self.assertEqual(jp.known_kind("gurenge"), "song")
        self.assertEqual(jp.known_kind("yorushika"), "artist")

    def test_legacy_unlabelled_files_still_work(self):
        readings = Pipeline._seed_from_name("周杰伦 - 黑色毛衣")[0]
        self.assertTrue(accepted(readings, "黑色毛衣 - 周杰伦", ""))
        self.assertTrue(accepted([("魚", "")], "魚", ""))

    def test_japanese_versions_are_not_originals(self):
        for marker in ("ライブ", "ライヴ", "カバー", "インスト", "インストゥルメンタル",
                       "アコースティック", "リミックス", "オフボーカル", "ピアノ"):
            for title in (f"魚({marker})", f"魚（{marker}）", f"魚 - {marker}"):
                with self.subTest(title=title):
                    self.assertIn(marker, version_markers(title))
                    self.assertFalse(accepted([("sakana", "yorushika")], title, "ヨルシカ"))
        for title in ("ライブのあとで", "カバーガラス", "ピアノの森"):
            self.assertEqual(version_markers(title), set())
            self.assertTrue(accepted([(title, "")], title, "ヨルシカ"))

    def test_pipeline_preserves_explicit_artist_during_typo_recovery(self):
        for title, artist in (("sakana", "atarayo"), ("saknaa", "atarayo"),
                               ("sakana", "yroushika")):
            tracks = [Track("jp_fixture", "魚", ""), Track("jp_fixture", "魚", "未知"),
                      Track("jp_fixture", "atarayo", "tayori"),
                      Track("jp_fixture", "魚", "tayori")]
            source = FakeSource(tracks)
            with tempfile.TemporaryDirectory() as tmp:
                pipe = Pipeline(Settings(remember_hashes=False), sources=[source],
                                cache_path=Path(tmp) / "cache.json", index_path=Path(tmp) / "index.db")
                seed, readings, query, requested = pipe._seed_for(title, artist)
                # Force adversarial typo candidates in both fields, including
                # a reversed pair and a dropped-artist candidate. None can
                # override an explicitly supplied artist.
                corrections = [("魚", artist), ("魚", "tayori"), (artist, "tayori"), ("魚", "")]
                with patch("mmusic.pipeline.fuzzy.corrected_readings", return_value=corrections):
                    with self.assertRaises(SearchNoResultError):
                        pipe._collect_pool(seed, readings, query, requested)
                self.assertLessEqual(len(source.queries), MAX_PER_SOURCE * 2)
                # Also exercise a direct caller that hands the gate untyped
                # hostile alternates, bypassing _collect_pool's typed wrapper.
                kept = pipe._search_and_gate(source, QueryVariant(query, "test"),
                                             readings, requested, alt_readings=corrections)[0]
                self.assertEqual(kept, [])
                pipe.close()

    def test_pipeline_dictionary_and_alias_recall_without_audio(self):
        for title, artist, candidate in (
            ("sakana", "yorushika", Track("jp_fixture", "魚", "ヨルシカ")),
            ("鱼", "yorushika", Track("jp_fixture", "魚", "ヨルシカ")),
            ("音辞", "yorushika", Track("jp_fixture", "だから僕は音楽を辞めた", "ヨルシカ")),
            ("極夜", "atarayo", Track("jp_fixture", "極夜", "あたらよ")),
        ):
            source = FakeSource([candidate])
            with tempfile.TemporaryDirectory() as tmp:
                pipe = Pipeline(Settings(remember_hashes=False), sources=[source],
                                cache_path=Path(tmp) / "cache.json", index_path=Path(tmp) / "index.db")
                seed, readings, query, requested = pipe._seed_for(title, artist)
                pool = pipe._collect_pool(seed, readings, query, requested)
                self.assertIn(candidate, pool)
                self.assertGreaterEqual(candidate.score, .34)
                pipe.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
