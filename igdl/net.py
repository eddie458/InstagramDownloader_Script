"""Small HTTP client built on urllib, so the downloader needs nothing outside the standard library."""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Mapping

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
COOKIE_DOMAIN = ".instagram.com"


class NetworkError(Exception):
    """The request never got an HTTP answer (no connection, DNS failure, timeout...)."""


class Response:
    def __init__(self, status: int, url: str, headers: Mapping[str, str] | None, body: bytes):
        self.status = status
        self.url = url
        self.headers = headers if headers is not None else {}
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        """Parse the body as JSON; raises ValueError if it isn't JSON."""
        return json.loads(self.body.decode("utf-8", "replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decode_body(raw: bytes, headers) -> bytes:
    encoding = (headers.get("Content-Encoding") or "").lower() if headers else ""
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        pass
    return raw


class HttpClient:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 30.0):
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        cookie_handler = urllib.request.HTTPCookieProcessor(self.cookies)
        self._opener = urllib.request.build_opener(cookie_handler)
        self._opener_no_redirect = urllib.request.build_opener(cookie_handler, _NoRedirect())

    # ------------------------------------------------------------------ cookies
    def get_cookie(self, name: str) -> str | None:
        for cookie in self.cookies:
            if cookie.name == name and cookie.domain.endswith("instagram.com"):
                return cookie.value
        return None

    def set_cookie(self, name: str, value: str, domain: str = COOKIE_DOMAIN) -> None:
        self.cookies.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True, secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))

    def set_cookies(self, cookies: Mapping[str, str]) -> None:
        for name, value in cookies.items():
            self.set_cookie(name, value)

    def instagram_cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self.cookies if c.domain.endswith("instagram.com")}

    def clear_cookies(self) -> None:
        self.cookies.clear()

    # ------------------------------------------------------------------ requests
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> Response:
        """Send a request and return the response, whatever its status code."""
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if body is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        opener = self._opener if follow_redirects else self._opener_no_redirect
        try:
            with opener.open(request, timeout=self.timeout) as resp:
                raw = resp.read()
                return Response(resp.status, resp.geturl(), resp.headers, _decode_body(raw, resp.headers))
        except urllib.error.HTTPError as err:
            try:
                raw = err.read()
            except Exception:
                raw = b""
            return Response(err.code, err.geturl() or url, err.headers, _decode_body(raw, err.headers))
        except (urllib.error.URLError, OSError) as err:
            raise NetworkError(str(getattr(err, "reason", err))) from err

    def open_stream(self, url: str, headers: Mapping[str, str] | None = None):
        """Open a URL for a streamed download. The caller must close the returned response.

        Raises NetworkError for connection problems and urllib.error.HTTPError for HTTP errors.
        """
        request_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, headers=request_headers)
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as err:
            raise NetworkError(str(getattr(err, "reason", err))) from err
