"""Explicit song aliases are identity evidence, never blanket API trust."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures as fx
from mmusic import jp
from mmusic.config import Settings
from mmusic.errors import SearchNoResultError
from mmusic.match import (best_reading_match, filter_title_alias_ambiguity,
                          rank_tracks_multi, recording_version_ok,
                          title_alias_evidence, title_gate_ok, title_match_score)
from mmusic.model import Track
from mmusic.pipeline import Pipeline
from mmusic.search.base import SearchSource
from mmusic.search.query import expand


def accepted(title, artist, track):
    return (recording_version_ok(track)
            and best_reading_match([(title, artist)], track,
                                   min_title=.6, min_artist=.6)[0])


class AliasSource(SearchSource):
    name = "alias_fixture"
    label = "Alias fixture"

    def __init__(self, tracks):
        super().__init__(None)
        self.tracks, self.queries = tracks, []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return copy.deepcopy(self.tracks)

    def resolve(self, track, **kwargs):
        track.url = track.raw["path"]
        return track


class TitleAliasEvidenceTests(unittest.TestCase):
    def test_whitelisted_api_fields_without_curated_mapping(self):
        for key in ("alias", "alia", "aliases", "tns", "transNames", "subtitle"):
            for value in ("卖春", ["卖春"]):
                with self.subTest(key=key, value=value), patch.object(jp, "title_aliases", return_value=()):
                    track = Track("kugou", "春ひさぎ", "ヨルシカ", raw={key: value})
                    self.assertTrue(accepted("卖春", "yorushika", track))
                    self.assertEqual(title_match_score([("卖春", "yorushika")], track), 1)
                    self.assertEqual(track.title, "春ひさぎ")

    def test_kugou_suffix_and_filename_need_bound_title_and_artist(self):
        fields = ({"Suffix": "(卖春)"},
                  {"trans_param": {"songname_suffix": "(卖春)"}},
                  {"FileName": "ヨルシカ - 春ひさぎ (卖春)"})
        with patch.object(jp, "title_aliases", return_value=()):
            for item in fields:
                raw = {"SongName": "春ひさぎ", "OriSongName": "春ひさぎ", **item}
                track = Track("kugou", "春ひさぎ", "ヨルシカ", raw=raw)
                self.assertTrue(accepted("卖春", "yorushika", track), raw)
                raw["SongName"] = raw["OriSongName"] = "又三郎"
                self.assertFalse(accepted("卖春", "yorushika", track), raw)
            track = Track("kugou", "春ひさぎ", "ヨルシカ",
                          raw={"SongName": "春ひさぎ", "FileName": "银杏Kyou - 春ひさぎ (卖春)"})
            self.assertFalse(accepted("卖春", "yorushika", track))

    def test_no_query_echo_lyrics_tags_album_or_artist_alias_evidence(self):
        payloads = ({"highlightStr": ["卖春", "yorushika"]}, {"query": "卖春"},
                    {"lyric": "卖春"}, {"tag": "卖春"}, {"tags": {"alias": "卖春"}},
                    {"album": {"alias": "卖春"}}, {"artist": {"alias": "卖春"}},
                    {"data": {"alias": "卖春"}}, {"search": {"subtitle": "卖春"}})
        with patch.object(jp, "title_aliases", return_value=()):
            for raw in payloads:
                track = Track("migu", "又三郎", "ヨルシカ", raw=raw)
                self.assertEqual(title_alias_evidence(track), (), raw)
                self.assertFalse(accepted("卖春", "yorushika", track), raw)

    def test_nested_song_record_requires_identity_binding(self):
        for name, expected in (("春ひさぎ", True), ("又三郎", False)):
            track = Track("api", "春ひさぎ", "ヨルシカ",
                          raw={"song": {"name": name, "tns": ["卖春"]}})
            with patch.object(jp, "title_aliases", return_value=()):
                self.assertEqual(accepted("卖春", "yorushika", track), expected)
        raw = {}
        raw["song"] = raw
        self.assertEqual(title_alias_evidence(Track("api", "春ひさぎ", raw=raw)), ())

    def test_artist_and_recording_guards_remain_mandatory(self):
        for artist, title, album, raw in (
                ("银杏Kyou", "春ひさぎ", "", {"alias": "卖春"}),
                ("", "春ひさぎ", "", {"alias": "卖春"}),
                ("tayori", "atarayo", "", {"alias": "卖春"}),
                ("ヨルシカ", "春ひさぎ (Cover)", "", {"alias": "卖春"}),
                ("ヨルシカ", "春ひさぎ", "LIVE「月光」", {"alias": "卖春"}),
                ("ヨルシカ", "春ひさぎ", "", {"SongName": "春ひさぎ", "Suffix": "(Live)", "alias": "卖春"}),
                ("ヨルシカ", "春ひさぎ", "", {"SongName": "春ひさぎ", "trans_param": {"songname_suffix": "(Cover)"}, "alias": "卖春"}),
                ("ヨルシカ", "春ひさぎ (未知版本)", "", {"alias": "卖春"})):
            with self.subTest(title=title, artist=artist, album=album):
                self.assertFalse(accepted("卖春", "yorushika", Track("api", title, artist, album, raw=raw)))

    def test_unknown_other_song_not_accepted_just_because_api_returned_it(self):
        for name in ("又三郎", "嘘月", "春ひさぎではない"):
            self.assertFalse(accepted("卖春", "yorushika", Track("api", name, "ヨルシカ")))

    def test_local_fact_table_bidirectional_offline_and_provenance(self):
        for alias, original, performer, source in jp.title_alias_records():
            with self.subTest(alias=alias):
                self.assertTrue(source)
                self.assertTrue(accepted(alias, performer, Track("api", original, performer)))
                self.assertTrue(accepted(original, performer, Track("api", alias, performer)))
                self.assertFalse(accepted(alias, performer, Track("api", original, "OtherPerformer")))
        self.assertIn(("春ひさぎ", "ヨルシカ"), jp.title_aliases("賣春"))
        self.assertTrue(accepted("说吧！", "yorushika", Track("api", "言って。", "ヨルシカ")))
        self.assertTrue(accepted("大概", "YOASOBI", Track("api", "たふ\u3099ん", "YOASOBI")))

    def test_translation_punctuation_and_high_threshold_missing_character(self):
        good = Track("api", "だから僕は音楽を辞めた", "ヨルシカ")
        for value in ("所以我放弃音乐", "所以，我放棄了音樂！", "所以我放弃了音乐"):
            self.assertTrue(accepted(value, "yorushika", good), value)
            first = [variant.text for variant in expand(value + " yorushika", [(value, "yorushika")])[:4]]
            self.assertTrue(any("だから僕は音楽を辞めた" in v for v in first), first)
        for value in ("卖花", "说吗", "所以我喜欢音乐", "所以我放弃所有音乐"):
            self.assertFalse(accepted(value, "yorushika", good), value)
        complex_title = Track("api", "別の原曲", "Ado", raw={"tns": ["你好，世界！"]})
        self.assertTrue(accepted("你好世界", "Ado", complex_title))
        self.assertFalse(accepted("你好世", "Ado", complex_title))

    def test_api_fuzzy_alias_needs_artist_and_rejects_ambiguous_works(self):
        query = "我们一起看星星吧"
        a = Track("api", "原曲甲", "Ado", raw={"transNames": [query + "啊"]})
        b = Track("api", "原曲乙", "Ado", raw={"transNames": [query + "呢"]})
        for track in (a, b):
            self.assertTrue(accepted(query, "Ado", track))
            self.assertFalse(accepted(query, "", track))
        self.assertEqual(filter_title_alias_ambiguity([(query, "Ado")], [a, b]), [])
        self.assertEqual(filter_title_alias_ambiguity([(query, "Ado")], [a]), [a])

    def test_alias_scores_never_manufacture_strong_duration_evidence(self):
        for ext in ("mp3", "flac"):
            track = Track("api", "春ひさぎ", "ヨルシカ", duration=218, ext=ext,
                          raw={"alias": ["卖春"]})
            rank_tracks_multi([("卖春", "yorushika")], [track], duration=218, prefer_ext=ext)
            self.assertLess(track.score, .95)
            self.assertGreater(track.score, .6)

    def test_full_pipeline_list_selected_auto_rematch_and_completeness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "fixture.flac"
            path.write_bytes(fx.make_flac(218))
            good = Track("alias_fixture", "春ひさぎ", "ヨルシカ", "春ひさぎ",
                         duration=218, ext="flac", raw={"path": path.as_uri(), "alias": ["卖春"]})
            bad = Track("alias_fixture", "又三郎", "ヨルシカ", duration=250,
                        raw={"path": path.as_uri(), "highlightStr": ["卖春"]})
            source = AliasSource([bad, good])
            pipe = Pipeline(Settings(output_dir=str(root / "out"), quality="flac",
                                     write_metadata=False, fetch_cover=False,
                                     remember_hashes=False, min_bitrate=0), sources=[source],
                            cache_path=root / "cache.json", index_path=root / "index.db")
            try:
                listed, reference = pipe.search_candidates_ex("卖春", artist="yorushika")
                self.assertEqual([t.title for t in listed], ["春ひさぎ"])
                self.assertEqual(reference, 218)
                self.assertTrue(pipe.download_chosen(listed[0], reference=reference).ok)
                self.assertTrue(pipe.acquire("卖春", artist="yorushika").ok)
                self.assertEqual(Pipeline._rank_rematch_candidates([bad, good],
                                 [("卖春", "yorushika")], [], set()), [good])
                self.assertLessEqual(len(source.queries), 4)
                path.write_bytes(fx.make_flac(120))
                with self.assertRaises(SearchNoResultError):
                    pipe._fetch_chosen(listed[0], reference=218)
            finally:
                pipe.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
