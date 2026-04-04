"""High-level service layer used by the CLI."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mpv_tracker.config import (
    APP_SETTINGS_FILE_NAME,
    DB_FILE_NAME,
    MAL_ANIME_CACHE_FILE_NAME,
    MAL_SETTINGS_FILE_NAME,
    RESUME_BACKTRACK_SECONDS,
    default_data_dir,
)
from mpv_tracker.library import LibraryRepository
from mpv_tracker.mal import (
    MALDataError,
    MALSyncError,
    hydrate_current_user,
    load_settings,
    parse_anime_reference,
    resolve_cached_anime_info,
    save_settings,
    update_anime_progress,
)
from mpv_tracker.models import (
    AppSettings,
    Episode,
    EpisodeProgress,
    LibraryEntry,
    MALAnimeInfo,
    MALSettings,
    SeriesDetail,
    SeriesProgress,
)
from mpv_tracker.mpv_client import MPVWatcher, PlaybackSnapshot
from mpv_tracker.progress import (
    current_progress,
    discover_episodes,
    load_state,
    reset_state,
    save_state,
    select_episode,
    transition_episode_progress,
    watched_count,
)
from mpv_tracker.settings_store import (
    load_settings as load_app_settings_file,
)
from mpv_tracker.settings_store import (
    save_settings as save_app_settings_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def slugify(value: str) -> str:
    """Convert a title into a CLI-friendly slug."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-")


_MAX_MAL_SCORE = 10


@dataclass(slots=True)
class TrackerService:
    """Application service coordinating library, state, and playback."""

    repository: LibraryRepository
    mal_settings_path: Path | None = None
    mal_anime_cache_path: Path | None = None
    app_settings_path: Path | None = None

    @classmethod
    def create_default(cls) -> "TrackerService":
        """Create the service using the default application data directory."""
        data_dir = default_data_dir()
        return cls(
            repository=LibraryRepository(data_dir / DB_FILE_NAME),
            mal_settings_path=data_dir / MAL_SETTINGS_FILE_NAME,
            mal_anime_cache_path=data_dir / MAL_ANIME_CACHE_FILE_NAME,
            app_settings_path=data_dir / APP_SETTINGS_FILE_NAME,
        )

    def add_series(
        self,
        *,
        title: str,
        directory: Path,
        slug: str | None,
        mal_anime: str | None = None,
        start_chapter: int | None = None,
    ) -> LibraryEntry:
        """Register a series in the global library index."""
        resolved_directory = directory.expanduser().resolve()
        if not resolved_directory.is_dir():
            msg = f"Directory does not exist: {resolved_directory}"
            raise ValueError(msg)

        effective_slug = slugify(slug or title)
        if not effective_slug:
            msg = "Slug cannot be empty."
            raise ValueError(msg)

        entry = LibraryEntry(
            slug=effective_slug,
            title=title.strip(),
            directory=resolved_directory,
            mal_anime_id=parse_anime_reference(mal_anime),
            start_chapter_index=_parse_start_chapter(start_chapter),
        )
        try:
            self.repository.add(entry)
        except sqlite3.IntegrityError as error:
            msg = "A series with the same slug or directory already exists."
            raise ValueError(msg) from error
        return entry

    def update_series(
        self,
        current_slug: str,
        *,
        title: str,
        directory: Path,
        slug: str | None,
        mal_anime: str | None = None,
    ) -> LibraryEntry:
        """Update tracked series metadata."""
        existing_entry = self.resolve_entry(current_slug)
        resolved_directory = directory.expanduser().resolve()
        if not resolved_directory.is_dir():
            msg = f"Directory does not exist: {resolved_directory}"
            raise ValueError(msg)

        effective_slug = slugify(slug or title)
        if not effective_slug:
            msg = "Slug cannot be empty."
            raise ValueError(msg)

        entry = LibraryEntry(
            slug=effective_slug,
            title=title.strip(),
            directory=resolved_directory,
            mal_anime_id=parse_anime_reference(mal_anime),
            start_chapter_index=existing_entry.start_chapter_index,
        )
        try:
            updated = self.repository.update(current_slug, entry)
        except sqlite3.IntegrityError as error:
            msg = "A series with the same slug or directory already exists."
            raise ValueError(msg) from error
        if not updated:
            msg = f"No series found for slug {current_slug!r}."
            raise ValueError(msg)
        return entry

    def load_mal_settings(self) -> MALSettings:
        """Load persisted MAL credentials."""
        return load_settings(self._resolve_mal_settings_path())

    def save_mal_settings(self, settings: MALSettings) -> MALSettings:
        """Persist MAL credentials."""
        save_settings(self._resolve_mal_settings_path(), settings)
        return settings

    def refresh_mal_current_user(self) -> MALSettings:
        """Refresh the persisted MAL account summary from the API."""
        settings = self.load_mal_settings()
        updated = hydrate_current_user(
            settings,
            app_settings=self.load_app_settings(),
        )
        return self.save_mal_settings(updated)

    def load_app_settings(self) -> AppSettings:
        """Load persisted application settings."""
        return load_app_settings_file(self._resolve_app_settings_path())

    def save_app_settings(self, settings: AppSettings) -> AppSettings:
        """Persist application settings."""
        save_app_settings_file(self._resolve_app_settings_path(), settings)
        return settings

    def list_progress(self) -> list[SeriesProgress]:
        """Summarize tracked series progress."""
        results: list[SeriesProgress] = []
        for entry in self.repository.list_entries():
            episodes = discover_episodes(entry.directory)
            state = load_state(entry.directory)
            current_episode, position_seconds = current_progress(state)
            results.append(
                SeriesProgress(
                    entry=entry,
                    watched_count=watched_count(state, episodes),
                    total_count=len(episodes),
                    current_episode=current_episode,
                    current_position_seconds=position_seconds,
                ),
            )
        return results

    def resolve_entry(self, slug: str) -> LibraryEntry:
        """Return a tracked series or fail with a user-facing error."""
        entry = self.repository.get(slug)
        if entry is None:
            msg = f"No series found for slug {slug!r}."
            raise ValueError(msg)
        return entry

    def choose_episode(
        self,
        slug: str,
        selector: str | None,
    ) -> tuple[LibraryEntry, Episode, float, int]:
        """Resolve the target series and the episode to play."""
        entry = self.resolve_entry(slug)
        state = load_state(entry.directory)
        episodes = discover_episodes(entry.directory)
        episode = select_episode(episodes, state, selector=selector)
        start_position = _resolve_start_position(state, episode)
        return entry, episode, start_position, episode.index - 1

    def get_series_detail(self, slug: str) -> SeriesDetail:
        """Return detailed series progress for the TUI."""
        entry = self.resolve_entry(slug)
        state = load_state(entry.directory)
        episodes = discover_episodes(entry.directory)
        suggested_episode = None
        mal_anime_info = None
        if episodes:
            suggested_episode = select_episode(episodes, state, selector=None)
        if entry.mal_anime_id is not None:
            mal_anime_info = self._resolve_mal_anime_info(entry.mal_anime_id)
        current_episode, current_position_seconds = current_progress(state)
        episode_states = state.get("episodes", {})
        if not isinstance(episode_states, dict):
            episode_states = {}

        detailed_episodes: list[EpisodeProgress] = []
        for episode in episodes:
            raw_episode_state = episode_states.get(episode.label, {})
            if not isinstance(raw_episode_state, dict):
                raw_episode_state = {}
            duration_seconds = raw_episode_state.get("duration_seconds")
            detailed_episodes.append(
                EpisodeProgress(
                    episode=episode,
                    watched=bool(raw_episode_state.get("watched")),
                    position_seconds=_coerce_seconds(
                        raw_episode_state.get("position_seconds", 0.0),
                    ),
                    duration_seconds=(
                        _coerce_seconds(duration_seconds)
                        if isinstance(duration_seconds, int | float)
                        else None
                    ),
                    is_current=episode.label == current_episode,
                ),
            )

        return SeriesDetail(
            entry=entry,
            watched_count=watched_count(state, episodes),
            total_count=len(episodes),
            current_episode=current_episode,
            current_position_seconds=current_position_seconds,
            suggested_episode=suggested_episode,
            mal_anime_info=mal_anime_info,
            episodes=detailed_episodes,
        )

    def refresh_series_mal_anime_info(self, slug: str) -> MALAnimeInfo | None:
        """Force refresh cached MAL anime metadata for a linked series."""
        entry = self.resolve_entry(slug)
        if entry.mal_anime_id is None:
            return None
        return self._resolve_mal_anime_info(entry.mal_anime_id, force_refresh=True)

    def watch(self, slug: str, selector: str | None) -> tuple[LibraryEntry, Episode]:
        """Launch MPV and update progress while playback runs."""
        entry, episode, start_position, playlist_start = self.choose_episode(
            slug,
            selector,
        )
        state = load_state(entry.directory)
        watcher = MPVWatcher(
            entry.directory,
            episode_name=episode.label,
            playlist_start=playlist_start,
            start_position_seconds=start_position,
            preferred_start_chapter_index=entry.start_chapter_index,
        )
        previous_snapshot: tuple[str, float, float | None, bool] | None = None

        def persist_snapshot(snapshot: PlaybackSnapshot) -> None:
            nonlocal previous_snapshot
            save_state(
                entry.directory,
                transition_episode_progress(
                    state,
                    previous_snapshot=previous_snapshot,
                    snapshot=(
                        snapshot.episode_name,
                        snapshot.position_seconds,
                        snapshot.duration_seconds,
                        snapshot.watched,
                    ),
                ),
            )
            previous_snapshot = _merge_previous_snapshot(previous_snapshot, snapshot)

        snapshot = watcher.watch(
            on_update=persist_snapshot,
        )
        persist_snapshot(snapshot)
        self._sync_series_progress_to_mal(entry)
        return entry, episode

    def reset_progress(self, slug: str) -> LibraryEntry:
        """Clear saved watch history for a tracked series."""
        entry = self.resolve_entry(slug)
        reset_state(entry.directory)
        return entry

    def remove_series(self, slug: str) -> LibraryEntry:
        """Remove a tracked series from the library index."""
        entry = self.resolve_entry(slug)
        removed = self.repository.remove(slug)
        if not removed:
            msg = f"No series found for slug {slug!r}."
            raise ValueError(msg)
        return entry

    def sync_series_progress_to_mal(self, slug: str) -> None:
        """Synchronize watched episode count to MAL for a linked series."""
        entry = self.resolve_entry(slug)
        self._sync_series_progress_to_mal(entry)

    def update_series_preferences(
        self,
        slug: str,
        *,
        start_chapter: int | None,
    ) -> LibraryEntry:
        """Update per-series preferences."""
        entry = self.resolve_entry(slug)
        updated = LibraryEntry(
            slug=entry.slug,
            title=entry.title,
            directory=entry.directory,
            mal_anime_id=entry.mal_anime_id,
            start_chapter_index=_parse_start_chapter(start_chapter),
        )
        self.repository.update(slug, updated)
        return updated

    def rate_series_on_mal(self, slug: str, *, score: int) -> None:
        """Set the linked MAL score from a 1-10 input."""
        if score < 1 or score > _MAX_MAL_SCORE:
            msg = "Score must be between 1 and 10."
            raise ValueError(msg)

        entry = self.resolve_entry(slug)
        if entry.mal_anime_id is None:
            msg = "Series is not linked to MAL."
            raise ValueError(msg)

        settings = self.load_mal_settings()
        if not settings.access_token:
            msg = "MAL authentication is not configured."
            raise ValueError(msg)

        update_anime_progress(
            anime_id=entry.mal_anime_id,
            access_token=settings.access_token,
            num_watched_episodes=watched_count(
                load_state(entry.directory),
                discover_episodes(entry.directory),
            ),
            score=score,
            app_settings=self.load_app_settings(),
        )

    def _resolve_mal_settings_path(self) -> Path:
        if self.mal_settings_path is not None:
            return self.mal_settings_path
        return default_data_dir() / MAL_SETTINGS_FILE_NAME

    def _resolve_app_settings_path(self) -> Path:
        if self.app_settings_path is not None:
            return self.app_settings_path
        return default_data_dir() / APP_SETTINGS_FILE_NAME

    def _resolve_mal_anime_cache_path(self) -> Path:
        if self.mal_anime_cache_path is not None:
            return self.mal_anime_cache_path
        return default_data_dir() / MAL_ANIME_CACHE_FILE_NAME

    def _sync_series_progress_to_mal(self, entry: LibraryEntry) -> None:
        if entry.mal_anime_id is None:
            return

        settings = self.load_mal_settings()
        if not settings.access_token:
            return

        episodes = discover_episodes(entry.directory)
        if not episodes:
            return

        state = load_state(entry.directory)
        watched_episodes = watched_count(state, episodes)
        if watched_episodes <= 0:
            return

        status = "completed" if watched_episodes >= len(episodes) else "watching"
        try:
            update_anime_progress(
                anime_id=entry.mal_anime_id,
                access_token=settings.access_token,
                num_watched_episodes=watched_episodes,
                status=status,
                app_settings=self.load_app_settings(),
            )
        except MALSyncError:
            return

    def _resolve_mal_anime_info(
        self,
        anime_id: int,
        *,
        force_refresh: bool = False,
    ) -> MALAnimeInfo | None:
        settings = self.load_mal_settings()
        client_id = settings.client_id.strip()
        if not client_id:
            return None
        try:
            return resolve_cached_anime_info(
                anime_id,
                client_id=client_id,
                cache_path=self._resolve_mal_anime_cache_path(),
                app_settings=self.load_app_settings(),
                force_refresh=force_refresh,
            )
        except MALDataError:
            return None


def _resolve_start_position(state: dict[str, object], episode: Episode) -> float:
    current_episode, current_position = current_progress(state)
    if current_episode == episode.label:
        return max(current_position - RESUME_BACKTRACK_SECONDS, 0.0)

    episodes_state = state.get("episodes", {})
    if not isinstance(episodes_state, dict):
        return 0.0

    episode_state = episodes_state.get(episode.label, {})
    if not isinstance(episode_state, dict):
        return 0.0

    position = episode_state.get("position_seconds", 0.0)
    if not isinstance(position, int | float):
        return 0.0
    return max(float(position) - RESUME_BACKTRACK_SECONDS, 0.0)


def _merge_previous_snapshot(
    previous_snapshot: tuple[str, float, float | None, bool] | None,
    snapshot: PlaybackSnapshot,
) -> tuple[str, float, float | None, bool]:
    if previous_snapshot is None or previous_snapshot[0] != snapshot.episode_name:
        return (
            snapshot.episode_name,
            snapshot.position_seconds,
            snapshot.duration_seconds,
            snapshot.watched,
        )

    _, previous_position, previous_duration, previous_watched = previous_snapshot
    effective_duration = snapshot.duration_seconds
    if effective_duration is None:
        effective_duration = previous_duration
    return (
        snapshot.episode_name,
        max(previous_position, snapshot.position_seconds),
        effective_duration,
        previous_watched or snapshot.watched,
    )


def _coerce_seconds(value: object) -> float:
    if isinstance(value, int | float):
        return max(float(value), 0.0)
    return 0.0


def _parse_start_chapter(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        msg = "Start chapter must be a positive integer."
        raise ValueError(msg)
    return value - 1
