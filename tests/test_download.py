import functools
import http.server
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from igdl.download import DownloadOptions, Downloader, safe_name
from igdl.media import PHOTO, VIDEO, MediaItem, Post, Variant
from igdl.net import HttpClient

TAKEN_AT = 1700000000
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.served = tempfile.TemporaryDirectory()
        root = Path(cls.served.name)
        (root / "big.jpg").write_bytes(b"\xff\xd8 big photo")
        (root / "small.jpg").write_bytes(b"\xff\xd8 small photo")
        (root / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 progressive video")
        handler = functools.partial(QuietHandler, directory=str(root))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.served.cleanup()

    def setUp(self):
        self.out = tempfile.TemporaryDirectory()
        self.lines = []
        self.downloader = Downloader(HttpClient(), self.out.name, ffmpeg=None, out=self.lines.append,
                                     show_progress=False)

    def tearDown(self):
        self.out.cleanup()

    def photo(self, position=1):
        return MediaItem(PHOTO, [Variant(f"{self.base}/big.jpg", 1080, 1350),
                                 Variant(f"{self.base}/small.jpg", 640, 800)], position, f"pk{position}", TAKEN_AT)

    def video(self, position=2):
        return MediaItem(VIDEO, [Variant(f"{self.base}/clip.mp4", 720, 1280)], position, f"pk{position}", TAKEN_AT)

    def post(self, *items, kind="post"):
        return Post(kind, "some.user", list(items), shortcode="Cabc123XYZ_", taken_at=TAKEN_AT, caption="Hi!",
                    title="My trip", reel_id="1791")

    def root(self):
        return Path(self.out.name)

    def test_carousel_names_dates_and_skip_existing(self):
        result = self.downloader.download(self.post(self.photo(1), self.video(2)), DownloadOptions())
        date = datetime.fromtimestamp(TAKEN_AT).strftime("%Y-%m-%d")
        photo_file = self.root() / f"{date}_some.user_Cabc123XYZ__1.jpg"
        video_file = self.root() / f"{date}_some.user_Cabc123XYZ__2.mp4"
        self.assertEqual(result.saved, [photo_file, video_file])
        self.assertEqual(photo_file.read_bytes(), b"\xff\xd8 big photo")
        self.assertEqual(int(photo_file.stat().st_mtime), TAKEN_AT)
        self.assertEqual(sorted(p.name for p in self.root().iterdir()), [photo_file.name, video_file.name])

        again = self.downloader.download(self.post(self.photo(1), self.video(2)), DownloadOptions())
        self.assertEqual((again.saved, again.existing), ([], [photo_file, video_file]))

    def test_single_item_has_no_position_suffix(self):
        result = self.downloader.download(self.post(self.photo(1)), DownloadOptions())
        date = datetime.fromtimestamp(TAKEN_AT).strftime("%Y-%m-%d")
        self.assertEqual(result.saved[0].name, f"{date}_some.user_Cabc123XYZ_.jpg")

    def test_media_filter(self):
        videos = self.downloader.download(self.post(self.photo(1), self.video(2)), DownloadOptions("videos"))
        self.assertEqual(([p.suffix for p in videos.saved], videos.filtered), ([".mp4"], 1))
        photos = self.downloader.download(self.post(self.photo(1), self.video(2)), DownloadOptions("photos"))
        self.assertEqual(([p.suffix for p in photos.saved], photos.filtered), ([".jpg"], 1))

    def test_resolution_limit_adds_the_size_to_the_name(self):
        result = self.downloader.download(self.post(self.photo(1)), DownloadOptions(quality="700"))
        self.assertTrue(result.saved[0].name.endswith("_640x800.jpg"))
        self.assertEqual(result.saved[0].read_bytes(), b"\xff\xd8 small photo")

    def test_ask_mode_uses_the_chooser(self):
        asked = []

        def chooser(post, kind, items, can_merge):
            asked.append((kind, len(items)))
            return items[0].variants[1]

        self.downloader.chooser = chooser
        result = self.downloader.download(self.post(self.photo(1)), DownloadOptions(quality="ask"))
        self.assertEqual(asked, [(PHOTO, 1)])
        self.assertTrue(result.saved[0].name.endswith("_640x800.jpg"))

    def test_stories_and_highlights_also_go_in_the_main_folder(self):
        story = self.downloader.download(self.post(self.photo(1), kind="story"), DownloadOptions())
        stamp = datetime.fromtimestamp(TAKEN_AT).strftime("%Y-%m-%d_%H-%M-%S")
        self.assertEqual(story.saved[0], self.root() / f"{stamp}_some.user_story_pk1.jpg")
        highlight = self.downloader.download(self.post(self.photo(1), kind="highlight"), DownloadOptions())
        self.assertEqual(highlight.saved[0], self.root() / f"{stamp}_some.user_highlight_My trip_pk1.jpg")

    def test_caption_file(self):
        self.downloader.download(self.post(self.photo(1)), DownloadOptions(save_captions=True))
        date = datetime.fromtimestamp(TAKEN_AT).strftime("%Y-%m-%d")
        caption = (self.root() / f"{date}_some.user_Cabc123XYZ_.txt").read_text(encoding="utf-8")
        self.assertIn("https://www.instagram.com/p/Cabc123XYZ_/", caption)
        self.assertIn("Hi!", caption)

    def test_missing_file_is_reported_not_raised(self):
        item = MediaItem(PHOTO, [Variant(f"{self.base}/missing.jpg", 1080, 1350)], 1, "pk1", TAKEN_AT)
        result = self.downloader.download(self.post(item), DownloadOptions())
        self.assertEqual(result.saved, [])
        self.assertEqual(len(result.failed), 1)
        self.assertIn("HTTP 404", result.failed[0])

    def test_video_without_files(self):
        result = self.downloader.download(self.post(MediaItem(VIDEO, [], 1)), DownloadOptions())
        self.assertEqual(len(result.failed), 1)

    def test_stream_needing_merge_without_ffmpeg_saves_video_only(self):
        stream = Variant(f"{self.base}/clip.mp4", 1080, 1920, bandwidth=4_000_000, dash=True,
                         audio_url=f"{self.base}/audio.mp4")
        result = self.downloader.download(self.post(MediaItem(VIDEO, [stream], 1, "pk1", TAKEN_AT)),
                                          DownloadOptions())
        self.assertEqual(len(result.saved), 1)
        self.assertTrue(any("no sound" in line for line in self.lines))

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
    def test_dash_video_and_audio_are_merged(self):
        root = Path(self.served.name)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                        "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "v.mp4")], check=True)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                        "-vn", "-c:a", "aac", str(root / "a.mp4")], check=True)
        stream = Variant(f"{self.base}/v.mp4", 320, 240, bandwidth=100_000, dash=True,
                         audio_url=f"{self.base}/a.mp4")
        self.downloader.ffmpeg = FFMPEG
        result = self.downloader.download(self.post(MediaItem(VIDEO, [stream], 1, "pk1", TAKEN_AT)),
                                          DownloadOptions())
        self.assertEqual(len(result.saved), 1, result.failed)
        probe = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                str(result.saved[0])], capture_output=True, text=True, check=True)
        self.assertEqual(sorted(probe.stdout.split()), ["audio", "video"])
        self.assertEqual(sorted(p.name for p in result.saved[0].parent.iterdir()), [result.saved[0].name])


class SafeNameTests(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(safe_name('a<b>c:"d/e\\f|g?h*'), "a_b_c__d_e_f_g_h_")
        self.assertEqual(safe_name("  trip.  "), "trip")
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name("..."), "untitled")
        self.assertEqual(safe_name("Viaje 🌴 2024"), "Viaje 🌴 2024")


if __name__ == "__main__":
    unittest.main()
