import json
import unittest
import urllib.parse

from igdl.api import (Checkpoint, InstagramAPI, LoginRequired, NotFound, RateLimited, SessionExpired)
from igdl.net import HttpClient, NetworkError, Response

from tests import samples


class FakeHttp(HttpClient):
    """HttpClient that answers from a queue instead of the network and records the requests."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, params=None, data=None, headers=None, follow_redirects=True):
        self.requests.append({"method": method, "url": url, "params": params, "data": data,
                              "headers": headers or {}})
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def reply(status=200, body=None, url="https://www.instagram.com/api/v1/x/", headers=None):
    raw = body.encode() if isinstance(body, str) else json.dumps(body if body is not None else {}).encode()
    return Response(status, url, headers or {}, raw)


def make_api(responses, logged_in=False):
    http = FakeHttp(responses)
    if logged_in:
        http.set_cookie("sessionid", "1234567890%3Aabc%3A1")
    sleeps = []
    api = InstagramAPI(http, min_delay=0, max_delay=0, sleep=sleeps.append)
    return api, http, sleeps


class PostTests(unittest.TestCase):
    def test_logged_out_uses_graphql_with_csrf_token(self):
        media = samples.reel_media()
        body = {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [media]}}}
        api, http, _ = make_api([reply(body=body)])
        self.assertEqual(api.media("Creel456ABC"), media)
        request = http.requests[0]
        self.assertEqual((request["method"], request["url"]), ("POST", "https://www.instagram.com/graphql/query"))
        self.assertEqual(json.loads(request["data"]["variables"])["shortcode"], "Creel456ABC")
        token = request["headers"]["X-CSRFToken"]
        self.assertTrue(token)
        self.assertEqual(http.get_cookie("csrftoken"), token)

    def test_logged_out_missing_post(self):
        body = {"errors": [{"message": "execution error"}], "data": None}
        api, _, _ = make_api([reply(body=body)])
        with self.assertRaises(NotFound):
            api.media("AAAAAAAAAAA")

    def test_logged_out_private_post(self):
        api, _, _ = make_api([reply(body={"data": {"xdt_api__v1__media__shortcode__web_info": None}})])
        with self.assertRaises(LoginRequired):
            api.media("Cabc123XYZ_" + "x" * 28)

    def test_logged_in_uses_media_info(self):
        media = samples.carousel_media()
        api, http, _ = make_api([reply(body={"items": [media], "status": "ok"})], logged_in=True)
        self.assertEqual(api.media("BQ0eAlwhDrw"), media)
        self.assertEqual(http.requests[0]["url"], "https://www.instagram.com/api/v1/media/1455920561485265648/info/")
        self.assertEqual(http.requests[0]["method"], "GET")


class ErrorTests(unittest.TestCase):
    def test_rate_limit_logged_in_waits_and_retries(self):
        api, http, sleeps = make_api([reply(429, {"message": "Please wait a few minutes before you try again."}),
                                      reply(body={"items": [{"pk": "1"}], "status": "ok"})], logged_in=True)
        self.assertEqual(api.media("BQ0eAlwhDrw"), {"pk": "1"})
        self.assertIn(60, sleeps)
        self.assertEqual(len(http.requests), 2)

    def test_rate_limit_logged_out_fails_fast(self):
        body = {"message": "Please wait a few minutes before you try again.", "require_login": True,
                "status": "fail"}
        api, http, _ = make_api([reply(401, body)])
        with self.assertRaises(RateLimited):
            api.media("BQ0eAlwhDrw")
        self.assertEqual(len(http.requests), 1)

    def test_login_page_when_logged_in_means_expired_session(self):
        api, _, _ = make_api([reply(200, "<!DOCTYPE html><html>login</html>",
                                    url="https://www.instagram.com/accounts/login/?next=%2F")], logged_in=True)
        with self.assertRaises(SessionExpired):
            api.media("BQ0eAlwhDrw")

    def test_login_required_when_logged_out(self):
        api, _, _ = make_api([reply(200, "<html></html>")])
        with self.assertRaises(LoginRequired):
            api.stories_tray()

    def test_missing_media(self):
        body = {"message": "Media not found or unavailable", "status": "fail"}
        api, _, _ = make_api([reply(400, body)], logged_in=True)
        with self.assertRaises(NotFound):
            api.media("BQ0eAlwhDrw")

    def test_checkpoint(self):
        body = {"message": "checkpoint_required", "checkpoint_url": "/challenge/x/", "status": "fail"}
        api, _, _ = make_api([reply(400, body)], logged_in=True)
        with self.assertRaises(Checkpoint):
            api.stories_tray()

    def test_network_errors_are_retried(self):
        api, http, sleeps = make_api([NetworkError("reset"), reply(body={"tray": [], "status": "ok"})],
                                     logged_in=True)
        self.assertEqual(api.stories_tray(), [])
        self.assertEqual(len(http.requests), 2)


class FeedTests(unittest.TestCase):
    def test_saved_posts_pagination(self):
        page1 = {"items": [{"media": {"pk": "1"}}, {"media": {"pk": "2"}}], "more_available": True,
                 "next_max_id": "CURSOR", "status": "ok"}
        page2 = {"items": [{"media": {"pk": "3"}}], "more_available": False, "status": "ok"}
        api, http, _ = make_api([reply(body=page1), reply(body=page2)], logged_in=True)
        self.assertEqual([m["pk"] for m in api.saved_media()], ["1", "2", "3"])
        self.assertEqual(http.requests[0]["url"], "https://www.instagram.com/api/v1/feed/saved/posts/")
        self.assertEqual(http.requests[1]["params"]["max_id"], "CURSOR")

    def test_collection(self):
        api, http, _ = make_api([reply(body={"items": [], "more_available": False, "status": "ok"})], logged_in=True)
        self.assertEqual(list(api.saved_media("17999")), [])
        self.assertEqual(http.requests[0]["url"], "https://www.instagram.com/api/v1/feed/collection/17999/posts/")

    def test_reels_both_response_shapes(self):
        reel = samples.story_reel()
        api, http, _ = make_api([reply(body={"reels_media": [reel], "status": "ok"}),
                                 reply(body={"reels": {"99": reel}, "status": "ok"})], logged_in=True)
        self.assertEqual(api.reels(["99"]), [reel])
        self.assertEqual(api.reels(["highlight:1", "highlight:2"]), [reel])
        self.assertEqual(http.requests[1]["params"], {"reel_ids": ["highlight:1", "highlight:2"]})

    def test_user_id_falls_back_to_search(self):
        api, http, _ = make_api([
            reply(401, {"message": "Please wait a few minutes before you try again.", "status": "fail"}),
            reply(body={"users": [{"user": {"pk": "111", "username": "other"}},
                                  {"user": {"pk": "222", "username": "Wanted"}}]}),
        ], logged_in=True)
        self.assertEqual(api.user_id("@wanted"), "222")
        self.assertEqual(api.user_id("WANTED"), "222")  # cached, no new request
        self.assertEqual(len(http.requests), 2)

    def test_unknown_user(self):
        api, _, _ = make_api([reply(404, {"data": {"user": None}, "status": "ok"})], logged_in=True)
        with self.assertRaises(NotFound):
            api.user_id("nobody_here")


class ShareLinkTests(unittest.TestCase):
    def test_redirect_to_reel(self):
        api, http, _ = make_api([
            reply(302, "", headers={"Location": "https://www.instagram.com/reel/Creel456ABC/?igsh=abc"}),
        ])
        self.assertEqual(api.resolve_share_link("https://www.instagram.com/share/reel/BAabc/"),
                         "https://www.instagram.com/reel/Creel456ABC/")

    def test_redirect_to_login_page(self):
        location = "https://www.instagram.com/accounts/login/?next=" + urllib.parse.quote("/p/Cabc123XYZ_/")
        api, _, _ = make_api([reply(302, "", headers={"Location": location})])
        self.assertEqual(api.resolve_share_link("https://www.instagram.com/share/BAabc/"),
                         "https://www.instagram.com/p/Cabc123XYZ_/")

    def test_link_inside_page(self):
        page = '<meta property="og:url" content="https://www.instagram.com/p/Cabc123XYZ_/" />'
        api, _, _ = make_api([reply(200, page)])
        self.assertEqual(api.resolve_share_link("https://www.instagram.com/share/BAabc/"),
                         "https://www.instagram.com/p/Cabc123XYZ_/")


if __name__ == "__main__":
    unittest.main()
