"""Shared dataclasses for library and playback state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(slots=True, frozen=True)
class LibraryEntry:
    """Tracked series metadata stored in SQLite."""

    slug: str
    title: str
    directory: Path
    mal_anime_id: int | None = None


@dataclass(slots=True, frozen=True)
class MALSettings:
    """Persisted MyAnimeList credentials and tokens."""

    client_id: str
    access_token: str = ""
    refresh_token: str = ""
    user_name: str = ""
    user_picture: str = ""


@dataclass(slots=True, frozen=True)
class MALCurrentUser:
    """Authenticated MyAnimeList account summary."""

    name: str
    picture: str = ""


@dataclass(slots=True, frozen=True)
class AppSettings:
    """Persisted application-level network settings."""

    http_proxy: str = ""
    https_proxy: str = ""


@dataclass(slots=True, frozen=True)
class Episode:
    """Video episode discovered on disk."""

    index: int
    path: Path

    @property
    def label(self) -> str:
        """Human-friendly episode name."""
        return self.path.name


@dataclass(slots=True, frozen=True)
class SeriesProgress:
    """Summary for `list` output."""

    entry: LibraryEntry
    watched_count: int
    total_count: int
    current_episode: str | None
    current_position_seconds: float


@dataclass(slots=True, frozen=True)
class EpisodeProgress:
    """Playback status for a discovered episode."""

    episode: Episode
    watched: bool
    position_seconds: float
    duration_seconds: float | None
    is_current: bool


@dataclass(slots=True, frozen=True)
class SeriesDetail:
    """Expanded progress data used by the TUI."""

    entry: LibraryEntry
    watched_count: int
    total_count: int
    current_episode: str | None
    current_position_seconds: float
    suggested_episode: Episode | None
    episodes: list[EpisodeProgress]
