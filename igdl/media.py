"""Turning Instagram API media objects into downloadable files, and picking a resolution."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from .links import pk_to_shortcode, post_url

PHOTO = "photo"
VIDEO = "video"


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(num_bytes / 1024))} KB"


def codec_name(codec: str) -> str:
    codec = codec.lower()
    for prefix, name in (("avc", "H.264"), ("vp09", "VP9"), ("vp9", "VP9"), ("av01", "AV1"),
                         ("hvc", "HEVC"), ("hev", "HEVC")):
        if codec.startswith(prefix):
            return name
    return codec


@dataclass
class Variant:
    """One downloadable version (resolution / bitrate) of a photo or video."""

    url: str
    width: int = 0
    height: int = 0
    bandwidth: int = 0  # bits per second; only known for DASH streams
    codec: str = ""
    dash: bool = False  # a separate video stream from Instagram's DASH manifest
    audio_url: str = ""  # DASH audio stream that has to be merged into the video
    size: int = 0  # bytes, when known in advance (video + audio for DASH)

    @property
    def short_side(self) -> int:
        sides = [side for side in (self.width, self.height) if side]
        return min(sides) if sides else 0

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def needs_merge(self) -> bool:
        return self.dash and bool(self.audio_url)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else "unknown size"

    def describe(self) -> str:
        parts = [self.resolution]
        if self.bandwidth:
            parts.append(f"{self.bandwidth / 1_000_000:.1f} Mbps")
        if self.codec and not self.codec.lower().startswith("avc"):
            parts.append(codec_name(self.codec))
        if self.size:
            parts.append(f"~{format_size(self.size)}")
        return ", ".join(parts)


@dataclass
class MediaItem:
    """A single photo or video. Posts have one, carousels several, story reels one per story."""

    kind: str  # PHOTO or VIDEO
    variants: list[Variant]  # best first
    position: int = 1  # 1-based place inside the post
    media_id: str = ""
    taken_at: int = 0
    still: list[Variant] = field(default_factory=list)  # the picture, for photos posted with music

    def as_photo(self) -> MediaItem | None:
        """The still picture of a photo that Instagram turned into a video by adding music."""
        if self.kind == VIDEO and self.still:
            return MediaItem(PHOTO, self.still, self.position, self.media_id, self.taken_at)
        return None


@dataclass
class Post:
    """Something that was downloaded from one link: a post/reel, a user's stories, or a highlight."""

    kind: str  # "post", "story" or "highlight"
    username: str
    items: list[MediaItem]
    shortcode: str = ""
    taken_at: int = 0
    caption: str = ""
    title: str = ""  # highlight title
    reel_id: str = ""  # highlight ID
    is_reel: bool = False

    @property
    def url(self) -> str:
        if self.kind == "post" and self.shortcode:
            return post_url(self.shortcode, self.is_reel)
        if self.kind == "highlight" and self.reel_id:
            return f"https://www.instagram.com/stories/highlights/{self.reel_id}/"
        return f"https://www.instagram.com/stories/{self.username}/"

    def summary(self) -> str:
        photos = sum(1 for item in self.items if item.kind == PHOTO)
        videos = len(self.items) - photos
        counts = []
        if photos:
            counts.append(f"{photos} photo{'s' if photos != 1 else ''}")
        if videos:
            counts.append(f"{videos} video{'s' if videos != 1 else ''}")
        what = " + ".join(counts) or "nothing to download"
        if self.kind == "post" and len(self.items) > 1:
            return f"carousel: {what}"
        if self.kind == "post" and self.is_reel:
            return "reel"
        return what


# ---------------------------------------------------------------------------- parsing


def post_from_media(media: dict) -> Post:
    """Build a Post from a media object of Instagram's API (feed post, reel or carousel)."""
    user = media.get("user") or media.get("owner") or {}
    caption = media.get("caption")
    taken_at = _int(media.get("taken_at"))
    nodes = media.get("carousel_media") or [media]
    items = []
    for position, node in enumerate(nodes, 1):
        item = item_from_node(node, position)
        if item is not None:
            item.taken_at = item.taken_at or taken_at
            items.append(item)
    shortcode = media.get("code") or (pk_to_shortcode(media["pk"]) if media.get("pk") else "")
    return Post(
        kind="post",
        username=user.get("username") or "unknown",
        items=items,
        shortcode=shortcode,
        taken_at=taken_at,
        caption=(caption.get("text") or "") if isinstance(caption, dict) else "",
        is_reel=media.get("product_type") == "clips",
    )


def post_from_reel(reel: dict, kind: str) -> Post:
    """Build a Post from a story reel (a user's current stories) or a highlight."""
    user = reel.get("user") or {}
    items = []
    for position, node in enumerate(reel.get("items") or [], 1):
        item = item_from_node(node, position)
        if item is not None:
            items.append(item)
    return Post(
        kind=kind,
        username=user.get("username") or "unknown",
        items=items,
        taken_at=max((item.taken_at for item in items), default=0),
        title=reel.get("title") or "",
        reel_id=str(reel.get("id") or "").split(":")[-1],
    )


def item_from_node(node: dict, position: int = 1) -> MediaItem | None:
    media_id = str(node.get("pk") or node.get("id") or "").split("_")[0]
    taken_at = _int(node.get("taken_at"))
    images = image_variants(node)
    videos = video_variants(node)
    if videos or node.get("media_type") == 2:
        still = images if node.get("original_media_type") == 1 else []
        return MediaItem(VIDEO, videos, position, media_id, taken_at, still=still)
    if images:
        return MediaItem(PHOTO, images, position, media_id, taken_at)
    return None


def image_variants(node: dict) -> list[Variant]:
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    variants: list[Variant] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        url = candidate.get("url")
        width, height = _int(candidate.get("width")), _int(candidate.get("height"))
        if not url or (width, height) in seen:
            continue
        seen.add((width, height))
        variants.append(Variant(url, width, height))
    variants.sort(key=lambda v: v.pixels, reverse=True)

    # Instagram also lists square crops (used for profile grids); keep the full picture only.
    reference = (_int(node.get("original_width")), _int(node.get("original_height")))
    if not all(reference) and variants:
        reference = (variants[0].width, variants[0].height)
    if all(reference):
        ratio = reference[0] / reference[1]
        full = [v for v in variants if v.width and v.height and abs(v.width / v.height - ratio) <= ratio * 0.03]
        variants = full or variants
    return variants


def video_variants(node: dict) -> list[Variant]:
    variants: list[Variant] = []
    by_file: dict[str, Variant] = {}
    for version in node.get("video_versions") or []:
        url = version.get("url")
        if not url:
            continue
        width, height = _int(version.get("width")), _int(version.get("height"))
        key = urllib.parse.urlparse(url).path
        existing = by_file.get(key)
        if existing is not None:  # the same file is often listed several times
            if width * height > existing.pixels:
                existing.width, existing.height = width, height
            continue
        by_file[key] = Variant(url, width, height)
        variants.append(by_file[key])
    manifest = node.get("video_dash_manifest")
    if manifest:
        variants.extend(parse_dash_manifest(manifest))
    variants.sort(key=lambda v: (v.pixels, not v.dash, v.codec.lower().startswith("avc"), v.bandwidth),
                  reverse=True)
    return variants


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_dash_manifest(manifest: str) -> list[Variant]:
    """Video streams listed in a DASH manifest, each paired with the best audio stream."""
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError:
        return []
    videos: list[Variant] = []
    audios: list[tuple[int, int, str]] = []  # (bandwidth, size, url)
    for adaptation in (el for el in root.iter() if _local_name(el.tag) == "AdaptationSet"):
        set_type = adaptation.get("contentType") or (adaptation.get("mimeType") or "").split("/")[0]
        for rep in adaptation:
            if _local_name(rep.tag) != "Representation":
                continue
            url = next((child.text.strip() for child in rep
                        if _local_name(child.tag) == "BaseURL" and child.text), "")
            if not url.startswith("http"):
                continue
            kind = set_type or (rep.get("mimeType") or "").split("/")[0]
            bandwidth = _int(rep.get("bandwidth"))
            size = _int(rep.get("FBContentLength"))
            if kind == "audio" or rep.get("audioSamplingRate"):
                audios.append((bandwidth, size, url))
            elif kind == "video" or rep.get("width"):
                videos.append(Variant(
                    url,
                    _int(rep.get("width") or adaptation.get("width")),
                    _int(rep.get("height") or adaptation.get("height")),
                    bandwidth=bandwidth,
                    codec=rep.get("codecs") or adaptation.get("codecs") or "",
                    dash=True,
                    size=size,
                ))
    if audios:
        _, audio_size, audio_url = max(audios)
        for video in videos:
            video.audio_url = audio_url
            if video.size and audio_size:
                video.size += audio_size
    return videos


# ---------------------------------------------------------------------------- choosing


def pick_variant(
    variants: list[Variant],
    limit: int = 0,
    allow_merge: bool = True,
    file_size: Callable[[Variant], int] | None = None,
) -> Variant | None:
    """Pick which version to download.

    Without a limit the highest resolution wins; with one, the highest resolution whose shorter
    side is at most `limit` (or the smallest available if none is that small). Streams that need
    ffmpeg to merge video and audio are skipped when `allow_merge` is False, unless nothing else
    exists.
    """
    pool = [v for v in variants if allow_merge or not v.needs_merge] or list(variants)
    if not pool:
        return None
    if limit:
        fitting = [v for v in pool if v.short_side and v.short_side <= limit]
        pool = fitting or [min(pool, key=lambda v: (v.pixels, v.bandwidth))]
    top = max(v.pixels for v in pool)
    tier = [v for v in pool if v.pixels == top]
    files = [v for v in tier if not v.dash]
    streams = sorted((v for v in tier if v.dash),
                     key=lambda v: (v.codec.lower().startswith("avc"), v.bandwidth), reverse=True)
    if not streams:
        return files[0]
    if not files:
        return streams[0]
    # The same resolution exists as a ready-made file and as DASH streams. The file is simpler
    # (audio included, no merging), so it wins unless the stream is clearly bigger = higher bitrate.
    if file_size is not None and streams[0].size:
        size = file_size(files[0])
        if size and streams[0].size > size * 1.15:
            return streams[0]
    return files[0]
