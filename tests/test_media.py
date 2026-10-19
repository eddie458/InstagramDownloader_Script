import unittest

from igdl.media import (PHOTO, VIDEO, Variant, item_from_node, parse_dash_manifest, pick_variant, post_from_media,
                        post_from_reel)

from tests import samples


class ParsingTests(unittest.TestCase):
    def test_carousel(self):
        post = post_from_media(samples.carousel_media())
        self.assertEqual((post.kind, post.username, post.shortcode), ("post", "someone", "Cabc123XYZ_"))
        self.assertEqual(post.caption, "Hello world")
        self.assertEqual(post.url, "https://www.instagram.com/p/Cabc123XYZ_/")
        self.assertEqual([item.kind for item in post.items], [PHOTO, VIDEO])
        self.assertEqual([item.position for item in post.items], [1, 2])
        self.assertEqual(post.summary(), "carousel: 1 photo + 1 video")

    def test_photo_variants_skip_square_crops(self):
        photo = post_from_media(samples.carousel_media()).items[0]
        self.assertEqual([v.resolution for v in photo.variants], ["1440x1800", "1080x1350", "750x938", "640x800"])

    def test_video_variants(self):
        video = post_from_media(samples.carousel_media()).items[1]
        described = [(v.resolution, v.dash, v.bandwidth) for v in video.variants]
        self.assertEqual(described, [
            ("1080x1920", True, 4000000),
            ("720x1280", False, 0),  # the progressive file listed twice is kept once, at its largest size
            ("720x1280", True, 1800000),
            ("720x1280", True, 900000),
            ("480x854", False, 0),
        ])
        best_stream = video.variants[0]
        self.assertTrue(best_stream.needs_merge)
        self.assertEqual(best_stream.audio_url, "https://cdn.example/audio.mp4")  # highest audio bitrate
        self.assertEqual(best_stream.url, "https://cdn.example/v1080.mp4?a=1&b=2")
        self.assertEqual(best_stream.size, 6500000 + 210000)

    def test_reel(self):
        post = post_from_media(samples.reel_media())
        self.assertTrue(post.is_reel)
        self.assertEqual(post.url, "https://www.instagram.com/reel/Creel456ABC/")
        self.assertEqual(post.caption, "")
        self.assertEqual(post.summary(), "reel")

    def test_dash_without_audio_needs_no_merge(self):
        streams = parse_dash_manifest(samples.DASH_VIDEO_ONLY)
        self.assertEqual(len(streams), 1)
        self.assertFalse(streams[0].needs_merge)

    def test_broken_manifest(self):
        self.assertEqual(parse_dash_manifest("<MPD"), [])

    def test_video_without_files(self):
        node = samples.video_node(manifest=None)
        node["video_versions"] = []
        item = item_from_node(node)
        self.assertEqual((item.kind, item.variants), (VIDEO, []))

    def test_photo_posted_with_music(self):
        node = samples.video_node()
        node["original_media_type"] = 1
        node["image_versions2"] = samples.photo_node()["image_versions2"]
        node["original_width"], node["original_height"] = 1440, 1800
        photo = item_from_node(node).as_photo()
        self.assertEqual(photo.kind, PHOTO)
        self.assertEqual(photo.variants[0].resolution, "1440x1800")
        self.assertIsNone(item_from_node(samples.video_node()).as_photo())

    def test_story_reel(self):
        post = post_from_reel(samples.story_reel(), "story")
        self.assertEqual((post.kind, post.username), ("story", "someone"))
        self.assertEqual([item.media_id for item in post.items], ["5001", "5002"])
        self.assertEqual(post.taken_at, samples.TAKEN_AT + 120)
        self.assertEqual(post.url, "https://www.instagram.com/stories/someone/")

    def test_highlight_reel(self):
        reel = dict(samples.story_reel(), id="highlight:17912345678901234", title="Trips")
        post = post_from_reel(reel, "highlight")
        self.assertEqual((post.reel_id, post.title), ("17912345678901234", "Trips"))
        self.assertEqual(post.url, "https://www.instagram.com/stories/highlights/17912345678901234/")


class PickVariantTests(unittest.TestCase):
    def setUp(self):
        post = post_from_media(samples.carousel_media())
        self.photo, self.video = post.items

    def test_best(self):
        self.assertEqual(pick_variant(self.photo.variants).resolution, "1440x1800")
        best = pick_variant(self.video.variants)
        self.assertEqual((best.resolution, best.dash), ("1080x1920", True))

    def test_without_ffmpeg_skips_streams_that_need_merging(self):
        best = pick_variant(self.video.variants, allow_merge=False)
        self.assertEqual((best.resolution, best.dash), ("720x1280", False))

    def test_limit(self):
        self.assertEqual(pick_variant(self.photo.variants, limit=1080).resolution, "1080x1350")
        self.assertEqual(pick_variant(self.photo.variants, limit=700).resolution, "640x800")
        self.assertEqual(pick_variant(self.video.variants, limit=720).dash, False)
        self.assertEqual(pick_variant(self.video.variants, limit=480).resolution, "480x854")

    def test_limit_below_everything_gives_the_smallest(self):
        self.assertEqual(pick_variant(self.photo.variants, limit=100).resolution, "640x800")
        self.assertEqual(pick_variant(self.video.variants, limit=100).resolution, "480x854")

    def test_equal_resolution_prefers_the_file_unless_the_stream_is_clearly_bigger(self):
        small_file = pick_variant(self.video.variants, limit=720, file_size=lambda v: 1_000_000)
        self.assertEqual((small_file.dash, small_file.bandwidth), (True, 1800000))
        similar_file = pick_variant(self.video.variants, limit=720, file_size=lambda v: 3_000_000)
        self.assertFalse(similar_file.dash)

    def test_only_streams_without_ffmpeg(self):
        streams = parse_dash_manifest(samples.DASH_WITH_AUDIO)
        best = pick_variant(streams, allow_merge=False)
        self.assertEqual(best.resolution, "1080x1920")  # nothing else exists, so it's still offered

    def test_empty(self):
        self.assertIsNone(pick_variant([]))

    def test_describe(self):
        self.assertEqual(Variant("u", 720, 1280, bandwidth=1_800_000, dash=True, size=3_110_000).describe(),
                         "720x1280, 1.8 Mbps, ~3.0 MB")
        self.assertEqual(Variant("u", 1080, 1920, codec="vp09.00.40.08").describe(), "1080x1920, VP9")


if __name__ == "__main__":
    unittest.main()
