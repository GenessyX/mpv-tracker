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
    start_chapter_index: int | None = None
    preferred_audio_track_id: int | None = None
    preferred_subtitle_track_id: int | None = None
    animefiller_url: str = ""
    filler_episode_numbers: tuple[int, ...] = ()
    filler_updated_at: int = 0
    skip_fillers: bool = False
    added_at: int = 0


@dataclass(slots=True, frozen=True)
class MediaTrackOption:
    """Selectable media track option discovered from a video file."""

    track_id: int
    track_type: str
    label: str


@dataclass(slots=True, frozen=True)
class RecentActivityEntry:
    """Persisted recent playback activity."""

    slug: str
    series_title: str
    episode_name: str
    watched_at: int
    position_seconds: float
    duration_seconds: float | None
    completed: bool


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
class MALAnimeInfo:
    """Cached public MyAnimeList anime metadata."""

    anime_id: int
    score: float | None = None
    rank: int | None = None
    popularity: int | None = None
    synopsis: str = ""
    background: str = ""
    alternative_titles: list[str] | None = None
    media_type: str = ""
    status: str = ""
    num_episodes: int | None = None
    start_date: str = ""
    end_date: str = ""
    source: str = ""
    average_episode_duration_seconds: int | None = None
    rating: str = ""
    studios: list[str] | None = None
    genres: list[str] | None = None


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
    mal_anime_info: MALAnimeInfo | None
    episodes: list[EpisodeProgress]
