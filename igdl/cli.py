"""Interactive menu and command-line interface."""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import __version__
from .api import Checkpoint, InstagramAPI, InstagramError, LoginRequired, NotFound, RateLimited, SessionExpired
from .auth import (CookieImportError, delete_session, load_cookie_file, load_firefox_cookies, load_session,
                   parse_cookie_text, save_session, user_id_from_sessionid)
from .download import Choice, DownloadOptions, DownloadResult, Downloader, find_ffmpeg
from .links import Link, extract_links, parse_link, pk_to_shortcode, post_url
from .media import PHOTO, VIDEO, MediaItem, Post, post_from_media, post_from_reel
from .net import HttpClient
from .settings import MEDIA_FILTER_LABELS, MEDIA_FILTERS, Settings, normalize_quality, quality_label

LINE = "=" * 64
DOWNLOADABLE_KINDS = ("post", "share", "story", "highlight")


class Cancelled(Exception):
    """The user closed the input (Ctrl+Z / Ctrl+D) while being asked something."""


class UnsupportedLink(InstagramError):
    """A link this program can't download from (retrying it later won't help)."""


# ---------------------------------------------------------------------------- prompts


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt)
    except EOFError:
        raise Cancelled from None
    return answer.strip() or default


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    while True:
        answer = ask(prompt + (" [Y/n]: " if default else " [y/N]: ")).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def ask_number(prompt: str, low: int, high: int, default: int | None = None) -> int | None:
    while True:
        answer = ask(prompt)
        if not answer:
            return default
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)
        print(f"Please enter a number from {low} to {high}.")


def _more_input_waiting() -> bool:
    """True while the rest of a multi-line paste is still waiting to be read."""
    if not sys.stdin.isatty():
        return False
    time.sleep(0.05)
    if os.name == "nt":
        import msvcrt
        return msvcrt.kbhit()
    import select
    return bool(select.select([sys.stdin], [], [], 0)[0])


def read_lines(prompt: str) -> list[str]:
    """Read lines until an empty line. Blank lines inside a pasted block don't end the input."""
    lines: list[str] = []
    while True:
        try:
            line = input(prompt)
        except EOFError:
            break
        if line.strip():
            lines.append(line)
        elif not _more_input_waiting():
            break
    return lines


def parse_selection(text: str, count: int) -> list[int] | None:
    """Parse "1,3,5-7" into zero-based indexes. Returns None if the text isn't valid."""
    chosen: list[int] = []
    for part in re.split(r"[\s,]+", text.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if not 1 <= start <= end <= count:
            return None
        chosen.extend(index - 1 for index in range(start, end + 1) if index - 1 not in chosen)
    return chosen or None


def collect_links(lines: list[str]) -> tuple[list[str], list[str]]:
    """Links from pasted lines; a line can also be the path of a text file with links."""
    urls: list[str] = []
    ignored: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        found = extract_links(line)
        if not found:
            found = links_from_file(line.strip("\"'"))
            if found is None:
                ignored.append(line)
                continue
        urls.extend(found)
    return list(dict.fromkeys(urls)), ignored


def links_from_file(path: str) -> list[str] | None:
    """Downloadable links inside a text/CSV file, or None if the file doesn't exist."""
    try:
        file = Path(path).expanduser()
        if not file.is_file():
            return None
        text = file.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return None
    # Keep links to content only (a CSV of saved posts also mentions profiles in captions).
    return [url for url in extract_links(text) if (link := parse_link(url)) and link.kind in DOWNLOADABLE_KINDS]


def choose_quality(post: Post, kind: str, items: list[MediaItem], can_merge: bool) -> Choice:
    """Ask which resolution to download ("let me choose" quality mode)."""
    noun = "photo" if kind == PHOTO else "video"
    if len(items) == 1:
        options = [v for v in items[0].variants if can_merge or not v.needs_merge]
        if len(options) <= 1:
            return None
        print(f"  Available versions of this {noun}, from highest to lowest quality:")
        for number, variant in enumerate(options, 1):
            print(f"    {number:>2}. {variant.describe()}")
        number = ask_number(f"  Choose 1-{len(options)} [Enter = 1]: ", 1, len(options), 1)
        return options[number - 1]

    levels = sorted({v.short_side for item in items for v in item.variants
                     if v.short_side and (can_merge or not v.needs_merge)}, reverse=True)
    if len(levels) <= 1:
        return None
    print(f"  This post has {len(items)} {noun}s. Available sizes:")
    for number, level in enumerate(levels, 1):
        example = next(v for item in items for v in item.variants if v.short_side == level)
        print(f"    {number:>2}. up to {level}p (e.g. {example.resolution})")
    number = ask_number(f"  Choose 1-{len(levels)} [Enter = 1]: ", 1, len(levels), 1)
    return None if number == 1 else levels[number - 1]


def _date(timestamp: int, with_time: bool = False) -> str:
    if not timestamp:
        return "unknown date"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def hint_for(err: Exception) -> str:
    if isinstance(err, SessionExpired):
        return ("\n  Log in again (main menu > Log in, or --login). To continue with public posts only, "
                "log out instead.")
    if isinstance(err, LoginRequired):
        return "\n  Log in first: main menu > Log in (or run with --login)."
    return ""


def saved_row(media: dict) -> dict[str, str]:
    code = media.get("code") or pk_to_shortcode(media.get("pk") or 0)
    is_reel = media.get("product_type") == "clips"
    kind = "reel" if is_reel else {1: "photo", 2: "video", 8: "carousel"}.get(media.get("media_type"), "post")
    caption = media.get("caption")
    text = (caption.get("text") or "") if isinstance(caption, dict) else ""
    text = " ".join(text.split())
    return {
        "url": post_url(code, is_reel),
        "username": (media.get("user") or {}).get("username") or "",
        "type": kind,
        "items": str(len(media.get("carousel_media") or []) or 1),
        "posted": _date(int(media.get("taken_at") or 0), with_time=True) if media.get("taken_at") else "",
        "caption": text[:200] + ("..." if len(text) > 200 else ""),
    }


# ---------------------------------------------------------------------------- application


class App:
    def __init__(self, settings: Settings, interactive: bool = True):
        self.settings = settings
        self.interactive = interactive
        self.http = HttpClient(settings.user_agent)
        self.api = InstagramAPI(self.http, settings.min_delay, settings.max_delay, log=print)
        self.ffmpeg = find_ffmpeg(settings.ffmpeg_path)
        self.username: str | None = None
        self.had_errors = False
        session = load_session()
        if session:
            cookies, self.username = session
            self.http.set_cookies(cookies)

    # ------------------------------------------------------------------ helpers

    def downloader(self) -> Downloader:
        return Downloader(self.http, self.settings.output_dir, self.ffmpeg, chooser=choose_quality)

    def default_options(self) -> DownloadOptions:
        return DownloadOptions(self.settings.media_filter, self.settings.quality, self.settings.save_captions)

    def require_login(self, what: str) -> None:
        if not self.api.logged_in:
            raise LoginRequired(f"You need to be logged in to download {what}.")

    def close(self) -> None:
        # Keep the saved session up to date with cookies Instagram refreshed during this run.
        if self.api.logged_in and load_session() is not None:
            save_session(self.http.instagram_cookies(), self.username)

    # ------------------------------------------------------------------ resolving links

    def posts_for_link(self, link: Link) -> Iterator[Post]:
        if link.kind == "share":
            target = self.api.resolve_share_link(link.url)
            print(f"  -> {target}")
            resolved = parse_link(target)
            if resolved is None or resolved.kind == "share":
                raise NotFound("Couldn't open this share link.")
            yield from self.posts_for_link(resolved)
        elif link.kind == "post":
            yield post_from_media(self.api.media(link.shortcode))
        elif link.kind == "story":
            post = self.story_post(link.username, link.story_id)
            if post is not None:
                yield post
        elif link.kind == "highlight":
            self.require_login("highlights")
            reels = self.api.reels([f"highlight:{link.highlight_id}"])
            if not reels or not reels[0].get("items"):
                raise NotFound("This highlight is empty or no longer available.")
            yield post_from_reel(reels[0], "highlight")
        elif link.kind in ("saved", "collection"):
            self.require_login("saved posts")
            for media in self.api.saved_media(link.collection_id or None):
                yield self.post_from_saved(media)
        elif link.kind == "profile":
            raise UnsupportedLink(f"Profile links aren't supported. Paste links to specific posts, or use the "
                                  f"stories / highlights options for @{link.username}.")
        else:
            raise UnsupportedLink("This kind of Instagram link isn't supported.")

    def story_post(self, username: str, story_id: str = "") -> Post | None:
        self.require_login("stories")
        reels = self.api.reels([self.api.user_id(username)])
        reel = reels[0] if reels else {"user": {"username": username}}
        items = reel.get("items") or []
        if story_id:
            items = [node for node in items if str(node.get("pk") or str(node.get("id", "")).split("_")[0]) == story_id]
            if not items:
                try:  # no longer in the user's current stories, but maybe still reachable directly
                    items = [self.api.media_by_id(story_id)]
                except NotFound:
                    raise NotFound("This story isn't available any more (stories disappear after 24 hours).") from None
            reel = {**reel, "items": items}
        if not items:
            print(f"  @{username} has no active stories right now (or the account is private).")
            return None
        post = post_from_reel(reel, "story")
        if post.username == "unknown":
            post.username = username
        return post

    def post_from_saved(self, media: dict) -> Post:
        post = post_from_media(media)
        # Saved-post lists sometimes leave out the video files; fetch the full post if so.
        if post.shortcode and any(item.kind == VIDEO and not item.variants for item in post.items):
            post = post_from_media(self.api.media(post.shortcode))
        return post

    # ------------------------------------------------------------------ downloading

    def save_post(self, post: Post, downloader: Downloader, options: DownloadOptions) -> DownloadResult:
        if post.kind == "post":
            print(f"  @{post.username} - {_date(post.taken_at)} - {post.summary()}")
        elif post.kind == "highlight":
            print(f"  @{post.username} - highlight \"{post.title}\" - {post.summary()}")
        else:
            print(f"  @{post.username} - stories - {post.summary()}")
        if not post.items:
            print("  Nothing to download in this post.")
            return DownloadResult()
        return downloader.download(post, options)

    def download_links(self, urls: list[str], options: DownloadOptions) -> None:
        downloader = self.downloader()
        total = DownloadResult()
        problems: list[tuple[str, str]] = []
        retry: list[str] = []  # links worth trying again later
        # Logged out, Instagram sometimes limits by quietly returning nothing instead of saying so.
        empty_in_a_row = 0
        for index, url in enumerate(urls, 1):
            print(f"\n[{index}/{len(urls)}] {url}")
            try:
                link = parse_link(url)
                if link is None:
                    raise UnsupportedLink("Not a supported Instagram link (posts, reels, stories and highlights "
                                          "are supported).")
                failed_before = len(total.failed)
                for post in self.posts_for_link(link):
                    total.add(self.save_post(post, downloader, options))
                if len(total.failed) > failed_before:
                    problems.append((url, "some files couldn't be downloaded"))
                    retry.append(url)
                empty_in_a_row = 0
            except (RateLimited, SessionExpired, Checkpoint) as err:
                print(f"  ERROR: {err}{hint_for(err)}")
                problems.append((url, str(err)))
                remaining = urls[index:]
                if remaining:
                    print(f"  Stopping here: the remaining {len(remaining)} link(s) would fail the same way.")
                    problems.extend((rest, "not attempted") for rest in remaining)
                retry += [url, *remaining]
                break
            except InstagramError as err:
                print(f"  ERROR: {err}{hint_for(err)}")
                problems.append((url, str(err)))
                if not isinstance(err, UnsupportedLink):
                    retry.append(url)
                empty_in_a_row = empty_in_a_row + 1 if isinstance(err, NotFound) and not self.api.logged_in else 0
                remaining = urls[index:]
                if empty_in_a_row >= 2 and remaining:
                    print("  Two posts in a row came back empty, which usually means Instagram is limiting "
                          f"logged-out visitors. Stopping here; the remaining {len(remaining)} link(s) are kept "
                          "for retrying. Logging in avoids this.")
                    problems.extend((rest, "not attempted") for rest in remaining)
                    retry += remaining
                    break
        self.print_summary(total, problems, f"{len(urls)} link{'s' if len(urls) != 1 else ''}")
        if retry:
            self.offer_to_save_failed(retry)

    def print_summary(self, result: DownloadResult, problems: list[tuple[str, str]], what: str) -> None:
        parts = [f"{len(result.saved)} file{'s' if len(result.saved) != 1 else ''} saved"]
        if result.existing:
            parts.append(f"{len(result.existing)} already downloaded")
        if result.filtered:
            parts.append(f"{result.filtered} skipped by the photos/videos filter")
        if problems:
            parts.append(f"{len(problems)} with problems")
            self.had_errors = True
        print("\n" + "-" * 64)
        print(f"Done: {what} - " + ", ".join(parts))
        if result.saved or result.existing:
            print(f"Folder: {self.settings.output_dir}")
        if problems:
            print("Problems:")
            for url, reason in problems:
                print(f"  {url}\n    {reason}")

    def offer_to_save_failed(self, urls: list[str]) -> None:
        if self.interactive and not ask_yes_no("Save these links to a file so you can retry them later?"):
            return
        folder = Path(self.settings.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"failed_links_{datetime.now():%Y-%m-%d_%H%M%S}.txt"
        path.write_text("\n".join(dict.fromkeys(urls)) + "\n", encoding="utf-8")
        print(f"Saved to {path}  (paste this file's path into 'Download posts' to retry)")

    def download_stories(self, usernames: list[str], options: DownloadOptions) -> None:
        self.require_login("stories")
        downloader = self.downloader()
        total = DownloadResult()
        problems: list[tuple[str, str]] = []
        for index, name in enumerate(usernames, 1):
            link = parse_link(name) if "/" in name else Link("story", "", username=name.lstrip("@"))
            if link is None or link.kind != "story":
                problems.append((name, "not a username or story link"))
                continue
            print(f"\n[{index}/{len(usernames)}] @{link.username}")
            try:
                post = self.story_post(link.username, link.story_id)
                if post is not None:
                    total.add(self.save_post(post, downloader, options))
            except (RateLimited, SessionExpired, Checkpoint) as err:
                print(f"  ERROR: {err}{hint_for(err)}")
                problems.append((name, str(err)))
                break
            except InstagramError as err:
                print(f"  ERROR: {err}")
                problems.append((name, str(err)))
        self.print_summary(total, problems, f"{len(usernames)} account{'s' if len(usernames) != 1 else ''}")

    def download_following_stories(self, options: DownloadOptions) -> None:
        self.require_login("stories")
        print("Checking which accounts you follow have stories...")
        tray = [entry for entry in self.api.stories_tray()
                if str(entry.get("id", "")).isdigit() and isinstance(entry.get("user"), dict)]
        if not tray:
            print("Nobody you follow has active stories right now.")
            return
        names = [entry["user"].get("username", "?") for entry in tray]
        print(f"{len(tray)} accounts have stories: " + ", ".join("@" + n for n in names[:12])
              + (", ..." if len(names) > 12 else ""))
        if self.interactive and not ask_yes_no("Download all of them?", default=True):
            return
        downloader = self.downloader()
        total = DownloadResult()
        problems: list[tuple[str, str]] = []
        user_ids = [str(entry["id"]) for entry in tray]
        try:
            for start in range(0, len(user_ids), 5):
                for reel in self.api.reels(user_ids[start:start + 5]):
                    print()
                    total.add(self.save_post(post_from_reel(reel, "story"), downloader, options))
        except InstagramError as err:
            print(f"  ERROR: {err}{hint_for(err)}")
            problems.append(("stories", str(err)))
        self.print_summary(total, problems, f"stories of {len(tray)} accounts")

    def download_highlights(self, username: str, options: DownloadOptions) -> None:
        self.require_login("highlights")
        username = username.strip().lstrip("@")
        tray = self.api.highlights(self.api.user_id(username))
        if not tray:
            print(f"@{username} has no highlights.")
            return
        print(f"\n@{username} has {len(tray)} highlight{'s' if len(tray) != 1 else ''}:")
        for number, entry in enumerate(tray, 1):
            count = entry.get("media_count")
            print(f"  {number:>3}. {entry.get('title') or '(untitled)'}" + (f" ({count})" if count else ""))
        selected = list(range(len(tray)))
        if self.interactive:
            while True:
                answer = ask("Which ones? Numbers like 1,3,5-7, or Enter for all: ")
                if not answer:
                    break
                picked = parse_selection(answer, len(tray))
                if picked is not None:
                    selected = picked
                    break
                print(f"Please use numbers from 1 to {len(tray)}.")
        reel_ids = [str(tray[index]["id"]) for index in selected]
        reel_ids = [rid if rid.startswith("highlight:") else f"highlight:{rid}" for rid in reel_ids]
        downloader = self.downloader()
        total = DownloadResult()
        problems: list[tuple[str, str]] = []
        try:
            for start in range(0, len(reel_ids), 5):
                for reel in self.api.reels(reel_ids[start:start + 5]):
                    post = post_from_reel(reel, "highlight")
                    if post.username == "unknown":
                        post.username = username
                    print()
                    total.add(self.save_post(post, downloader, options))
        except InstagramError as err:
            print(f"  ERROR: {err}{hint_for(err)}")
            problems.append((username, str(err)))
        self.print_summary(total, problems, f"{len(reel_ids)} highlight{'s' if len(reel_ids) != 1 else ''}")

    # ------------------------------------------------------------------ saved posts

    def export_saved(self, collection_id: str | None = None) -> list[dict]:
        """Write the links of your saved posts to a .txt and a .csv file. Returns the media objects."""
        medias = self.collect_saved(collection_id)
        if not medias:
            return medias
        folder = Path(self.settings.output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"saved_collection_{collection_id}" if collection_id else "saved_posts"
        base = folder / f"{name}_{datetime.now():%Y-%m-%d_%H%M%S}"
        rows = [saved_row(media) for media in medias]
        txt_path, csv_path = base.with_suffix(".txt"), base.with_suffix(".csv")
        txt_path.write_text("\n".join(row["url"] for row in rows) + "\n", encoding="utf-8")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nExported {len(rows)} saved post link{'s' if len(rows) != 1 else ''} to:\n  {txt_path}\n  {csv_path}")
        return medias

    def collect_saved(self, collection_id: str | None = None) -> list[dict]:
        self.require_login("your saved posts")
        print("Collecting your saved posts (this takes a while if you have many)...")
        medias: list[dict] = []
        for media in self.api.saved_media(collection_id):
            medias.append(media)
            if len(medias) % 50 == 0:
                print(f"  {len(medias)} so far...")
        if not medias:
            print("No saved posts found.")
        return medias

    def download_saved(self, medias: list[dict], options: DownloadOptions) -> None:
        downloader = self.downloader()
        total = DownloadResult()
        problems: list[tuple[str, str]] = []
        for index, media in enumerate(medias, 1):
            url = saved_row(media)["url"]
            print(f"\n[{index}/{len(medias)}] {url}")
            try:
                total.add(self.save_post(self.post_from_saved(media), downloader, options))
            except (RateLimited, SessionExpired, Checkpoint) as err:
                print(f"  ERROR: {err}{hint_for(err)}")
                problems.append((url, str(err)))
                problems.extend((saved_row(rest)["url"], "not attempted") for rest in medias[index:])
                break
            except InstagramError as err:
                print(f"  ERROR: {err}")
                problems.append((url, str(err)))
        self.print_summary(total, problems, f"{len(medias)} saved post{'s' if len(medias) != 1 else ''}")
        if problems:
            self.offer_to_save_failed([url for url, _ in problems])

    # ------------------------------------------------------------------ listing resolutions

    def list_link(self, url: str) -> None:
        print(f"\n{url}")
        link = parse_link(url)
        if link is None:
            print("  Not a supported Instagram link.")
            return
        try:
            for post in self.posts_for_link(link):
                print(f"  @{post.username} - {post.summary()}")
                for item in post.items:
                    print(f"  [{item.position}/{len(post.items)}] {item.kind}")
                    for variant in item.variants or []:
                        extra = "  (video + audio, merged with ffmpeg)" if variant.needs_merge else ""
                        print(f"      {variant.describe()}{extra}")
                    if not item.variants:
                        print("      no downloadable file")
        except InstagramError as err:
            self.had_errors = True
            print(f"  ERROR: {err}{hint_for(err)}")

    # ------------------------------------------------------------------ logging in

    def login_with(self, method: str, value: str | None = None) -> None:
        if method == "firefox":
            cookies = load_firefox_cookies()
        elif method == "cookies":
            if not value:
                value = ask("Path of the cookies file (drag the file here): ").strip("\"'")
            cookies = load_cookie_file(value)
        elif method == "sessionid":
            print(SESSIONID_HELP)
            if not value:
                try:
                    value = getpass.getpass("Paste the sessionid value and press Enter (it stays hidden): ")
                except EOFError:
                    raise Cancelled from None
            cookies = parse_cookie_text(value)
        else:
            raise CookieImportError(f"Unknown login method: {method}")
        self.finish_login(cookies)

    def finish_login(self, cookies: dict[str, str]) -> None:
        sessionid = cookies.get("sessionid", "")
        if not sessionid:
            raise CookieImportError("No 'sessionid' cookie found, so there's no login to import.")
        if not cookies.get("ds_user_id") and user_id_from_sessionid(sessionid):
            cookies["ds_user_id"] = user_id_from_sessionid(sessionid)
        self.http.clear_cookies()
        self.http.set_cookies(cookies)
        print("Checking the login with Instagram...")
        try:
            username = self.api.current_user().get("username")
        except LoginRequired:
            self.http.clear_cookies()
            raise CookieImportError("Instagram didn't accept this login. Log in to instagram.com in your browser "
                                    "again, then import it here.") from None
        except RateLimited:
            print("Instagram is limiting requests right now, so the login couldn't be checked yet. "
                  "It's saved anyway and will be used for your downloads.")
            username = None
        except InstagramError as err:
            print(f"Couldn't check the login right now ({err}). It's saved anyway.")
            username = None
        self.username = username
        path = save_session(self.http.instagram_cookies(), username)
        print(f"Logged in as @{username}." if username else "Logged in.")
        print(f"(Session saved in {path}. Keep it private: it gives access to your account.)")

    def logout(self) -> None:
        delete_session()
        self.http.clear_cookies()
        self.username = None
        print("Logged out. (This only removes the session saved by this program; your browser stays logged in.)")

    # ------------------------------------------------------------------ interactive menu

    def run_menu(self) -> None:
        while True:
            self.print_header()
            print(MENU)
            try:
                choice = ask("Choose an option (or paste a link): ")
            except (Cancelled, KeyboardInterrupt):
                print()
                return
            if choice in ("0", "q", "quit", "exit"):
                return
            try:
                self.handle_menu_choice(choice)
            except Cancelled:
                print("\nCancelled.")
            except KeyboardInterrupt:
                print("\nStopped.")
            except (InstagramError, CookieImportError) as err:
                print(f"\nERROR: {err}{hint_for(err)}")
            try:
                ask("\nPress Enter to go back to the menu...")
            except (Cancelled, KeyboardInterrupt):
                print()
                return

    def handle_menu_choice(self, choice: str) -> None:
        actions = {
            "1": self.menu_download_links,
            "2": self.menu_stories,
            "3": self.menu_following_stories,
            "4": self.menu_highlights,
            "5": self.menu_saved,
            "6": self.menu_settings,
            "7": self.menu_login,
            "8": self.logout,
        }
        if choice in actions:
            actions[choice]()
            return
        lines = [choice]
        while _more_input_waiting():  # the rest of a multi-line paste
            lines += read_lines("")
        urls, _ = collect_links(lines)
        if urls:
            self.download_links(urls, self.confirm_options(len(urls)))
        else:
            print("Please choose one of the options above.")

    def print_header(self) -> None:
        account = f"@{self.username}" if self.username else "logged in"
        if not self.api.logged_in:
            account = "not logged in (public posts only)"
        ffmpeg = "found" if self.ffmpeg else "not found (videos limited to the single-file version)"
        print(f"\n{LINE}\n Instagram Downloader {__version__}\n{LINE}")
        print(f" Account : {account}")
        print(f" Folder  : {self.settings.output_dir}")
        print(f" Default : {MEDIA_FILTER_LABELS[self.settings.media_filter]}, "
              f"{quality_label(self.settings.quality).lower()}")
        print(f" ffmpeg  : {ffmpeg}")
        print(LINE)

    def confirm_options(self, count: int | None = None) -> DownloadOptions:
        options = self.default_options()
        ready = f"{count} link{'s' if count != 1 else ''} ready. " if count else ""
        print(f"\n{ready}Download: {MEDIA_FILTER_LABELS[options.media_filter]} | "
              f"Quality: {quality_label(options.quality)}")
        if ask("Press Enter to start, or type o to change these options: ").lower().startswith("o"):
            options = DownloadOptions(ask_media_filter(options.media_filter), ask_quality(options.quality),
                                      options.save_captions)
        return options

    def menu_download_links(self) -> None:
        print("\nPaste one or more Instagram links (posts, reels, stories, highlights).")
        print("One per line or separated by spaces. You can also enter the path of a .txt file")
        print("with links (for example an exported saved-posts list).")
        print("Press Enter on an empty line to start.\n")
        urls, ignored = collect_links(read_lines("link> "))
        for line in ignored:
            print(f"  Ignored (not an Instagram link or an existing file): {line}")
        if not urls:
            print("No links entered.")
            return
        self.download_links(urls, self.confirm_options(len(urls)))

    def menu_stories(self) -> None:
        self.require_login("stories")
        answer = ask("\nUsername(s) or story link(s), separated by spaces or commas: ")
        names = [name for name in re.split(r"[\s,]+", answer) if name]
        if not names:
            print("No usernames entered.")
            return
        self.download_stories(names, self.confirm_options())

    def menu_following_stories(self) -> None:
        self.require_login("stories")
        self.download_following_stories(self.confirm_options())

    def menu_highlights(self) -> None:
        self.require_login("highlights")
        username = ask("\nUsername whose highlights you want: ").lstrip("@")
        if not username:
            print("No username entered.")
            return
        self.download_highlights(username, self.confirm_options())

    def menu_saved(self) -> None:
        self.require_login("your saved posts")
        print("\nThis exports the links of your saved posts to a .txt file (one link per line)")
        print("and a .csv file (with user, date and caption) in your download folder.")
        answer = ask("Press Enter for all saved posts, or paste the link of one of your collections: ")
        collection_id = None
        if answer:
            link = parse_link(answer)
            if link is None or link.kind not in ("saved", "collection"):
                print("That isn't a link to a saved collection "
                      "(it looks like instagram.com/<you>/saved/<name>/<number>/).")
                return
            collection_id = link.collection_id or None
        medias = self.export_saved(collection_id)
        if medias and ask_yes_no(f"Download all {len(medias)} of these posts now?"):
            self.download_saved(medias, self.confirm_options())

    def menu_login(self) -> None:
        print(LOGIN_MENU)
        choice = ask_number("Choose: ", 0, 3, 0)
        if choice:
            self.login_with({1: "firefox", 2: "cookies", 3: "sessionid"}[choice])

    def menu_settings(self) -> None:
        while True:
            s = self.settings
            print(f"\nSettings (saved in {Settings.path()})")
            print(f"  1. Download folder ........... {s.output_dir}")
            print(f"  2. Default media type ........ {MEDIA_FILTER_LABELS[s.media_filter]}")
            print(f"  3. Default quality ........... {quality_label(s.quality)}")
            print(f"  4. Save captions as .txt ..... {'On' if s.save_captions else 'Off'}")
            print(f"  5. Delay between requests .... {s.min_delay:g}-{s.max_delay:g} seconds")
            print(f"  6. ffmpeg .................... {self.ffmpeg or 'not found'}")
            print("  0. Back")
            choice = ask_number("Choose: ", 0, 6, 0)
            if not choice:
                return
            if choice == 1:
                folder = ask("New download folder (Enter = keep current): ").strip("\"'")
                if folder:
                    self.update_setting(output_dir=str(Path(folder).expanduser().resolve()))
            elif choice == 2:
                self.update_setting(media_filter=ask_media_filter(s.media_filter))
            elif choice == 3:
                self.update_setting(quality=ask_quality(s.quality))
            elif choice == 4:
                self.update_setting(save_captions=not s.save_captions)
            elif choice == 5:
                print("Instagram can temporarily block accounts that make too many requests too fast.")
                print("Waiting a few seconds between requests keeps your account safe. Recommended: 4-8.")
                answer = ask("Minimum and maximum seconds, e.g. 4-8 (Enter = keep current): ")
                numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", answer)]
                if numbers:
                    self.update_setting(min_delay=numbers[0], max_delay=max(numbers[-1], numbers[0]))
                    self.api.min_delay, self.api.max_delay = s.min_delay, s.max_delay
            elif choice == 6:
                path = ask("Path to ffmpeg (or its folder); Enter = find it automatically: ").strip("\"'")
                found = find_ffmpeg(path)
                if path and not found:
                    print("ffmpeg wasn't found there; keeping the previous setting.")
                else:
                    self.update_setting(ffmpeg_path=path)
                    self.ffmpeg = found

    def update_setting(self, **changes) -> None:
        """Change settings for this session and save them, without saving one-off command-line options."""
        stored = Settings.load()
        for name, value in changes.items():
            setattr(self.settings, name, value)
            setattr(stored, name, value)
        stored.save()


def ask_media_filter(current: str) -> str:
    print("\nWhat do you want to download?")
    for number, key in enumerate(MEDIA_FILTERS, 1):
        print(f"  {number}. {MEDIA_FILTER_LABELS[key]}")
    default = MEDIA_FILTERS.index(current) + 1
    return MEDIA_FILTERS[ask_number(f"Choose [Enter = {default}]: ", 1, len(MEDIA_FILTERS), default) - 1]


def ask_quality(current: str) -> str:
    print("\nWhich quality?")
    print("  1. Highest available")
    print("  2. Let me choose for each post (shows the available resolutions)")
    print("  3. Limit to a maximum resolution (for example 1080, 720 or 480)")
    default = 1 if current == "best" else 2 if current == "ask" else 3
    choice = ask_number(f"Choose [Enter = {default}]: ", 1, 3, default)
    if choice == 1:
        return "best"
    if choice == 2:
        return "ask"
    fallback = current if current.isdigit() else "1080"
    while True:
        value = normalize_quality(ask(f"Maximum resolution (shorter side in pixels) [Enter = {fallback}]: ", fallback))
        if value and value.isdigit():
            return value
        print("Please enter a number such as 1080, 720 or 480.")


MENU = """
  1. Download posts / reels / carousels (paste one or more links)
  2. Download stories of a user
  3. Download stories of everyone I follow
  4. Download highlights of a user
  5. Export the links of my saved posts
  6. Settings
  7. Log in
  8. Log out
  0. Exit
"""

LOGIN_MENU = """
Log in by importing the Instagram session of a browser where you're already logged in.
Your password is never needed. Logging in is required for stories, highlights, saved
posts and private accounts you follow, and makes downloads more reliable.

  1. Import from Firefox (automatic)
  2. Import a cookies file (cookies.txt or JSON, exported with a browser extension)
  3. Paste the 'sessionid' cookie (works with Chrome, Edge and any other browser)
  0. Back
"""

SESSIONID_HELP = """
How to copy your sessionid:
  1. Open https://www.instagram.com in your browser and make sure you're logged in.
  2. Press F12. Open the "Application" tab (Firefox: "Storage"), then
     Cookies > https://www.instagram.com.
  3. Click the row named "sessionid" and copy its Value.
Treat it like a password: anyone who has it can use your account.
"""


# ---------------------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instagram_downloader",
        description="Download Instagram photos and videos (posts, reels, carousels, stories, highlights) "
                    "and export the links of your saved posts. Run without arguments for the menu.",
    )
    parser.add_argument("links", nargs="*", help="links to posts, reels, stories or highlights")
    parser.add_argument("-f", "--file", action="append", default=[], metavar="FILE",
                        help="text file with links (one per line); can be repeated")
    parser.add_argument("-s", "--stories", nargs="+", metavar="USER", help="download the current stories of users")
    parser.add_argument("--following-stories", action="store_true",
                        help="download the current stories of everyone you follow")
    parser.add_argument("--highlights", nargs="+", metavar="USER", help="download all highlights of users")
    parser.add_argument("--export-saved", action="store_true",
                        help="export the links of your saved posts to .txt and .csv files")
    parser.add_argument("--download-saved", action="store_true", help="download all your saved posts")
    parser.add_argument("--collection", metavar="LINK",
                        help="with --export-saved / --download-saved: only this saved collection")
    parser.add_argument("-t", "--type", choices=MEDIA_FILTERS, help="download everything (all), photos or videos")
    parser.add_argument("-q", "--quality",
                        help="'best' (default), 'ask' to choose for each post, or a maximum resolution like 720")
    parser.add_argument("-l", "--list", action="store_true",
                        help="only list the available resolutions of the links, don't download")
    parser.add_argument("-o", "--output", metavar="DIR", help="download folder for this run")
    parser.add_argument("--captions", action="store_true", help="also save captions as .txt files")
    parser.add_argument("--login", choices=("firefox", "cookies", "sessionid"),
                        help="log in by importing your browser's Instagram session")
    parser.add_argument("--cookies", metavar="FILE", help="log in with this cookies.txt / JSON cookie file")
    parser.add_argument("--logout", action="store_true", help="forget the saved login")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def run_command_line(app: App, args: argparse.Namespace) -> None:
    if args.logout:
        app.logout()
    if args.login or args.cookies:
        app.login_with(args.login or "cookies", args.cookies)

    options = app.default_options()
    urls, ignored = collect_links(args.links)
    for line in ignored:
        print(f"Ignored (not an Instagram link): {line}")
    for file in args.file:
        found = links_from_file(file)
        if found is None:
            raise InstagramError(f"File not found: {file}")
        urls += [url for url in found if url not in urls]
    if urls:
        if args.list:
            for url in urls:
                app.list_link(url)
        else:
            app.download_links(urls, options)
    if args.stories:
        app.download_stories(args.stories, options)
    if args.following_stories:
        app.download_following_stories(options)
    for user in args.highlights or []:
        app.download_highlights(user, options)
    if args.export_saved or args.download_saved:
        collection_id = None
        if args.collection:
            link = parse_link(args.collection)
            if link is None or link.kind != "collection":
                raise InstagramError("--collection needs a link like instagram.com/<you>/saved/<name>/<number>/")
            collection_id = link.collection_id
        medias = app.export_saved(collection_id) if args.export_saved else app.collect_saved(collection_id)
        if args.download_saved and medias:
            app.download_saved(medias, options)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on emoji when output is redirected
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    # One-off options for this run (never written to the settings file).
    if args.output:
        settings.output_dir = str(Path(args.output).expanduser().resolve())
    if args.type:
        settings.media_filter = args.type
    if args.captions:
        settings.save_captions = True
    if args.quality:
        if normalize_quality(args.quality) is None:
            print(f"ERROR: invalid --quality {args.quality!r}: use best, ask, or a number like 720.")
            return 2
        settings.quality = normalize_quality(args.quality)
    actions = (args.links, args.file, args.stories, args.following_stories, args.highlights,
               args.export_saved, args.download_saved, args.login, args.cookies, args.logout)
    app = App(settings, interactive=not any(actions))
    try:
        if app.interactive:
            app.run_menu()
        else:
            run_command_line(app, args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Cancelled:
        print("\nCancelled.")
        return 1
    except (InstagramError, CookieImportError) as err:
        print(f"ERROR: {err}{hint_for(err)}")
        return 1
    finally:
        app.close()
    return 1 if app.had_errors else 0
