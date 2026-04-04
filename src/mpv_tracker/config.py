"""Project-level constants and path helpers."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "mpv-tracker"
STATE_FILE_NAME = ".mpv-tracker.json"
DB_FILE_NAME = "library.sqlite3"
MAL_SETTINGS_FILE_NAME = "mal.json"
APP_SETTINGS_FILE_NAME = "settings.json"
AVATAR_CACHE_DIR_NAME = "avatars"
DEFAULT_MAL_CLIENT_ID = "774e9161d6f70a57fbc5d4b7072d9417"
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}
WATCHED_THRESHOLD = 0.98
RESUME_BACKTRACK_SECONDS = 2.0


def default_data_dir() -> Path:
    """Return the application data directory."""
    xdg_data_home = Path.home() / ".local" / "share"
    return xdg_data_home / APP_NAME


def debug_mode_enabled() -> bool:
    """Return whether debug mode is enabled via environment."""
    return os.environ.get("MPV_TRACKER_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def textual_features() -> str:
    """Return the current Textual feature flag string."""
    return os.environ.get("TEXTUAL", "")
