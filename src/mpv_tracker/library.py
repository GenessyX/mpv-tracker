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
                    "added_at) "
                    "VALUES (?, ?, ?, ?, ?, "
                    "COALESCE(NULLIF(?, 0), CAST(strftime('%s', 'now') AS INTEGER)))"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                    entry.start_chapter_index,
                    entry.added_at,
                ),
            )

    def update(self, current_slug: str, entry: LibraryEntry) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                (
                    "UPDATE library "
                    "SET slug = ?, title = ?, directory = ?, mal_anime_id = ?, "
                    "start_chapter_index = ?, added_at = ? "
                    "WHERE slug = ?"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                    entry.start_chapter_index,
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
                    "start_chapter_index, added_at "
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
                    "start_chapter_index, added_at "
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
                added_at=row["added_at"],
            )
            for row in rows
        ]
