import base64
import unittest

from igdl.links import extract_links, parse_link, pk_to_shortcode, shortcode_to_pk


class ShortcodeTests(unittest.TestCase):
    # Pairs observed in real API responses.
    KNOWN = {
        "BQ0eAlwhDrw": 1455920561485265648,
        "CDUMkliABpa": 2365570995034528346,
        "Chunk8-jurw": 2913440072144448240,
    }

    def test_shortcode_to_pk(self):
        for shortcode, pk in self.KNOWN.items():
            self.assertEqual(shortcode_to_pk(shortcode), pk)

    def test_pk_to_shortcode_round_trip(self):
        for shortcode, pk in self.KNOWN.items():
            self.assertEqual(pk_to_shortcode(pk), shortcode)
            self.assertEqual(pk_to_shortcode(f"{pk}_25025320"), shortcode)

    def test_private_post_shortcode_has_28_extra_characters(self):
        self.assertEqual(shortcode_to_pk("BQ0eAlwhDrw" + "x" * 28), self.KNOWN["BQ0eAlwhDrw"])

    def test_invalid_shortcode(self):
        with self.assertRaises(ValueError):
            shortcode_to_pk("abc!def")


class ParseLinkTests(unittest.TestCase):
    def assert_post(self, url, shortcode, reel=False):
        link = parse_link(url)
        self.assertIsNotNone(link, url)
        self.assertEqual(link.kind, "post", url)
        self.assertEqual(link.shortcode, shortcode, url)
        expected = f"https://www.instagram.com/{'reel' if reel else 'p'}/{shortcode}/"
        self.assertEqual(link.url, expected, url)

    def test_post_links(self):
        self.assert_post("https://www.instagram.com/p/C7abcDEF123/", "C7abcDEF123")
        self.assert_post("https://instagram.com/p/C7abcDEF123", "C7abcDEF123")
        self.assert_post("instagram.com/p/C7abcDEF123/?igsh=MWQ1ZGUxMzBkMA==&img_index=2", "C7abcDEF123")
        self.assert_post("https://m.instagram.com/p/C7abcDEF123/", "C7abcDEF123")
        self.assert_post("https://instagr.am/p/C7abcDEF123/", "C7abcDEF123")
        self.assert_post("https://www.instagram.com/someuser/p/C7abc-EF_23/", "C7abc-EF_23")
        self.assert_post("https://www.instagram.com/tv/BkfuX9UB-eK/", "BkfuX9UB-eK")
        self.assert_post("https://www.instagram.com/p/C7abcDEF123/embed/captioned/", "C7abcDEF123")

    def test_reel_links(self):
        self.assert_post("https://www.instagram.com/reel/Chunk8-jurw/", "Chunk8-jurw", reel=True)
        self.assert_post("https://www.instagram.com/reels/Chunk8-jurw/", "Chunk8-jurw", reel=True)
        self.assert_post("https://www.instagram.com/someuser/reel/Chunk8-jurw/?utm_source=ig_web_copy_link",
                         "Chunk8-jurw", reel=True)

    def test_story_links(self):
        link = parse_link("https://www.instagram.com/stories/some.user/3412345678901234567/?igsh=abc")
        self.assertEqual((link.kind, link.username, link.story_id), ("story", "some.user", "3412345678901234567"))
        link = parse_link("https://www.instagram.com/stories/some_user/")
        self.assertEqual((link.kind, link.username, link.story_id), ("story", "some_user", ""))

    def test_highlight_links(self):
        link = parse_link("https://www.instagram.com/stories/highlights/17912345678901234/")
        self.assertEqual((link.kind, link.highlight_id), ("highlight", "17912345678901234"))
        token = base64.urlsafe_b64encode(b"highlight:17912345678901234").decode().rstrip("=")
        link = parse_link(f"https://www.instagram.com/s/{token}?story_media_id=3412345678901234567_99&igsh=x")
        self.assertEqual((link.kind, link.highlight_id), ("highlight", "17912345678901234"))

    def test_share_saved_collection_and_profile_links(self):
        self.assertEqual(parse_link("https://www.instagram.com/share/BAabc123/").kind, "share")
        self.assertEqual(parse_link("https://www.instagram.com/share/reel/BAabc123/").kind, "share")
        self.assertEqual(parse_link("https://www.instagram.com/me/saved/").kind, "saved")
        self.assertEqual(parse_link("https://www.instagram.com/me/saved/all-posts/").kind, "saved")
        link = parse_link("https://www.instagram.com/me/saved/recipes/17999999999999999/")
        self.assertEqual((link.kind, link.collection_id), ("collection", "17999999999999999"))
        link = parse_link("https://www.instagram.com/natgeo/")
        self.assertEqual((link.kind, link.username), ("profile", "natgeo"))

    def test_unsupported_links(self):
        for url in ("https://www.instagram.com/explore/tags/cats/", "https://www.instagram.com/reels/audio/123/",
                    "https://www.instagram.com/", "https://example.com/p/C7abcDEF123/",
                    "https://www.instagram.com/accounts/login/", "https://notinstagram.com/p/C7abcDEF123/"):
            self.assertIsNone(parse_link(url), url)


class ExtractLinksTests(unittest.TestCase):
    def test_messy_text(self):
        text = ("look https://www.instagram.com/p/AAAAA11111/?igsh=x, and instagram.com/reel/BBBBB22222/.\n"
                "(https://www.instagram.com/p/AAAAA11111/?igsh=x) again plus https://example.com/p/zzz")
        self.assertEqual(extract_links(text), [
            "https://www.instagram.com/p/AAAAA11111/?igsh=x",
            "https://instagram.com/reel/BBBBB22222/",
        ])

    def test_no_links(self):
        self.assertEqual(extract_links("C:\\Users\\me\\instagram.com_links.txt"), [])


if __name__ == "__main__":
    unittest.main()
