"""Failed-search integration: weak suggestions never become accepted audio."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic.config import Settings
from mmusic.errors import NetworkError, SearchNoResultError
from mmusic.model import Track
from mmusic.pipeline import Pipeline
from mmusic.search.base import SearchSource


class MetadataSource(SearchSource):
    def __init__(self, tracks=(), *, name="matrix", error=False, no_link=False):
        super().__init__(None)
        self.name = self.label = name
        self.tracks = tracks
        self.error = error
        self.no_link = no_link
        self.queries = []
        self.resolves = 0

    def search(self, query, **kwargs):
        self.queries.append(query)
        if self.error:
            raise NetworkError("模拟网络不可用")
        result = copy.deepcopy(self.tracks)
        for item in result:
            item.source = self.name
        return result

    def resolve(self, track, **kwargs):
        self.resolves += 1
        if self.no_link:
            raise NetworkError("该曲目无免费直链")
        raise AssertionError("Filtered search suggestions must never resolve audio")


class SearchFailureMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="failure_matrix_")
        self.root = Path(self.tmp.name)
        self.pipes = []
        self.logs = []

    def tearDown(self):
        for pipe in self.pipes:
            pipe.close()
        self.tmp.cleanup()

    def pipe(self, sources):
        number = len(self.pipes)
        pipe = Pipeline(Settings(remember_hashes=False, fetch_cover=False,
                                 output_dir=str(self.root / "output"), retries=0),
                        sources=sources, cache_path=self.root / f"c{number}.json",
                        index_path=self.root / f"i{number}.db",
                        on_log=lambda message, level="detail": self.logs.append((message, level)))
        self.pipes.append(pipe)
        return pipe

    def test_failed_search_matrix_list_auto_file_and_batch(self):
        rows = [
            ("Unfamiliar Long Melody", "yorushika", "Unfamiliar Long Melodi", "ヨルシカ"),
            ("Another Different Melody", "Test Artist", "Another Diferent Melody", "Test Artist"),
        ]
        for query, artist, title, performer in rows:
            for mode in ("list", "auto", "batch", "file"):
                with self.subTest(query=query, mode=mode):
                    source = MetadataSource([Track("matrix", title, performer)])
                    pipe = self.pipe([source])
                    with patch.object(pipe, "_download", side_effect=AssertionError("download")), \
                         patch.object(pipe, "_probe_audio", side_effect=AssertionError("probe")):
                        if mode == "batch":
                            results = pipe.acquire_many([(query, artist)])
                            self.assertEqual(results[0].status, "failed")
                            message, suggestions = results[0].message, results[0].suggestions
                        elif mode == "file":
                            src = self.root / f"{artist} - {query}.mgg"
                            result = pipe._fallback_result(src, src.stem, "", note="test")
                            self.assertEqual(result.status, "failed")
                            message, suggestions = result.message, result.suggestions
                        else:
                            with self.assertRaises(SearchNoResultError) as raised:
                                if mode == "list":
                                    pipe.search_candidates_ex(query, artist=artist)
                                else:
                                    pipe.acquire(query, artist=artist)
                            message, suggestions = raised.exception.message, raised.exception.suggestions
                    self.assertIn("你可能想找的歌是：", message)
                    self.assertIn((title, performer), suggestions)
                    self.assertNotIn("是否需要下载", message)
                    self.assertEqual(source.resolves, 0)
                    self.assertLessEqual(len(source.queries), 4)
        self.assertFalse((self.root / "output").exists())

    def test_all_filtered_versions_never_offer_to_download(self):
        for title, album, raw in (
            ("夜曲 (伴奏)", "", {}),
            ("夜曲", "某次 LIVE 演唱会", {}),
            ("夜曲", "", {"song": {"title": "夜曲 (Live)"}}),
        ):
            for entry in ("list", "auto"):
                with self.subTest(title=title, album=album, entry=entry):
                    source = MetadataSource([Track("matrix", title, "周杰伦", album=album, raw=raw)])
                    pipe = self.pipe([source])
                    with self.assertRaises(SearchNoResultError) as error:
                        (pipe.search_candidates_ex if entry == "list" else pipe.acquire)("夜曲子")
                    self.assertNotIn("确认是否", error.exception.message)
                    self.assertNotIn("当前仅有", error.exception.message)
                    self.assertEqual(source.resolves, 0)

    def test_no_evidence_network_errors_wrong_artist_do_not_fabricate_suggestions(self):
        for sources in (
            [], [MetadataSource()], [MetadataSource(error=True)],
            [MetadataSource([Track("matrix", "Unfamiliar Long Melodi", "Wrong Artist")])],
            [MetadataSource([Track("matrix", "Unfamiliar Long Melodi", "群星")])],
            [MetadataSource([Track("matrix", "Unfamiliar Long Melodi", "ヨルシカ",
                                    raw={"query_echo": True})])],
        ):
            with self.subTest(sources=sources):
                pipe = self.pipe(sources)
                with self.assertRaises(SearchNoResultError) as error:
                    pipe.search_candidates_ex("Unfamiliar Long Melody", artist="yorushika")
                self.assertEqual(error.exception.suggestions, ())
                self.assertNotIn("你可能想找", error.exception.message)

    def test_hints_are_per_query_not_carried_into_next_request(self):
        source = MetadataSource([Track("matrix", "Unfamiliar Long Melodi", "ヨルシカ")])
        pipe = self.pipe([source])
        for query, expected in (("Unfamiliar Long Melody", True), ("完全不同的名字", False)):
            with self.assertRaises(SearchNoResultError) as error:
                pipe.search_candidates_ex(query, artist="yorushika")
            self.assertEqual(bool(error.exception.suggestions), expected)

    def test_exact_but_unresolvable_list_keeps_empty_result_contract(self):
        source = MetadataSource([Track("matrix", "夜曲", "周杰伦", duration=240)], no_link=True)
        pipe = self.pipe([source])
        tracks, _reference = pipe.search_candidates_ex("夜曲", artist="周杰伦")
        self.assertEqual(tracks, [])
        self.assertTrue(any("无法解析直链" in message for message, _ in self.logs))

    def test_time_failure_is_not_misreported_as_cover_or_download_question(self):
        track = Track("matrix", "夜曲", "周杰伦", duration=240, actual_duration=120, score=1)
        pipe = self.pipe([MetadataSource()])
        with patch.object(pipe, "_probe_durations"), \
             patch.object(pipe, "_download", side_effect=AssertionError("download")):
            with self.assertRaises(SearchNoResultError) as error:
                pipe._take_from_pool([track], src=None, digest="", note="", label="夜曲")
        self.assertIn("时长或完整度校验", error.exception.message)
        self.assertNotIn("伴奏", error.exception.message)
        self.assertNotIn("是否", error.exception.message)

    def test_job_result_default_suggestions_remain_optional(self):
        from mmusic.pipeline import JobResult
        result = JobResult("search://x", "x", "downloaded")
        self.assertEqual(result.suggestions, ())
        self.assertTrue(result.ok)
        self.assertEqual(SearchNoResultError("none").suggestions, ())

    def test_failure_only_metadata_rescue_never_enters_download_pool(self):
        from mmusic.fuzzy import suggestion_queries

        class RescueSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    return [Track(self.name, "夜曲", "周杰伦")]
                return [Track(self.name, "夜曲作品15号", "郎朗")]

        sources = [RescueSource(name=name) for name in ("migu", "kugou", "aggregator")]
        pipe = self.pipe(sources)
        with self.assertRaises(SearchNoResultError) as error:
            pipe.search_candidates_ex("夜曲子")
        self.assertEqual(error.exception.suggestions, (("夜曲", "周杰伦"),))
        self.assertEqual(sum("夜曲" in s.queries for s in sources), 2)
        self.assertTrue(all(len(s.queries) <= 4 and not s.resolves for s in sources))
        self.assertEqual(suggestion_queries([("夜曲子", "周杰伦")]), ["夜曲 周杰伦"])
        for text in ("夜曲", "夜", "!!!!", "Unrelated strings", "x" * 10000):
            self.assertEqual(suggestion_queries([(text, "")]), [])
        self.assertFalse((self.root / "output").exists())

    def test_optional_http_limits_restore_and_cap_timeout_and_retry(self):
        import io
        from contextlib import contextmanager
        from mmusic.net import HttpClient
        client = HttpClient(timeout=30, retries=3)
        timeouts = []

        class Response(io.BytesIO):
            status, headers = 200, {}

        @contextmanager
        def opening(req, timeout):
            timeouts.append(timeout)
            yield Response(b"{}")

        try:
            with patch.object(client, "_open", side_effect=opening):
                with client.request_limits(timeout=3, retries=0):
                    client.get_json("https://metadata.invalid", timeout=8)
                client.get_json("https://metadata.invalid")
            self.assertEqual(timeouts, [3, 30])
            with self.assertRaises(ValueError):
                with client.request_limits():
                    raise ValueError("test")
            self.assertIsNone(client._request_state.limits)
            with patch.object(client, "_open", side_effect=OSError("offline")) as attempt:
                with client.request_limits(retries=0):
                    with self.assertRaises(NetworkError):
                        client.get_json("https://metadata.invalid", retries=5)
                self.assertEqual(attempt.call_count, 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
