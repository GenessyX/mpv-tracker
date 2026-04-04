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
                    mal_anime_id INTEGER
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

    def add(self, entry: LibraryEntry) -> None:
        with self._connect() as connection:
            connection.execute(
                (
                    "INSERT INTO library (slug, title, directory, mal_anime_id) "
                    "VALUES (?, ?, ?, ?)"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                ),
            )

    def update(self, current_slug: str, entry: LibraryEntry) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                (
                    "UPDATE library "
                    "SET slug = ?, title = ?, directory = ?, mal_anime_id = ? "
                    "WHERE slug = ?"
                ),
                (
                    entry.slug,
                    entry.title,
                    str(entry.directory),
                    entry.mal_anime_id,
                    current_slug,
                ),
            )
        return cursor.rowcount > 0

    def get(self, slug: str) -> LibraryEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                (
                    "SELECT slug, title, directory, mal_anime_id "
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
                    "SELECT slug, title, directory, mal_anime_id FROM library "
                    "ORDER BY title COLLATE NOCASE"
                ),
            ).fetchall()
        return [
            LibraryEntry(
                slug=row["slug"],
                title=row["title"],
                directory=Path(row["directory"]),
                mal_anime_id=row["mal_anime_id"],
            )
            for row in rows
        ]
