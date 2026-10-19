"""Logging in by importing the Instagram session of a browser where you're already logged in.

Your password is never asked for or stored; only the session cookies are kept, in your
per-user settings folder.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from .settings import config_dir


class CookieImportError(Exception):
    pass


def user_id_from_sessionid(sessionid: str) -> str | None:
    """The account ID is the first part of the sessionid cookie ("<id>%3A<token>%3A...")."""
    first = urllib.parse.unquote(sessionid).split(":", 1)[0]
    return first if first.isdigit() else None


def parse_cookie_text(text: str) -> dict[str, str]:
    """Accept a raw sessionid value, or a whole cookie string "name=value; name2=value2"."""
    text = text.strip().strip("\"'")
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    if "=" not in text:
        return {"sessionid": text} if text else {}
    cookies = {}
    for part in text.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            cookies[name.strip()] = value.strip().strip('"')
    return cookies


def load_cookie_file(path: str | Path) -> dict[str, str]:
    """Read instagram.com cookies from a cookies.txt (Netscape format) or JSON cookie export."""
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8-sig", errors="replace")
    except OSError as err:
        raise CookieImportError(f"Couldn't read {path}: {err.strerror or err}") from err

    cookies: dict[str, str] = {}
    if content.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(content)
        except ValueError as err:
            raise CookieImportError(f"{path} looks like JSON but couldn't be parsed.") from err
        entries = data.get("cookies", []) if isinstance(data, dict) else data
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and str(entry.get("domain", "")).lstrip(".").endswith("instagram.com"):
                if entry.get("name"):
                    cookies[str(entry["name"])] = str(entry.get("value", ""))
    else:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7 and fields[0].lstrip(".").endswith("instagram.com"):
                cookies[fields[5]] = fields[6]
    if not cookies.get("sessionid"):
        raise CookieImportError(
            f"No Instagram login found in {path}. Make sure you were logged in to instagram.com "
            "when you exported the cookies.")
    return cookies


# ---------------------------------------------------------------------------- Firefox


def firefox_profile_dirs() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        if os.environ.get("APPDATA"):
            roots.append(Path(os.environ["APPDATA"]) / "Mozilla" / "Firefox" / "Profiles")
        if os.environ.get("LOCALAPPDATA"):  # Microsoft Store version of Firefox
            roots.extend(Path(os.environ["LOCALAPPDATA"], "Packages").glob(
                "Mozilla.Firefox_*/LocalCache/Roaming/Mozilla/Firefox/Profiles"))
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles")
    else:
        roots.extend([
            Path.home() / ".mozilla" / "firefox",
            Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
            Path.home() / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
        ])
    profiles = []
    for root in roots:
        if root.is_dir():
            profiles.extend(child for child in root.iterdir() if (child / "cookies.sqlite").is_file())
    return profiles


def _read_firefox_cookies(db_path: Path) -> list[tuple]:
    # Firefox keeps the database locked and may hold recent changes in the -wal file,
    # so read a private copy of both.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copyfile(db_path, copy)
        for suffix in ("-wal", "-shm"):
            extra = db_path.with_name(db_path.name + suffix)
            if extra.exists():
                shutil.copyfile(extra, copy.with_name(copy.name + suffix))
        connection = sqlite3.connect(copy)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(moz_cookies)")}
            origin = "originAttributes" if "originAttributes" in columns else "''"
            return connection.execute(
                f"SELECT name, value, {origin}, lastAccessed FROM moz_cookies "
                "WHERE host LIKE '%instagram.com'").fetchall()
        finally:
            connection.close()


def load_firefox_cookies(profiles: list[Path] | None = None) -> dict[str, str]:
    """Instagram cookies from the Firefox profile (or container) that used Instagram most recently."""
    profiles = firefox_profile_dirs() if profiles is None else profiles
    if not profiles:
        raise CookieImportError("Firefox doesn't seem to be installed (no Firefox profile found).")
    best: tuple[int, dict[str, str]] | None = None
    for profile in profiles:
        try:
            rows = _read_firefox_cookies(profile / "cookies.sqlite")
        except (OSError, sqlite3.Error):
            continue
        groups: dict[str, list[tuple]] = {}
        for name, value, origin, last_accessed in rows:
            groups.setdefault(origin or "", []).append((name, value, last_accessed or 0))
        for cookies in groups.values():
            sessions = [last for name, value, last in cookies if name == "sessionid" and value]
            if sessions and (best is None or max(sessions) > best[0]):
                best = (max(sessions), {name: value for name, value, _ in cookies})
    if best is None:
        raise CookieImportError(
            "No Instagram login found in Firefox. Open instagram.com in Firefox, log in, then try again.")
    return best[1]


# ---------------------------------------------------------------------------- saved session


def session_path() -> Path:
    return config_dir() / "session.json"


def save_session(cookies: dict[str, str], username: str | None) -> Path:
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"username": username, "saved_at": int(time.time()), "cookies": cookies}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_session() -> tuple[dict[str, str], str | None] | None:
    try:
        data = json.loads(session_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, dict) or not cookies.get("sessionid"):
        return None
    return {str(k): str(v) for k, v in cookies.items()}, data.get("username")


def delete_session() -> bool:
    try:
        session_path().unlink()
        return True
    except FileNotFoundError:
        return False
