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


if __name__ == "__main__":
    unittest.main()
