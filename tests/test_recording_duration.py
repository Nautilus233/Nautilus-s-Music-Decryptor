"""Recording identity must precede duration reference and output tag rewriting."""
from __future__ import annotations

import copy
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures as fx
from mmusic.config import Settings
from mmusic.errors import SearchNoResultError
from mmusic.duration import audio_duration
from mmusic.match import album_version_markers, recording_version_ok, track_version_markers
from mmusic.model import Track
from mmusic.pipeline import Pipeline, _recording_tags
from mmusic.search.base import SearchSource

TITLE = "だから僕は音楽を辞めた"
ARTIST = "ヨルシカ"
LIVE = "ヨルシカ LIVE「月光」"


def syncsafe(n):
    return bytes((n >> shift) & 127 for shift in (21, 14, 7, 0))


def tagged_mp3(seconds, album, version=3, encoding=3):
    value = bytes([encoding]) + album.encode(("latin-1", "utf-16", "utf-16-be", "utf-8")[encoding])
    name = b"TAL" if version == 2 else b"TALB"
    size = (len(value).to_bytes(3, "big") if version == 2 else
            syncsafe(len(value)) if version == 4 else len(value).to_bytes(4, "big"))
    frame = name + size + (b"" if version == 2 else b"\x00\x00") + value
    return b"ID3" + bytes([version, 0, 0]) + syncsafe(len(frame)) + frame + fx.make_mp3_xing(seconds)


def vorbis_comment(album):
    value = ("ALBUM=" + album).encode()
    return b"\x00" * 4 + (1).to_bytes(4, "little") + len(value).to_bytes(4, "little") + value


class Source(SearchSource):
    def __init__(self, name, tracks, resolved_album=None):
        super().__init__(None)
        self.name = self.label = name
        self.tracks, self.resolved_album = tracks, resolved_album

    def search(self, query, **kwargs):
        return copy.deepcopy(self.tracks)

    def resolve(self, track, **kwargs):
        track.url = track.raw["path"]
        if self.resolved_album is not None:
            track.album = self.resolved_album
        return track


class RecordingDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipes = []

    def tearDown(self):
        for pipe in self.pipes:
            pipe.close()
        self.temp.cleanup()

    def track(self, source="kugou", duration=242, album=TITLE, seconds=243,
              embedded=None, ext="mp3"):
        path = self.root / f"{source}-{len(list(self.root.iterdir()))}.{ext}"
        data = (fx.make_flac(seconds) if ext == "flac" else
                tagged_mp3(seconds, embedded) if embedded is not None else fx.make_mp3_xing(seconds))
        path.write_bytes(data)
        return Track(source, TITLE, ARTIST, album, duration=duration, ext=ext,
                     bitrate=128, raw={"path": path.as_uri()})

    def pipe(self, *sources, quality="mp3"):
        logs = []
        pipe = Pipeline(Settings(output_dir=str(self.root / "out"), quality=quality,
                                 write_metadata=False, fetch_cover=False, remember_hashes=False,
                                 min_bitrate=0),
                        cache_path=self.root / "cache.json", index_path=self.root / "index.db",
                        sources=list(sources), on_log=logs.append)
        self.pipes.append(pipe)
        return pipe, logs

    def test_album_context_and_raw_field_matrix(self):
        for album in (LIVE, "LIVE『月光』", "Live at Wembley", "Live 2024", "Live Vol.2",
                      "Live版本", "Live 版", "Example (Live Ver.)",
                      "ライブ「月光」", "ライヴ盤", "现场", "現場錄音", "巡回演唱会"):
            for raw in ({}, {"AlbumName": album}, {"album_name": album},
                        {"album": {"name": album}}, {"albums": [{"albumName": album}]},
                        {"data": {"song": {"album": {"title": album}}}},
                        {"metadata": {"album": [album]}}):
                with self.subTest(album=album, raw=raw):
                    track = Track("test", TITLE, ARTIST, album if not raw else TITLE, raw=raw)
                    self.assertFalse(recording_version_ok(track))
                    self.assertTrue(recording_version_ok(track, {"live"}))
        for album in ("Alive", "Live Through This", "Live and Let Die", "Love Live!",
                      "Delivery", "电影版原声带", "ライブのあとで", "Concert for Peace"):
            with self.subTest(album=album):
                self.assertEqual(album_version_markers(album), set())
                self.assertTrue(recording_version_ok(Track("test", TITLE, ARTIST, album)))
        self.assertEqual(track_version_markers(Track("test", "晴天 (KTV版伴奏)")), {"伴奏"})

    def test_studio_list_reference_242_not_live_255_and_chosen_243(self):
        studio = self.track()
        live = self.track(duration=255, album=LIVE, seconds=255)
        flac = self.track("aggregator", duration=0, seconds=243, ext="flac")
        pipe, _ = self.pipe(Source("kugou", [studio, live]), Source("aggregator", [flac]))
        listed, reference = pipe.search_candidates_ex("音辞", artist="yorushika")
        self.assertEqual(reference, 242)
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(t.album != LIVE for t in listed))
        chosen = next(t for t in listed if t.source == "aggregator")
        fetched, _, _ = pipe._fetch_chosen(chosen, reference=reference)
        self.assertAlmostEqual(fetched.actual_duration, 243, places=1)

    def test_only_live_never_supplies_original_reference(self):
        live = self.track(duration=255, album=LIVE, seconds=255)
        pipe, _ = self.pipe(Source("kugou", [live]))
        with self.assertRaises(SearchNoResultError):
            pipe.search_candidates_ex(TITLE, artist=ARTIST)
        with self.assertRaises(SearchNoResultError):
            pipe._take_from_pool([live], src=None, digest="", note="", label=TITLE)

    def test_explicit_live_keeps_only_live_and_own_reference(self):
        studio = self.track()
        live = self.track(duration=255, album=LIVE, seconds=255, embedded=LIVE)
        pipe, _ = self.pipe(Source("kugou", [studio, live]))
        listed, reference = pipe.search_candidates_ex(TITLE + " (Live)", artist=ARTIST)
        self.assertEqual(reference, 255)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].album, LIVE)
        self.assertTrue(pipe.download_chosen(listed[0], reference=reference).ok)

    def test_resolution_cannot_change_recording_in_list_or_chosen(self):
        studio = self.track()
        pipe, _ = self.pipe(Source("kugou", [studio], resolved_album=LIVE))
        listed, reference = pipe.search_candidates_ex(TITLE, artist=ARTIST)
        self.assertEqual(listed, [])
        self.assertIsNone(reference)
        pipe._bind_recording(studio, ())
        with self.assertRaisesRegex(SearchNoResultError, "录音版本不符"):
            pipe._fetch_chosen(studio, reference=242)

    def test_late_resolve_live_cannot_pollute_remaining_studio_reference(self):
        studio = self.track("studio")
        hidden = self.track("hidden", duration=255, seconds=255)
        pipe, _ = self.pipe(Source("studio", [studio]),
                            Source("hidden", [hidden], resolved_album=LIVE))
        listed, reference = pipe.search_candidates_ex(TITLE, artist=ARTIST)
        self.assertEqual(reference, 242)
        self.assertEqual([t.source for t in listed], ["studio"])

    def test_unavailable_but_compatible_reference_is_retained(self):
        class Unavailable(Source):
            def resolve(self, track, **kwargs):
                raise SearchNoResultError("fixture unavailable")

        full = self.track("full", duration=235, seconds=235)
        cut = self.track("cut", duration=142, seconds=142)
        pipe, _ = self.pipe(Unavailable("full", [full]), Source("cut", [cut]))
        listed, reference = pipe.search_candidates_ex(TITLE, artist=ARTIST)
        self.assertEqual(reference, 235)
        self.assertEqual([t.source for t in listed], ["cut"])
        with self.assertRaises(SearchNoResultError):
            pipe.download_chosen(listed[0], reference=reference)

    def test_resolver_new_object_is_rechecked_before_auto_reference(self):
        class Replacement(Source):
            def resolve(self, track, **kwargs):
                result = copy.deepcopy(track)
                result.album, result.url = LIVE, result.raw["path"]
                return result

        hidden = self.track("hidden", duration=255, seconds=255)
        studio = self.track("studio")
        pipe, _ = self.pipe(Replacement("hidden", [hidden]), Source("studio", [studio]))
        result = pipe.acquire(TITLE, artist=ARTIST)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(audio_duration(Path(result.out_path).read_bytes()), 243, places=1)

    def test_hidden_live_bytes_rejected_even_within_duration_tolerance(self):
        studio_api_live_bytes = self.track(seconds=243, embedded=LIVE)
        pipe, _ = self.pipe(Source("kugou", [studio_api_live_bytes]))
        with self.assertRaisesRegex(SearchNoResultError, "录音版本不符"):
            pipe.acquire(TITLE, artist=ARTIST, chosen=studio_api_live_bytes, reference=242)
        self.assertFalse((self.root / "out").exists())

    def test_probe_tags_exclude_hidden_live_from_auto_reference(self):
        hidden = self.track("first", duration=255, seconds=255, embedded=LIVE)
        studio = self.track("second", duration=242, seconds=243)
        pipe, logs = self.pipe(Source("first", [hidden]), Source("second", [studio]))
        result = pipe.acquire(TITLE, artist=ARTIST)
        self.assertTrue(result.ok)
        self.assertEqual(Path(result.out_path).read_bytes(), pipe._local_path(studio.raw["path"]).read_bytes())
        self.assertTrue(any("参考时长 4:02" in line for line in logs))

    def test_auto_download_guard_if_probe_tags_unavailable(self):
        hidden = self.track("first", duration=242, seconds=243, embedded=LIVE)
        studio = self.track("second", duration=242, seconds=243)
        pipe, _ = self.pipe(Source("first", [hidden]), Source("second", [studio]))
        with patch.object(pipe, "_probe_audio", return_value=(243, 128)):
            result = pipe.acquire(TITLE, artist=ARTIST)
        self.assertTrue(result.ok)
        self.assertNotIn(b"TALB", Path(result.out_path).read_bytes())

    def test_rematch_filters_recordings_for_both_studio_and_live(self):
        studio = self.track()
        live = self.track(album=LIVE, duration=255, seconds=255)
        args = ([(TITLE, ARTIST)], [])
        self.assertEqual(Pipeline._rank_rematch_candidates([live, studio], *args, set()), [studio])
        self.assertEqual(Pipeline._rank_rematch_candidates([live, studio], *args, {"live"}), [live])

    def test_album_source_evidence_is_not_erased_by_target_metadata(self):
        live = self.track(album=LIVE, duration=242, seconds=243)
        pipe, _ = self.pipe(Source("kugou", [live]))
        with self.assertRaises(SearchNoResultError):
            pipe.download_album_track(live, album=TITLE, reference=242)
        self.assertEqual(live.album, LIVE)
        self.assertFalse((self.root / "out").exists())

    def test_album_bytes_version_checked_before_output_tags(self):
        studio_api_live_bytes = self.track(seconds=243, embedded=LIVE)
        pipe, _ = self.pipe(Source("kugou", [studio_api_live_bytes]))
        with self.assertRaises(SearchNoResultError):
            pipe.download_album_track(studio_api_live_bytes, album=TITLE, reference=242)
        self.assertEqual(studio_api_live_bytes.raw["_recording_tags"]["album"], LIVE)
        self.assertFalse((self.root / "out").exists())

    def test_live_album_without_live_track_title_remains_supported(self):
        live = self.track(album=LIVE, duration=255, seconds=255, embedded=LIVE)
        pipe, _ = self.pipe(Source("kugou", [live]))
        self.assertTrue(pipe.download_album_track(live, album=LIVE, reference=255).ok)

    def test_authoritative_declared_reference_not_overridden_by_longer_bytes(self):
        studio = self.track("first", duration=242, seconds=243)
        longer = self.track("second", duration=242, seconds=255)
        pipe, logs = self.pipe(Source("first", [studio]), Source("second", [longer]))
        self.assertTrue(pipe.acquire(TITLE, artist=ARTIST).ok)
        self.assertTrue(any("参考时长 4:02" in line and "不以最长" in line for line in logs))
        self.assertFalse(any("参考时长 4:15" in line for line in logs))

    def test_short_clips_and_longest_movie_gate_remain(self):
        short = self.track("first", duration=142, seconds=142)
        full = self.track("second", duration=235, seconds=235)
        pipe, _ = self.pipe(Source("first", [short]), Source("second", [full]))
        result = pipe.acquire(TITLE, artist=ARTIST)
        self.assertTrue(result.ok)
        data = Path(result.out_path).read_bytes()
        self.assertAlmostEqual(audio_duration(data, len(data)), 235, places=1)
        for seconds in (11, 200):
            candidate = self.track("second", duration=228, seconds=seconds)
            with self.assertRaises(SearchNoResultError):
                pipe._fetch_chosen(candidate, reference=228)

    def test_tag_parser_both_scripts_id3_versions_flac_ogg_no_mutagen(self):
        with patch.dict(sys.modules, {"mutagen": None}):
            for version in (2, 3, 4):
                for enc in (0, 1, 2, 3):
                    album = "Artist LIVE (Concert)" if enc == 0 else LIVE
                    with self.subTest(version=version, encoding=enc):
                        self.assertEqual(_recording_tags(tagged_mp3(243, album, version, enc))["album"], album)
            block = vorbis_comment(LIVE)
            flac = fx.make_flac(243, pad=0) + b"\x84" + len(block).to_bytes(3, "big") + block
            self.assertEqual(_recording_tags(flac)["album"], LIVE)
            packet = b"\x03vorbis" + block
            page = b"OggS" + bytes(22) + b"\x01" + bytes([len(packet)]) + packet
            self.assertEqual(_recording_tags(page)["album"], LIVE)

    def test_tag_parser_malformed_and_bounded(self):
        rng = random.Random(42028)
        for prefix in (b"ID3", b"fLaC", b"OggS"):
            for _ in range(200):
                value = prefix + rng.randbytes(rng.randrange(400))
                self.assertIsInstance(_recording_tags(value), dict)
        for count in (0, 5, 10, 20):
            self.assertIsInstance(_recording_tags(tagged_mp3(243, LIVE)[:count]), dict)
        # An audio body containing tag-like strings must never be interpreted as tags.
        self.assertEqual(_recording_tags(fx.make_mp3_xing(243) + b"ALBUM=LIVE"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
