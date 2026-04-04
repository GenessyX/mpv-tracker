"""Project-level constants and path helpers."""

from __future__ import annotations

from pathlib import Path

APP_NAME = "mpv-tracker"
STATE_FILE_NAME = ".mpv-tracker.json"
DB_FILE_NAME = "library.sqlite3"
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
