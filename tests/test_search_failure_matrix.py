"""Failed-search integration: weak suggestions never become accepted audio."""
from __future__ import annotations

import copy
from itertools import permutations, product
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
        self.pipe_number = 0
        self.logs = []

    def tearDown(self):
        for pipe in self.pipes:
            pipe.close()
        self.tmp.cleanup()

    def pipe(self, sources, **preferences):
        number = self.pipe_number
        self.pipe_number += 1
        settings = dict(remember_hashes=False, fetch_cover=False,
                        output_dir=str(self.root / "output"), retries=0)
        settings.update(preferences)
        pipe = Pipeline(Settings(**settings),
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
                self.assertIn("未形成可靠的检索建议", error.exception.message)

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

    def test_format_fast_cache_and_all_configured_source_orders(self):
        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    if self.name in {"migu", "kugou"}:
                        return [Track(self.name, "夜曲", "周杰伦"),
                                Track(self.name, "夜曲 (Live)", "周杰伦")]
                    return [Track(self.name, "夜曲", "别的歌手")]
                return [Track(self.name, "完全不同的曲名", "甲歌手")]

        # Actual user order is included, together with every permutation.
        for quality, fast, cached, order in product(
                ("mp3", "flac", "wav"), (False, True), (False, True),
                permutations(("kuwo", "kugou", "migu", "aggregator"))):
            with self.subTest(quality=quality, fast=fast, cached=cached, order=order):
                sources = [CatalogSource(name=name) for name in order]
                pipe = self.pipe(sources, quality=quality, fast_mode=fast,
                                 remember_hashes=True)
                if cached:
                    for source in sources:
                        pipe.index.note_search(source.name, "夜曲子", found=False)
                with patch.object(pipe, "_download", side_effect=AssertionError("download")), \
                     patch.object(pipe, "_probe_audio", side_effect=AssertionError("probe")):
                    with self.assertRaises(SearchNoResultError) as error:
                        pipe.search_candidates_ex("夜曲子")
                self.assertEqual(error.exception.suggestions,
                                 (("夜曲", "周杰伦"), ("夜曲 (Live)", "周杰伦")))
                self.assertIn("你可能想找的歌是", error.exception.message)
                self.assertIn("尚未通过下载校验", error.exception.message)
                self.assertEqual([s.name for s in sources if "夜曲" in s.queries],
                                 [name for name in order if name in {"migu", "kugou"}])
                self.assertTrue(all(not s.resolves and len(s.queries) <= 4 for s in sources))
                self.assertEqual(sum(s.queries.count("夜曲") for s in sources), 2)
                self.assertEqual(sum(s.queries.count("夜曲子") for s in sources),
                                 0 if cached else 4)
                # Close each SQLite-backed matrix row promptly.
                pipe.close()
                self.pipes.remove(pipe)
        self.assertFalse((self.root / "output").exists())

    def test_cached_skips_do_not_exhaust_actual_request_budget(self):
        from mmusic.search.query import QueryVariant

        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                return [Track(self.name, "夜曲", "周杰伦")] if query == "夜曲" else []

        variants = [QueryVariant(f"缓存变体{i}", "test") for i in range(4)]
        for cached_count in (0, 1, 3, 4):
            with self.subTest(cached_count=cached_count):
                source = CatalogSource(name="migu")
                pipe = self.pipe([source], remember_hashes=True)
                for variant in variants[:cached_count]:
                    pipe.index.note_search(source.name, variant.text, found=False)
                with patch("mmusic.pipeline.expand_query", return_value=variants):
                    with self.assertRaises(SearchNoResultError) as error:
                        pipe.search_candidates_ex("夜曲子")
                self.assertEqual(bool(error.exception.suggestions), bool(cached_count))
                self.assertEqual(source.queries.count("夜曲"), int(bool(cached_count)))
                self.assertLessEqual(len(source.queries), 4)
                self.assertEqual(source.resolves, 0)

    def test_metadata_priority_fallback_and_cached_rescue_query(self):
        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    return [Track(self.name, "夜曲", "周杰伦")]
                return []

        for missing, blocked, expected in (
                ("migu", "", ("kugou", "kuwo")),
                ("kugou", "", ("migu", "kuwo")),
                ("kuwo", "migu", ("kugou", "aggregator"))):
            with self.subTest(missing=missing, blocked=blocked):
                sources = [CatalogSource(name=name) for name in
                           ("aggregator", "kuwo", "migu", "kugou") if name != missing]
                pipe = self.pipe(sources, quality="flac", remember_hashes=True)
                if blocked:
                    pipe.index.note_search(blocked, "夜曲", found=False)
                with self.assertRaises(SearchNoResultError) as error:
                    pipe.search_candidates_ex("夜曲子")
                self.assertEqual(error.exception.suggestions, (("夜曲", "周杰伦"),))
                self.assertEqual({s.name for s in sources if "夜曲" in s.queries}, set(expected))
                self.assertEqual(sum(s.queries.count("夜曲") for s in sources), 2)

    def test_single_available_catalog_retains_version_and_tied_artist_choices(self):
        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    return [Track(self.name, "夜曲", "周杰伦", album="Live at Somewhere"),
                            Track(self.name, "夜曲", "另一歌手")]
                return []

        sources = [MetadataSource(name="aggregator", error=True), CatalogSource(name="migu")]
        pipe = self.pipe(sources, quality="flac")
        with self.assertRaises(SearchNoResultError) as error:
            pipe.search_candidates_ex("夜曲子")
        self.assertEqual(error.exception.suggestions,
                         (("夜曲 (Live)", "周杰伦"), ("夜曲", "另一歌手")))
        self.assertTrue(all(not s.resolves for s in sources))

    def test_rescue_is_bounded_even_if_catalog_ignores_limit(self):
        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    return [Track(self.name, "无关的歌曲", "歌手")] * self.padding + [
                        Track(self.name, "夜曲", "周杰伦")]
                # Fill the ordinary record budget before the failure rescue.
                return [Track(self.name, "无关的歌曲", "歌手")] * 256

        for padding in (0, 8):
            with self.subTest(padding=padding):
                sources = [CatalogSource(name="migu"), CatalogSource(name="kugou")]
                for source in sources:
                    source.padding = padding
                pipe = self.pipe(sources)
                with self.assertRaises(SearchNoResultError) as error:
                    pipe.search_candidates_ex("夜曲子")
                self.assertEqual(error.exception.suggestions,
                                 () if padding else (("夜曲", "周杰伦"),))
                if padding:
                    self.assertIn("未形成可靠的检索建议", error.exception.message)

    def test_rescue_limits_restore_after_failure_and_cancellation(self):
        from mmusic.errors import CancelledError
        from mmusic.net import HttpClient

        class CatalogSource(MetadataSource):
            def search(self, query, **kwargs):
                self.queries.append(query)
                if query == "夜曲":
                    self.limits = self.http._request_state.limits
                    self.kwargs = kwargs
                    raise self.problem
                return []

        for problem in (NetworkError("offline"), ValueError("bad record"), CancelledError("已取消")):
            source = CatalogSource(name="migu")
            pipe = self.pipe([source])
            source.http = pipe.http
            source.problem = problem
            with self.assertRaises(CancelledError if isinstance(problem, CancelledError)
                                   else SearchNoResultError):
                pipe.search_candidates_ex("夜曲子")
            self.assertIsInstance(source.http, HttpClient)
            self.assertEqual(source.limits, (3.0, 0))
            self.assertEqual(source.kwargs["limit"], 8)
            self.assertIsNone(source.http._request_state.limits)
            self.assertEqual(source.queries.count("夜曲"), 1)
            self.assertEqual(source.resolves, 0)

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
