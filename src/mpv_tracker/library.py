"""SQLite-backed library index for tracked series."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mpv_tracker.models import LibraryEntry


class LibraryRepository:
    """Manage tracked series metadata."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS library (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    directory TEXT NOT NULL UNIQUE,
                    mal_anime_id INTEGER,
                    start_chapter_index INTEGER,
                    preferred_audio_track_id INTEGER,
                    preferred_subtitle_track_id INTEGER,
                    animefiller_url TEXT NOT NULL DEFAULT '',
                    filler_episode_numbers TEXT NOT NULL DEFAULT '[]',
                    filler_updated_at INTEGER NOT NULL DEFAULT 0,
                    skip_fillers INTEGER NOT NULL DEFAULT 0,
                    added_at INTEGER NOT NULL
                )
                """,
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(library)").fetchall()
            }
            if "mal_anime_id" not in columns:
                connection.execute(
                    "ALTER TABLE library ADD COLUMN mal_anime_id INTEGER",
                )
            if "start_chapter_index" not in columns:
                connection.execute(
                    "ALTER TABLE library ADD COLUMN start_chapter_index INTEGER",
                )
            if "preferred_audio_track_id" not in columns:
                connection.execute(
                    "ALTER TABLE library ADD COLUMN preferred_audio_track_id INTEGER",
                )
            if "preferred_subtitle_track_id" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN preferred_subtitle_track_id INTEGER"
                    ),
                )
            if "animefiller_url" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN animefiller_url TEXT NOT NULL DEFAULT ''"
                    ),
                )
            if "filler_episode_numbers" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN filler_episode_numbers TEXT NOT NULL DEFAULT '[]'"
                    ),
                )
            if "filler_updated_at" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN filler_updated_at INTEGER NOT NULL DEFAULT 0"
                    ),
                )
            if "skip_fillers" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN skip_fillers INTEGER NOT NULL DEFAULT 0"
                    ),
                )
            if "added_at" not in columns:
                connection.execute(
                    (
                        "ALTER TABLE library "
                        "ADD COLUMN added_at INTEGER NOT NULL DEFAULT 0"
                    ),
                )
                connection.execute(
                    (
                        "UPDATE library "
                        "SET added_at = CAST(strftime('%s', 'now') AS INTEGER) "
                        "WHERE added_at = 0"
                    ),
                )
            else:
                connection.execute(
                    (
                        "UPDATE library "
                        "SET added_at = CAST(strftime('%s', 'now') AS INTEGER) "
                        "WHERE added_at = 0"
                    ),
                )

    def add(self, entry: LibraryEntry) -> None:
        with self._connect() as connection:
            connection.execute(
                (
                    "INSERT INTO library "
                    "(slug, title, directory, mal_anime_id, start_chapter_index, "
                    "preferred_audio_track_id, preferred_subtitle_track_id, "
                    "animefiller_url, filler_episode_numbers, filler_updated_at, "
                    "skip_fillers, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "COALESCE(NULLIF(?, 0), CAST(strftime('%s', 'now') AS INTEGER)))"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                    entry.start_chapter_index,
                    entry.preferred_audio_track_id,
                    entry.preferred_subtitle_track_id,
                    entry.animefiller_url,
                    _serialize_episode_numbers(entry.filler_episode_numbers),
                    entry.filler_updated_at,
                    int(entry.skip_fillers),
                    entry.added_at,
                ),
            )

    def update(self, current_slug: str, entry: LibraryEntry) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                (
                    "UPDATE library "
                    "SET slug = ?, title = ?, directory = ?, mal_anime_id = ?, "
                    "start_chapter_index = ?, preferred_audio_track_id = ?, "
                    "preferred_subtitle_track_id = ?, animefiller_url = ?, "
                    "filler_episode_numbers = ?, filler_updated_at = ?, "
                    "skip_fillers = ?, added_at = ? "
                    "WHERE slug = ?"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                    entry.start_chapter_index,
                    entry.preferred_audio_track_id,
                    entry.preferred_subtitle_track_id,
                    entry.animefiller_url,
                    _serialize_episode_numbers(entry.filler_episode_numbers),
                    entry.filler_updated_at,
                    int(entry.skip_fillers),
                    entry.added_at,
                    current_slug,
                ),
            )
        return cursor.rowcount > 0

    def get(self, slug: str) -> LibraryEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                (
                    "SELECT slug, title, directory, mal_anime_id, "
                    "start_chapter_index, preferred_audio_track_id, "
                    "preferred_subtitle_track_id, animefiller_url, "
                    "filler_episode_numbers, filler_updated_at, skip_fillers, added_at "
                    "FROM library WHERE slug = ?"
                ),
                (slug,),
            ).fetchone()
        if row is None:
            return None
        return LibraryEntry(
            slug=row["slug"],
            title=row["title"],
            directory=Path(row["directory"]),
            mal_anime_id=row["mal_anime_id"],
            start_chapter_index=row["start_chapter_index"],
            preferred_audio_track_id=row["preferred_audio_track_id"],
            preferred_subtitle_track_id=row["preferred_subtitle_track_id"],
            animefiller_url=row["animefiller_url"],
            filler_episode_numbers=_deserialize_episode_numbers(
                row["filler_episode_numbers"],
            ),
            filler_updated_at=row["filler_updated_at"],
            skip_fillers=bool(row["skip_fillers"]),
            added_at=row["added_at"],
        )

    def remove(self, slug: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM library WHERE slug = ?",
                (slug,),
            )
        return cursor.rowcount > 0

    def list_entries(self) -> list[LibraryEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                (
                    "SELECT slug, title, directory, mal_anime_id, "
                    "start_chapter_index, preferred_audio_track_id, "
                    "preferred_subtitle_track_id, animefiller_url, "
                    "filler_episode_numbers, filler_updated_at, skip_fillers, added_at "
                    "FROM library "
                    "ORDER BY added_at ASC"
                ),
            ).fetchall()
        return [
            LibraryEntry(
                slug=row["slug"],
                title=row["title"],
                directory=Path(row["directory"]),
                mal_anime_id=row["mal_anime_id"],
                start_chapter_index=row["start_chapter_index"],
                preferred_audio_track_id=row["preferred_audio_track_id"],
                preferred_subtitle_track_id=row["preferred_subtitle_track_id"],
                animefiller_url=row["animefiller_url"],
                filler_episode_numbers=_deserialize_episode_numbers(
                    row["filler_episode_numbers"],
                ),
                filler_updated_at=row["filler_updated_at"],
                skip_fillers=bool(row["skip_fillers"]),
                added_at=row["added_at"],
            )
            for row in rows
        ]


def _serialize_episode_numbers(values: tuple[int, ...]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


def _deserialize_episode_numbers(value: str) -> tuple[int, ...]:
    stripped = value.strip()
    if not stripped or stripped == "[]":
        return ()
    body = stripped.removeprefix("[").removesuffix("]")
    items = [item.strip() for item in body.split(",") if item.strip()]
    numbers = [int(item) for item in items if item.isdigit()]
    return tuple(numbers)
