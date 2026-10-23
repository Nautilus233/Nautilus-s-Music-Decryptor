"""Independent album context allows bounded typos, never album-wide matching."""
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic.model import Track
from mmusic.config import Settings
from mmusic.pipeline import Pipeline
from mmusic.search.base import SearchSource


class AlbumContextTests(unittest.TestCase):
    title = "这是一个很长很长的原版歌曲名字"
    typo = "这是一个很长很长原版歌曲名字"

    def track(self, title=None, **values):
        kwargs = dict(artist="测试歌手", album="原版专辑", duration=245)
        kwargs.update(values)
        return Track("migu", title or self.typo, **kwargs)

    def select(self, tracks, **context):
        return Pipeline._rank_rematch_candidates(
            tracks, [(self.title, "测试歌手")], [], set(), **context)

    def test_only_independent_complete_context_enables_tolerance(self):
        track = self.track()
        self.assertEqual(self.select([track]), [])
        self.assertEqual(self.select([track], expected_album="原版专辑"), [])
        self.assertEqual(self.select([track], expected_album="原版专辑",
                                     reference_duration=245), [track])

    def test_missing_wrong_or_invalid_evidence_does_not_relax_title(self):
        for changes in (dict(artist="其他歌手"), dict(artist="未知歌手"),
                        dict(artist=""), dict(album="其他专辑"), dict(album=""),
                        dict(duration=0), dict(duration=230), dict(duration=-245),
                        dict(duration=float("nan")), dict(duration=float("inf"))):
            with self.subTest(changes=changes):
                self.assertEqual(self.select([self.track(**changes)],
                                             expected_album="原版专辑", reference_duration=245), [])

    def test_same_album_other_song_short_titles_and_versions_fail(self):
        for title in ("这是完全不同的另一个原版歌曲名字", self.typo + "(Live)",
                      self.typo + "(R&B)", self.typo + "(未知说明)", self.typo + "(伴奏)"):
            with self.subTest(title=title):
                self.assertEqual(self.select([self.track(title)],
                                             expected_album="原版专辑", reference_duration=245), [])
        self.assertEqual(Pipeline._rank_rematch_candidates(
            [self.track("雨天")], [("晴天", "测试歌手")], [], set(),
            expected_album="原版专辑", reference_duration=245), [])

    def test_ambiguous_approximate_titles_are_not_chosen(self):
        tracks = [self.track(), self.track("这是一个很长很长的原版歌名字")]
        self.assertEqual(self.select(tracks, expected_album="原版专辑",
                                     reference_duration=245), [])
        exact = self.track(self.title)
        self.assertEqual(self.select(tracks + [exact], expected_album="原版专辑",
                                     reference_duration=245), [exact])

    def test_existing_cross_album_exact_title_and_metadata_are_preserved(self):
        exact = self.track(self.title, album="另一张精选集", ext="flac")
        self.assertEqual(self.select([exact], expected_album="原版专辑",
                                     reference_duration=245), [exact])
        self.assertEqual(exact.album, "另一张精选集")
        self.assertEqual(exact.title, self.title)

    def test_album_rematch_passes_reference_before_output_metadata_rewrite(self):
        candidate = self.track()

        class Source(SearchSource):
            name = "migu"
            label = "fixture"

            def search(self, query, **kwargs):
                return [candidate]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipe = Pipeline(Settings(remember_hashes=False), sources=[Source(None)],
                            cache_path=root / "cache.json", index_path=root / "index.db")
            try:
                reference = Track(
                    "aggregator", self.title, "测试歌手", album="原版专辑", duration=245)
                self.assertEqual(list(pipe._rematch_album_track(reference)), [candidate])
                candidate.album = "错误专辑"
                self.assertEqual(list(pipe._rematch_album_track(reference)), [])
                self.assertEqual(candidate.album, "错误专辑")
            finally:
                pipe.close()


if __name__ == "__main__":
    unittest.main()
