"""Soundtrack credits must not hide the original or admit alternate versions."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmusic.match import (best_reading_match, has_extra_qualifier,
                          title_match_score, track_version_markers)
from mmusic.model import Track
from mmusic.pipeline import Pipeline


class SoundtrackTitleTests(unittest.TestCase):
    def accepts(self, query, title, artist="周杰伦"):
        readings, _, _ = Pipeline._seed_from_name(query)
        track = Track("migu", title, artist, raw={"name": title})
        return (best_reading_match(readings, track, min_title=.6, min_artist=.6)[0]
                and not track_version_markers(track))

    def test_real_migu_title_and_both_input_forms(self):
        title = "飘移(电影《头文字D》主题曲)"
        for query in ("周杰伦 - 飘移", "周杰伦 - " + title):
            with self.subTest(query=query):
                self.assertTrue(self.accepts(query, title))
                self.assertTrue(self.accepts(query, "飘移"))
        self.assertEqual(title_match_score([("飘移", "周杰伦")],
                                          Track("migu", title, "周杰伦")), 1)
        self.assertFalse(has_extra_qualifier("飘移", title))

    def test_credit_syntax_and_multiple_bracket_styles(self):
        for left, right in (("(", ")"), ("（", "）"), ("【", "】"), ("[", "]")):
            for credit in ("电影《头文字D》主题曲", "电视剧《某剧》插曲",
                           "《某片》片尾曲", "动画《某片》片头曲"):
                with self.subTest(credit=credit, brackets=left):
                    self.assertTrue(self.accepts("周杰伦 - 飘移", "飘移" + left + credit + right))

    def test_alternate_versions_and_other_artists_still_fail(self):
        for suffix in ("(Live)", "(R&B)", "(伴奏)", "(Remix)", "(钢琴版)",
                       "(电影《头文字D》主题曲 Live)", "(未知说明)"):
            with self.subTest(suffix=suffix):
                self.assertFalse(self.accepts("周杰伦 - 飘移", "飘移" + suffix))
                self.assertFalse(self.accepts("周杰伦 - 飘移",
                                             "飘移(电影《头文字D》主题曲)" + suffix))
        self.assertFalse(self.accepts("周杰伦 - 飘移", "飘移(电影《头文字D》主题曲)", "其他歌手"))
        self.assertFalse(self.accepts("周杰伦 - 飘移", "一路向北(电影《头文字D》主题曲)"))

    def test_credit_only_is_not_an_empty_title_wildcard(self):
        self.assertFalse(self.accepts("周杰伦 - 飘移", "(电影《头文字D》主题曲)"))
        self.assertFalse(self.accepts("周杰伦 - 飘移", "飘移(电影《头文字D》主题曲)/一路向北"))


if __name__ == "__main__":
    unittest.main()
