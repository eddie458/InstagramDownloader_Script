"""User settings, stored as JSON in the per-user configuration folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .net import DEFAULT_USER_AGENT

MEDIA_FILTERS = ("all", "photos", "videos")
MEDIA_FILTER_LABELS = {"all": "Everything (photos + videos)", "photos": "Photos only", "videos": "Videos only"}


def config_dir() -> Path:
    """Where settings and the login session are kept (override with the IGDL_HOME variable)."""
    override = os.environ.get("IGDL_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "InstagramDownloader"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "instagram-downloader"


def default_output_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.cwd()) / "Instagram"


def normalize_quality(value: str) -> str | None:
    """'best', 'ask' or a maximum resolution such as '1080' (also accepts '720p'). None if invalid."""
    value = str(value).strip().lower()
    if value in ("best", "ask"):
        return value
    if value.endswith("p"):
        value = value[:-1]
    if value.isdigit() and int(value) > 0:
        return str(int(value))
    return None


def quality_label(quality: str) -> str:
    if quality == "best":
        return "Highest available"
    if quality == "ask":
        return "Let me choose for each post"
    return f"Up to {quality}p"


@dataclass
class Settings:
    output_dir: str = ""
    media_filter: str = "all"
    quality: str = "best"
    save_captions: bool = False
    min_delay: float = 4.0
    max_delay: float = 8.0
    ffmpeg_path: str = ""
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if not self.output_dir:
            self.output_dir = str(default_output_dir())
        if self.media_filter not in MEDIA_FILTERS:
            self.media_filter = "all"
        self.quality = normalize_quality(self.quality) or "best"
        self.min_delay = max(0.0, float(self.min_delay))
        self.max_delay = max(self.min_delay, float(self.max_delay))
        self.user_agent = self.user_agent or DEFAULT_USER_AGENT

    @staticmethod
    def path() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{key: value for key, value in data.items() if key in known})
        except (TypeError, ValueError):
            return cls()

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
