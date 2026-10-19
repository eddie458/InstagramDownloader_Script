import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from igdl import cli
from igdl.api import NotFound, RateLimited
from igdl.media import post_from_media
from igdl.settings import Settings

from tests import samples


class HelperTests(unittest.TestCase):
    def test_collect_links_from_lines_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "links.txt"
            txt.write_text("https://www.instagram.com/p/AAAAA11111/\n\nhttps://www.instagram.com/reel/BBBBB22222/\n",
                           encoding="utf-8")
            csv_file = Path(tmp) / "saved.csv"
            csv_file.write_text("url,caption\nhttps://www.instagram.com/p/CCCCC33333/,follow instagram.com/someone\n",
                                encoding="utf-8")
            urls, ignored = cli.collect_links([
                "https://www.instagram.com/p/AAAAA11111/ https://www.instagram.com/p/DDDDD44444/",
                f'"{txt}"',
                str(csv_file),
                "hello there",
            ])
        self.assertEqual(urls, [
            "https://www.instagram.com/p/AAAAA11111/",
            "https://www.instagram.com/p/DDDDD44444/",
            "https://www.instagram.com/reel/BBBBB22222/",
            "https://www.instagram.com/p/CCCCC33333/",
        ])
        self.assertEqual(ignored, ["hello there"])

    def test_parse_selection(self):
        self.assertEqual(cli.parse_selection("1,3, 5-7", 10), [0, 2, 4, 5, 6])
        self.assertEqual(cli.parse_selection("2 2 1", 3), [1, 0])
        self.assertIsNone(cli.parse_selection("0", 3))
        self.assertIsNone(cli.parse_selection("4", 3))
        self.assertIsNone(cli.parse_selection("a", 3))

    def test_saved_row(self):
        row = cli.saved_row(samples.reel_media())
        self.assertEqual((row["url"], row["type"], row["items"], row["username"]),
                         ("https://www.instagram.com/reel/Creel456ABC/", "reel", "1", "someone"))
        row = cli.saved_row(samples.carousel_media())
        self.assertEqual((row["url"], row["type"], row["items"], row["caption"]),
                         ("https://www.instagram.com/p/Cabc123XYZ_/", "carousel", "2", "Hello world"))

    def test_choose_quality_single_item(self):
        video = post_from_media(samples.reel_media()).items[0]
        with mock.patch("builtins.input", side_effect=["3"]), redirect_stdout(io.StringIO()):
            choice = cli.choose_quality(None, "video", [video], can_merge=True)
        self.assertEqual(choice, video.variants[2])

    def test_choose_quality_without_ffmpeg_hides_streams_that_need_merging(self):
        video = post_from_media(samples.reel_media()).items[0]
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=[""]), redirect_stdout(output):
            choice = cli.choose_quality(None, "video", [video], can_merge=False)
        self.assertEqual(choice.resolution, "720x1280")
        self.assertNotIn("1080x1920", output.getvalue())

    def test_choose_quality_for_a_carousel_returns_a_limit(self):
        post = post_from_media(samples.carousel_media())
        photos = [post.items[0], post_from_media(samples.carousel_media()).items[0]]
        with mock.patch("builtins.input", side_effect=["2"]), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.choose_quality(post, "photo", photos, can_merge=True), 1080)


class FakeAPI:
    logged_in = False

    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.requested = []

    def media(self, shortcode):
        self.requested.append(shortcode)
        if shortcode in self.fail_on:
            raise self.fail_on[shortcode]
        media = samples.carousel_media()
        media["code"] = shortcode
        media["carousel_media"] = []  # nothing to download, keeps the test offline
        return media


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"IGDL_HOME": self.tmp.name})
        self.env.start()
        self.app = cli.App(Settings(output_dir=self.tmp.name), interactive=False)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_batch(self, urls, fail_on):
        self.app.api = FakeAPI(fail_on)
        output = io.StringIO()
        with redirect_stdout(output):
            self.app.download_links(urls, cli.DownloadOptions())
        return output.getvalue()

    def test_errors_on_one_link_dont_stop_the_others(self):
        urls = [f"https://www.instagram.com/p/{code}/" for code in ("AAAAA11111", "BBBBB22222", "CCCCC33333")]
        output = self.run_batch(urls, {"BBBBB22222": NotFound("Post not found.")})
        self.assertEqual(self.app.api.requested, ["AAAAA11111", "BBBBB22222", "CCCCC33333"])
        self.assertIn("1 with problems", output)
        saved = list(Path(self.tmp.name).glob("failed_links_*.txt"))
        self.assertEqual(saved[0].read_text(encoding="utf-8").split(), [urls[1]])

    def test_rate_limit_stops_the_batch(self):
        urls = [f"https://www.instagram.com/p/{code}/" for code in ("AAAAA11111", "BBBBB22222", "CCCCC33333")]
        output = self.run_batch(urls, {"BBBBB22222": RateLimited("slow down")})
        self.assertEqual(self.app.api.requested, ["AAAAA11111", "BBBBB22222"])
        self.assertIn("not attempted", output)
        saved = list(Path(self.tmp.name).glob("failed_links_*.txt"))
        self.assertEqual(saved[0].read_text(encoding="utf-8").split(), urls[1:])

    def test_unsupported_link_is_not_saved_for_retrying(self):
        output = self.run_batch(["https://www.instagram.com/natgeo/", "https://example.com/p/AAAAA11111/"], {})
        self.assertIn("Profile links aren't supported", output)
        self.assertIn("Not a supported Instagram link", output)
        self.assertEqual(list(Path(self.tmp.name).glob("failed_links_*.txt")), [])

    def test_stories_need_login(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(cli.LoginRequired):
            self.app.download_stories(["someone"], cli.DownloadOptions())


class LoggedInAPI:
    """Stands in for InstagramAPI with the response shapes of a logged-in session."""

    logged_in = True

    def __init__(self):
        self.reel_requests = []

    def user_id(self, username):
        return {"someone": "99"}[username]

    def reels(self, reel_ids):
        self.reel_requests.append(list(reel_ids))
        reels = []
        for reel_id in reel_ids:
            reel = samples.story_reel()
            if reel_id.startswith("highlight:"):
                reel.update(id=reel_id, title=f"Title {reel_id[10:]}")
            else:
                reel["id"] = reel_id
            reels.append(reel)
        return reels

    def media_by_id(self, media_id):
        raise NotFound("gone")

    def highlights(self, user_id):
        return [{"id": f"highlight:{n}", "title": f"Title {n}", "media_count": 2} for n in range(1, 8)]

    def stories_tray(self):
        return [{"id": 99, "user": {"username": "someone"}}, {"id": 98, "user": {"username": "other"}}]

    def saved_media(self, collection_id=None):
        yield samples.carousel_media()
        yield samples.reel_media()

    def media(self, shortcode):
        raise AssertionError("saved posts already contain everything needed")


class RecordingDownloader:
    def __init__(self):
        self.posts = []

    def download(self, post, options):
        self.posts.append(post)
        return cli.DownloadResult()


class LoggedInFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"IGDL_HOME": self.tmp.name})
        self.env.start()
        self.app = cli.App(Settings(output_dir=self.tmp.name), interactive=False)
        self.app.api = LoggedInAPI()
        self.recorder = RecordingDownloader()
        self.app.downloader = lambda: self.recorder
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def test_stories_of_users_and_a_single_story_link(self):
        self.app.download_stories(["@someone", "https://www.instagram.com/stories/someone/5002/"],
                                  cli.DownloadOptions())
        first, second = self.recorder.posts
        self.assertEqual((first.kind, first.username, len(first.items)), ("story", "someone", 2))
        self.assertEqual([item.media_id for item in second.items], ["5002"])

    def test_expired_story_link(self):
        link = cli.parse_link("https://www.instagram.com/stories/someone/1234/")
        with self.assertRaises(NotFound):
            list(self.app.posts_for_link(link))

    def test_highlight_link(self):
        link = cli.parse_link("https://www.instagram.com/stories/highlights/17912345678901234/")
        posts = list(self.app.posts_for_link(link))
        self.assertEqual((posts[0].kind, posts[0].title), ("highlight", "Title 17912345678901234"))

    def test_all_highlights_are_fetched_in_groups(self):
        self.app.download_highlights("someone", cli.DownloadOptions())
        self.assertEqual([len(ids) for ids in self.app.api.reel_requests], [5, 2])
        self.assertEqual([post.title for post in self.recorder.posts], [f"Title {n}" for n in range(1, 8)])

    def test_stories_of_everyone_followed(self):
        self.app.download_following_stories(cli.DownloadOptions())
        self.assertEqual(self.app.api.reel_requests, [["99", "98"]])
        self.assertEqual(len(self.recorder.posts), 2)

    def test_export_and_download_saved_posts(self):
        medias = self.app.export_saved()
        txt = next(Path(self.tmp.name).glob("saved_posts_*.txt")).read_text(encoding="utf-8")
        self.assertEqual(txt.split(), ["https://www.instagram.com/p/Cabc123XYZ_/",
                                       "https://www.instagram.com/reel/Creel456ABC/"])
        csv_text = next(Path(self.tmp.name).glob("saved_posts_*.csv")).read_text(encoding="utf-8-sig")
        self.assertTrue(csv_text.startswith("url,username,type,items,posted,caption"))
        self.assertIn("carousel", csv_text)
        self.app.download_saved(medias, cli.DownloadOptions())
        self.assertEqual([post.shortcode for post in self.recorder.posts], ["Cabc123XYZ_", "Creel456ABC"])


class MainTests(unittest.TestCase):
    def test_invalid_quality(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"IGDL_HOME": tmp}), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["-q", "huge", "https://www.instagram.com/p/AAAAA11111/"]), 2)
        self.assertIn("invalid --quality", output.getvalue())


if __name__ == "__main__":
    unittest.main()
