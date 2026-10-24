"""Client for the web API that instagram.com itself uses in the browser."""

from __future__ import annotations

import json
import random
import re
import secrets
import time
import urllib.parse
from typing import Callable, Iterator

from .auth import user_id_from_sessionid
from .links import parse_link, shortcode_to_pk
from .net import HttpClient, NetworkError, Response

BASE_URL = "https://www.instagram.com"
API_URL = BASE_URL + "/api/v1"
APP_ID = "936619743392459"
ASBD_ID = "359341"
# GraphQL document that instagram.com uses to load a post for logged-out visitors.
POST_DOC_ID = "27128499623469141"
# Instagram allows only a few logged-out requests in a short time, so those are spaced out more.
LOGGED_OUT_DELAY = (15.0, 25.0)


class InstagramError(Exception):
    """Something went wrong; the message is written for the user."""


class LoginRequired(InstagramError):
    pass


class SessionExpired(LoginRequired):
    pass


class RateLimited(InstagramError):
    pass


class NotFound(InstagramError):
    pass


class Checkpoint(InstagramError):
    pass


_POST_IN_PAGE_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reels?|tv)/[A-Za-z0-9_-]{5,}")


class InstagramAPI:
    def __init__(
        self,
        http: HttpClient,
        min_delay: float = 4.0,
        max_delay: float = 8.0,
        log: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = http
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.log = log or (lambda message: None)
        self._sleep = sleep
        self._recent_calls: list[float] = []
        self._www_claim = "0"
        self._user_ids: dict[str, str] = {}

    @property
    def logged_in(self) -> bool:
        return bool(self.http.get_cookie("sessionid"))

    @property
    def viewer_id(self) -> str | None:
        return self.http.get_cookie("ds_user_id") or user_id_from_sessionid(self.http.get_cookie("sessionid") or "")

    # ------------------------------------------------------------------ plumbing

    def _throttle(self) -> None:
        """Space out requests the way a person browsing would, to stay clear of rate limits.

        Logged in, a few quick requests are fine; after that each one waits min_delay..max_delay
        seconds. Logged out, every request after the first waits at least LOGGED_OUT_DELAY.
        """
        now = time.monotonic()
        self._recent_calls = [t for t in self._recent_calls if now - t < 60]
        if self._recent_calls:
            if not self.logged_in:
                gap = random.uniform(max(self.min_delay, LOGGED_OUT_DELAY[0]),
                                     max(self.max_delay, LOGGED_OUT_DELAY[1]))
            elif len(self._recent_calls) >= 3:
                gap = random.uniform(self.min_delay, self.max_delay)
            else:
                gap = random.uniform(min(1.0, self.min_delay), min(2.0, self.max_delay))
            wait = self._recent_calls[-1] + gap - now
            if wait > 0:
                self._sleep(wait)
        self._recent_calls.append(time.monotonic())

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        csrf_token = self.http.get_cookie("csrftoken")
        if not csrf_token:
            csrf_token = secrets.token_hex(16)
            self.http.set_cookie("csrftoken", csrf_token)
        return {
            "Accept": "*/*",
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": ASBD_ID,
            "X-IG-WWW-Claim": self._www_claim,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": referer or BASE_URL + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _call(self, method: str, url: str, *, params=None, data=None, referer: str | None = None,
              wait_on_rate_limit: bool = True) -> dict:
        waits = [60, 180] if wait_on_rate_limit else []
        network_failures = 0
        while True:
            self._throttle()
            try:
                response = self.http.request(method, url, params=params, data=data, headers=self._headers(referer))
            except NetworkError as err:
                network_failures += 1
                if network_failures >= 3:
                    raise InstagramError(f"Network problem ({err}). Check your internet connection.") from err
                self._sleep(5 * network_failures)
                continue
            claim = response.headers.get("x-ig-set-www-claim")
            if claim:
                self._www_claim = claim
            try:
                return self._check(response)
            except RateLimited:
                # Logged out, waiting rarely helps: Instagram wants a login. Logged in, it usually passes.
                if not self.logged_in or not waits:
                    raise
                wait = waits.pop(0)
                self.log(f"Instagram asked to slow down. Waiting {wait} seconds, then retrying "
                         "(press Ctrl+C to stop)...")
                self._sleep(wait)

    def _check(self, response: Response) -> dict:
        """Return the JSON body of a successful response, or raise a helpful error."""
        try:
            data = response.json()
        except ValueError:
            data = None
        if data is not None and not isinstance(data, dict):
            data = {"data": data}
        message = str((data or {}).get("message") or "")
        lowered = message.lower()
        url = response.url or ""

        if "/challenge" in url or (data is not None and (
                data.get("checkpoint_url") or "checkpoint" in lowered or "challenge" in lowered)):
            raise Checkpoint("Instagram wants to verify your account. Open instagram.com in your browser, "
                             "complete the security check, then log in here again.")
        if response.status == 429 or any(text in lowered for text in
                                          ("wait a few minutes", "rate limit", "feedback_required")):
            if self.logged_in:
                raise RateLimited("Instagram is temporarily limiting requests, which happens after many "
                                  "requests in a short time. Take a break (15 minutes or more) and try again.")
            raise RateLimited("Instagram is limiting logged-out visitors right now. Log in for reliable "
                              "downloads, or try again later (it can take 15 minutes or more).")
        if response.status >= 500:
            raise InstagramError(f"Instagram is having problems right now (HTTP {response.status}). "
                                 "Try again later.")
        needs_login = (response.status == 401 or "/accounts/login" in url or "login_required" in lowered
                       or bool(data and data.get("require_login"))
                       or (response.status == 403 and not message))
        if needs_login or data is None:
            if self.logged_in:
                raise SessionExpired("Your Instagram session has expired or was logged out. Please log in again.")
            raise LoginRequired("Instagram only shows this to logged-in users.")
        if response.status == 404:
            raise NotFound("Not found. The link may be wrong, or the content was deleted.")
        if response.status >= 400 or data.get("status") == "fail":
            if response.status in (400, 403) or "not found" in lowered or "unavailable" in lowered:
                raise NotFound((message or "Not available") + ". The content may be deleted, or from a "
                               "private account you don't follow.")
            raise InstagramError(f"Instagram returned an error (HTTP {response.status}): {message or 'no details'}")
        return data

    # ------------------------------------------------------------------ posts

    def media(self, shortcode: str) -> dict:
        """The media object (API v1 format) of a post, reel or carousel."""
        if not self.logged_in:
            return self._media_logged_out(shortcode)
        data = self._call("GET", f"{API_URL}/media/{shortcode_to_pk(shortcode)}/info/")
        items = data.get("items") or []
        if not items:
            raise NotFound("Post not found.")
        return items[0]

    def media_by_id(self, media_id: str) -> dict:
        data = self._call("GET", f"{API_URL}/media/{media_id}/info/")
        items = data.get("items") or []
        if not items:
            raise NotFound("Not found.")
        return items[0]

    def _media_logged_out(self, shortcode: str) -> dict:
        variables = {
            "shortcode": shortcode,
            "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
        }
        data = self._call("POST", f"{BASE_URL}/graphql/query", data={
            "variables": json.dumps(variables, separators=(",", ":")),
            "doc_id": POST_DOC_ID,
            "server_timestamps": "true",
        }, referer=f"{BASE_URL}/p/{shortcode}/")
        info = (data.get("data") or {}).get("xdt_api__v1__media__shortcode__web_info") or {}
        items = info.get("items") or []
        if items:
            return items[0]
        if len(shortcode) > 28:
            raise LoginRequired("This post is from a private account. Log in with an account that follows it.")
        # Instagram can give this same empty answer when it's quietly limiting logged-out visitors.
        raise NotFound("Instagram didn't return this post. It may be deleted or from a private account, or "
                       "Instagram may be limiting logged-out visitors right now; logging in usually fixes this.")

    def resolve_share_link(self, url: str) -> str:
        """Find out where an instagram.com/share/... link points to."""
        current = url
        for _ in range(5):
            self._throttle()
            try:
                response = self.http.request("GET", current, follow_redirects=False, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                })
            except NetworkError as err:
                raise InstagramError(f"Network problem ({err}). Check your internet connection.") from err
            location = response.headers.get("Location")
            if response.status in (301, 302, 303, 307, 308) and location:
                current = urllib.parse.urljoin(current, location)
                target = _target_in_url(current)
                if target:
                    return target
                continue
            for match in _POST_IN_PAGE_RE.finditer(response.text()):
                link = parse_link("https://www." + match.group(0) + "/")
                if link and link.kind == "post":
                    return link.url
            break
        raise NotFound("Couldn't find what this share link points to.")

    # ------------------------------------------------------------------ users

    def user_id(self, username: str) -> str:
        name = username.strip().lstrip("@").lower()
        if name in self._user_ids:
            return self._user_ids[name]
        user_id = None
        try:
            data = self._call("GET", f"{API_URL}/users/web_profile_info/", params={"username": name},
                              wait_on_rate_limit=False)
            user = (data.get("data") or {}).get("user") or {}
            user_id = str(user.get("id") or user.get("pk") or "") or None
        except NotFound:
            raise NotFound(f"The user @{name} doesn't exist.") from None
        except (SessionExpired, Checkpoint):
            raise
        except InstagramError:
            pass  # this endpoint is often rate-limited; the search endpoint below also works
        if not user_id:
            user_id = self._search_user_id(name)
        if not user_id:
            raise NotFound(f"Couldn't find the user @{name}.")
        self._user_ids[name] = user_id
        return user_id

    def _search_user_id(self, name: str) -> str | None:
        data = self._call("GET", f"{BASE_URL}/web/search/topsearch/", params={"query": name})
        for entry in data.get("users") or []:
            user = (entry.get("user") or {}) if isinstance(entry, dict) else {}
            if str(user.get("username", "")).lower() == name:
                return str(user.get("pk") or user.get("id") or user.get("pk_id") or "") or None
        return None

    # ------------------------------------------------------------------ stories

    def reels(self, reel_ids: list[str]) -> list[dict]:
        """Story reels: a user ID gives that user's current stories, "highlight:<id>" a highlight."""
        if not reel_ids:
            return []
        data = self._call("GET", f"{API_URL}/feed/reels_media/", params={"reel_ids": list(reel_ids)})
        reels = data.get("reels_media")
        if reels is None:
            reels = list((data.get("reels") or {}).values())
        return [reel for reel in reels if isinstance(reel, dict)]

    def stories_tray(self) -> list[dict]:
        """Accounts you follow that currently have stories."""
        data = self._call("GET", f"{API_URL}/feed/reels_tray/")
        return [entry for entry in data.get("tray") or [] if isinstance(entry, dict)]

    def highlights(self, user_id: str) -> list[dict]:
        data = self._call("GET", f"{API_URL}/highlights/{user_id}/highlights_tray/")
        return [entry for entry in data.get("tray") or [] if isinstance(entry, dict)]

    # ------------------------------------------------------------------ saved posts

    def saved_media(self, collection_id: str | None = None) -> Iterator[dict]:
        """Your saved posts (newest first), or the posts of one of your saved collections."""
        if collection_id:
            endpoint = f"{API_URL}/feed/collection/{collection_id}/posts/"
        else:
            endpoint = f"{API_URL}/feed/saved/posts/"
        params: dict[str, str] = {"count": "50"}
        while True:
            data = self._call("GET", endpoint, params=params)
            for entry in data.get("items") or []:
                media = entry.get("media") if isinstance(entry, dict) else None
                if isinstance(media, dict):
                    yield media
            next_max_id = data.get("next_max_id")
            if not data.get("more_available") or not next_max_id:
                return
            params = {"count": "50", "max_id": str(next_max_id)}

    # ------------------------------------------------------------------ account

    def current_user(self) -> dict:
        user_id = self.viewer_id
        if not user_id:
            raise LoginRequired("You're not logged in.")
        data = self._call("GET", f"{API_URL}/users/{user_id}/info/", wait_on_rate_limit=False)
        return data.get("user") or {}


def _target_in_url(url: str) -> str | None:
    """A supported link in a redirect address, including inside a login page's ?next= parameter."""
    link = parse_link(url)
    if link and link.kind not in ("share", "profile"):
        return link.url
    next_path = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("next", [""])[0]
    if next_path.startswith("/"):
        link = parse_link(BASE_URL + next_path)
        if link and link.kind not in ("share", "profile"):
            return link.url
    return None
