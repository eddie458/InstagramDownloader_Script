"""Saving photos and videos to disk (with optional ffmpeg merging of HD video and audio)."""

from __future__ import annotations

import http.client
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

from .media import PHOTO, VIDEO, MediaItem, Post, Variant, format_size, pick_variant
from .net import HttpClient, NetworkError

# What the user picked in "let me choose" mode: an exact version (single item), a maximum
# resolution (applied to every item of that kind in the post) or None for the best one.
Choice = Union[Variant, int, None]
Chooser = Callable[[Post, str, "list[MediaItem]", bool], Choice]

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


class DownloadError(Exception):
    pass


@dataclass
class DownloadOptions:
    media_filter: str = "all"  # all | photos | videos
    quality: str = "best"  # best | ask | a maximum resolution such as "720"
    save_captions: bool = False


@dataclass
class DownloadResult:
    saved: list[Path] = field(default_factory=list)
    existing: list[Path] = field(default_factory=list)
    filtered: int = 0
    failed: list[str] = field(default_factory=list)

    def add(self, other: DownloadResult) -> None:
        self.saved += other.saved
        self.existing += other.existing
        self.filtered += other.filtered
        self.failed += other.failed


def safe_name(name: str, fallback: str = "untitled", max_length: int = 80) -> str:
    """Make a string usable as a file or folder name on Windows, macOS and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()[:max_length].rstrip(". ")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def find_ffmpeg(configured: str = "") -> Optional[str]:
    if configured:
        path = Path(configured).expanduser()
        if path.is_dir():
            path = path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if path.is_file():
            return str(path)
        return shutil.which(configured)
    return shutil.which("ffmpeg")


def merge_streams(ffmpeg: str, video: Path, audio: Path, output: Path) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
               "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(output)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    except OSError as err:
        raise DownloadError(f"couldn't run ffmpeg ({err})") from err
    if completed.returncode != 0:
        details = (completed.stderr or "").strip().splitlines()
        raise DownloadError("ffmpeg couldn't merge video and audio" + (f": {details[-1]}" if details else ""))


def _stamp(timestamp: int, with_time: bool) -> str:
    if not timestamp:
        return "undated"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d_%H-%M-%S" if with_time else "%Y-%m-%d")


def _post_stem(post: Post) -> str:
    return f"{_stamp(post.taken_at, False)}_{safe_name(post.username, 'unknown')}_{post.shortcode}"


def _extension(variant: Variant, kind: str) -> str:
    if kind == VIDEO:
        return ".mp4"
    url = variant.url.lower()
    if "dst-jpg" in url or "dst-jpegr" in url:
        return ".jpg"
    path = urllib.parse.urlparse(url).path
    for extension in (".png", ".webp", ".heic", ".avif"):
        if path.endswith(extension):
            return extension
    return ".jpg"


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class Downloader:
    def __init__(
        self,
        http: HttpClient,
        output_dir: str | Path,
        ffmpeg: Optional[str] = None,
        chooser: Optional[Chooser] = None,
        out: Callable[[str], None] = print,
        show_progress: Optional[bool] = None,
    ):
        self.http = http
        self.output_dir = Path(output_dir)
        self.ffmpeg = ffmpeg
        self.chooser = chooser
        self.out = out
        self.show_progress = sys.stdout.isatty() if show_progress is None else show_progress
        self._size_cache: dict[str, int] = {}
        self._status_width = 0
        self._last_progress = 0.0
        self._ffmpeg_tip_shown = False

    # ------------------------------------------------------------------ naming
    # Everything goes straight into the download folder. Names start with the date, so the folder
    # sorts chronologically, followed by the account they came from.

    def stem_for(self, post: Post, item: MediaItem) -> str:
        if post.kind == "post":
            stem = _post_stem(post)
            return f"{stem}_{item.position}" if len(post.items) > 1 else stem
        when = _stamp(item.taken_at or post.taken_at, True)
        user = safe_name(post.username, "unknown")
        item_id = item.media_id or item.position
        if post.kind == "highlight":
            title = safe_name(post.title or post.reel_id, "highlight", max_length=40)
            return f"{when}_{user}_highlight_{title}_{item_id}"
        return f"{when}_{user}_story_{item_id}"

    # ------------------------------------------------------------------ choosing

    def remote_size(self, variant: Variant) -> int:
        """Size of a file on Instagram's servers (used to compare versions of equal resolution)."""
        if variant.size:
            return variant.size
        if variant.url not in self._size_cache:
            size = 0
            try:
                response = self.http.request("HEAD", variant.url, headers={"Accept": "*/*",
                                                                          "Accept-Encoding": "identity"})
                if response.ok:
                    size = int(response.headers.get("Content-Length") or 0)
            except (NetworkError, ValueError):
                pass
            self._size_cache[variant.url] = size
        return self._size_cache[variant.url]

    def best_variant(self, item: MediaItem, limit: int = 0) -> Optional[Variant]:
        return pick_variant(item.variants, limit=limit, allow_merge=bool(self.ffmpeg), file_size=self.remote_size)

    def _choose(self, item: MediaItem, choice: Choice, limit: int) -> Variant:
        if not item.variants:
            raise DownloadError(f"Instagram didn't provide a downloadable {item.kind} "
                                "(it may only be available to logged-in users)")
        if isinstance(choice, Variant) and choice in item.variants:
            return choice
        if isinstance(choice, int):
            limit = choice
        variant = self.best_variant(item, limit)
        if not self.ffmpeg and not self._ffmpeg_tip_shown:
            better = pick_variant(item.variants, limit=limit)
            if better is not None and better.pixels > variant.pixels:
                self._ffmpeg_tip_shown = True
                self.out(f"  Tip: install ffmpeg to get videos in higher quality (this one is available in "
                         f"{better.resolution}, without ffmpeg {variant.resolution}).")
        return variant

    # ------------------------------------------------------------------ downloading

    def download(self, post: Post, options: DownloadOptions) -> DownloadResult:
        result = DownloadResult()
        items = self._apply_filter(post, options.media_filter, result)
        if result.filtered:
            kept = "photos" if options.media_filter == "photos" else "videos"
            skipped = "video" if kept == "photos" else "photo"
            self.out(f"  Skipping {result.filtered} {skipped}{'s' if result.filtered != 1 else ''} "
                     f"(downloading {kept} only).")
        if not items:
            return result

        folder = self.output_dir
        limit = int(options.quality) if options.quality.isdigit() else 0
        choices: dict[str, Choice] = {}
        if options.quality == "ask" and self.chooser is not None:
            for kind in (PHOTO, VIDEO):
                group = [item for item in items if item.kind == kind and item.variants]
                if group:
                    choices[kind] = self.chooser(post, kind, group, bool(self.ffmpeg))

        for item in items:
            label = f"  [{item.position}/{len(post.items)}] {item.kind}"
            try:
                variant = self._choose(item, choices.get(item.kind), limit)
                stem = self.stem_for(post, item)
                if variant != self.best_variant(item):
                    stem += f"_{variant.width}x{variant.height}"
                    if variant.dash and variant.bandwidth:
                        stem += f"_{variant.bandwidth // 1000}k"
                self._save(post, item, variant, folder / (stem + _extension(variant, item.kind)), label, result)
            except DownloadError as err:
                self._finish_line(f"{label}: FAILED - {err}")
                result.failed.append(f"{post.url} ({item.kind} {item.position}): {err}")

        if options.save_captions and post.kind == "post" and post.caption:
            self._save_caption(post, folder)
        return result

    def _apply_filter(self, post: Post, media_filter: str, result: DownloadResult) -> list[MediaItem]:
        selected = []
        for item in post.items:
            if media_filter == "photos" and item.kind == VIDEO:
                photo = item.as_photo()  # a photo that was posted with music
                if photo is not None:
                    selected.append(photo)
                else:
                    result.filtered += 1
            elif media_filter == "videos" and item.kind == PHOTO:
                result.filtered += 1
            else:
                selected.append(item)
        return selected

    def _save(self, post: Post, item: MediaItem, variant: Variant, target: Path, label: str,
              result: DownloadResult) -> None:
        if target.exists() and target.stat().st_size > 0:
            result.existing.append(target)
            self.out(f"{label}: already downloaded ({target.name})")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        description = f"{label} {variant.resolution}"
        note = ""
        if variant.needs_merge and self.ffmpeg:
            size = self._download_merged(variant, target, description)
        else:
            if variant.needs_merge:
                note = "  (no sound: install ffmpeg to merge the audio)"
            size = self._download_file(variant.url, target, description)
        timestamp = item.taken_at or post.taken_at
        if timestamp:
            os.utime(target, (timestamp, timestamp))
        result.saved.append(target)
        self._finish_line(f"{description} -> {target.name} ({format_size(size)}){note}")

    def _download_file(self, url: str, target: Path, description: str) -> int:
        part = target.with_name(target.name + ".part")
        try:
            size = self._fetch(url, part, description)
            os.replace(part, target)
            return size
        finally:
            _remove(part)

    def _download_merged(self, variant: Variant, target: Path, description: str) -> int:
        video = target.with_name(target.name + ".video.part")
        audio = target.with_name(target.name + ".audio.part")
        merged = target.with_name(target.name + ".part")
        try:
            self._fetch(variant.url, video, description + " video")
            self._fetch(variant.audio_url, audio, description + " audio")
            self._status(f"{description} ... merging video and audio")
            merge_streams(self.ffmpeg, video, audio, merged)
            os.replace(merged, target)
            return target.stat().st_size
        finally:
            _remove(video, audio, merged)

    def _fetch(self, url: str, destination: Path, description: str) -> int:
        last_error = ""
        for attempt in range(1, 4):
            try:
                with self.http.open_stream(url) as response, open(destination, "wb") as fh:
                    total = int(response.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        self._progress(description, done, total)
                if total and done < total:
                    raise DownloadError(f"connection dropped ({format_size(done)} of {format_size(total)})")
                return done
            except urllib.error.HTTPError as err:
                err.close()
                if err.code in (403, 404, 410):
                    raise DownloadError(f"Instagram refused the file (HTTP {err.code}); its link may have "
                                        "expired, try again") from err
                last_error = f"HTTP {err.code}"
            except (NetworkError, DownloadError, OSError, http.client.HTTPException) as err:
                last_error = str(err) or type(err).__name__
            if attempt < 3:
                time.sleep(2 * attempt)
        raise DownloadError(f"download failed ({last_error})")

    def _save_caption(self, post: Post, folder: Path) -> None:
        path = folder / f"{_post_stem(post)}.txt"
        if path.exists():
            return
        when = datetime.fromtimestamp(post.taken_at).strftime("%Y-%m-%d %H:%M") if post.taken_at else ""
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{post.url}\n@{post.username}  {when}\n\n{post.caption}\n", encoding="utf-8")

    # ------------------------------------------------------------------ console output

    def _progress(self, description: str, done: int, total: int) -> None:
        if not self.show_progress:
            return
        now = time.monotonic()
        if now - self._last_progress < 0.25 and done != total:
            return
        self._last_progress = now
        if total:
            self._status(f"{description} ... {done * 100 // total}% of {format_size(total)}")
        else:
            self._status(f"{description} ... {format_size(done)}")

    def _status(self, text: str) -> None:
        if not self.show_progress:
            return
        sys.stdout.write("\r" + text.ljust(self._status_width))
        sys.stdout.flush()
        self._status_width = max(self._status_width, len(text))

    def _finish_line(self, text: str) -> None:
        if self._status_width:
            sys.stdout.write("\r" + " " * self._status_width + "\r")
            sys.stdout.flush()
            self._status_width = 0
        self.out(text)
