"""Recognising Instagram links and converting between shortcodes and media IDs."""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from dataclasses import dataclass

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# Links to posts of private accounts carry 28 extra characters after the real shortcode.
PRIVATE_SUFFIX_LENGTH = 28

_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]{5,}")
_USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
_LINK_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)?(?:instagram\.com|instagr\.am)/[^\s<>\"'()\[\]{}|\\^`,]*",
    re.IGNORECASE,
)
_POST_TYPES = {"p", "reel", "reels", "tv"}
# First path segments that are Instagram pages rather than usernames.
_NOT_USERNAMES = {
    "about", "accounts", "api", "challenge", "developer", "direct", "explore", "graphql", "legal",
    "p", "privacy", "reel", "reels", "s", "share", "stories", "terms", "tv", "web",
}


def shortcode_to_pk(shortcode: str) -> int:
    """Convert a post shortcode (the code in /p/<code>/) to its numeric media ID."""
    if len(shortcode) > PRIVATE_SUFFIX_LENGTH:
        shortcode = shortcode[:-PRIVATE_SUFFIX_LENGTH]
    value = 0
    for char in shortcode:
        index = SHORTCODE_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"not a valid Instagram shortcode: {shortcode!r}")
        value = value * 64 + index
    return value


def pk_to_shortcode(pk: int | str) -> str:
    """Convert a numeric media ID (optionally "<id>_<owner id>") to its shortcode."""
    value = int(str(pk).split("_")[0])
    chars = []
    while value:
        value, rest = divmod(value, 64)
        chars.append(SHORTCODE_ALPHABET[rest])
    return "".join(reversed(chars)) or "A"


@dataclass(frozen=True)
class Link:
    """A parsed Instagram link.

    kind is one of: post, share, story, highlight, saved, collection, profile.
    """

    kind: str
    url: str
    shortcode: str = ""
    username: str = ""
    story_id: str = ""
    highlight_id: str = ""
    collection_id: str = ""


def extract_links(text: str) -> list[str]:
    """Find every Instagram link in a block of pasted text (duplicates removed, order kept)."""
    seen: set[str] = set()
    links: list[str] = []
    for match in _LINK_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def post_url(shortcode: str, reel: bool = False) -> str:
    return f"https://www.instagram.com/{'reel' if reel else 'p'}/{shortcode}/"


def _decode_base64(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def parse_link(url: str) -> Link | None:
    """Work out what an Instagram link points to. Returns None for links that aren't supported."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if not (host in ("instagram.com", "instagr.am") or host.endswith(".instagram.com")):
        return None
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        return None
    first = parts[0].lower()

    if first == "share" and len(parts) >= 2:
        return Link("share", url)

    # Posts, reels and IGTV: /p/<code>/, /reel/<code>/, /<username>/p/<code>/ ...
    for index in (0, 1):
        if len(parts) > index + 1 and parts[index].lower() in _POST_TYPES:
            if index == 1 and first in _NOT_USERNAMES:
                break
            shortcode = parts[index + 1]
            if _SHORTCODE_RE.fullmatch(shortcode) and shortcode.lower() != "audio":
                reel = parts[index].lower() in ("reel", "reels")
                return Link("post", post_url(shortcode, reel), shortcode=shortcode)
            return None

    if first == "stories" and len(parts) >= 2:
        if parts[1].lower() == "highlights":
            if len(parts) >= 3 and parts[2].isdigit():
                return Link("highlight", url, highlight_id=parts[2])
            return None
        username = parts[1]
        if not _USERNAME_RE.fullmatch(username):
            return None
        story_id = parts[2] if len(parts) >= 3 and parts[2].isdigit() else ""
        return Link("story", url, username=username, story_id=story_id)

    # Shared highlight links: /s/<base64 of "highlight:<id>">?story_media_id=...
    # They name the story that was on screen, but the whole highlight is what gets downloaded.
    if first == "s" and len(parts) >= 2:
        decoded = _decode_base64(parts[1])
        if decoded.startswith("highlight:") and decoded[10:].isdigit():
            return Link("highlight", url, highlight_id=decoded[10:])
        return None

    if first in _NOT_USERNAMES or not _USERNAME_RE.fullmatch(parts[0]):
        return None
    username = parts[0]
    if len(parts) >= 2 and parts[1].lower() == "saved":
        if len(parts) >= 4 and parts[3].isdigit():
            return Link("collection", url, username=username, collection_id=parts[3])
        return Link("saved", url, username=username)
    if len(parts) == 1:
        return Link("profile", url, username=username)
    return None
