"""Helpers for persisted recent activity."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from mpv_tracker.models import RecentActivityEntry

if TYPE_CHECKING:
    from pathlib import Path

_MAX_ACTIVITY_ENTRIES = 200


def load_recent_activity(path: Path) -> list[RecentActivityEntry]:
    """Load recent activity from disk."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, list):
        return []
    entries: list[RecentActivityEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        entries.append(
            RecentActivityEntry(
                slug=_coerce_string(item.get("slug")),
                series_title=_coerce_string(item.get("series_title")),
                episode_name=_coerce_string(item.get("episode_name")),
                watched_at=_coerce_int(item.get("watched_at")),
                position_seconds=_coerce_float(item.get("position_seconds")),
                duration_seconds=_coerce_optional_float(item.get("duration_seconds")),
                completed=bool(item.get("completed")),
            ),
        )
    return entries


def append_recent_activity(
    path: Path,
    entry: RecentActivityEntry,
) -> list[RecentActivityEntry]:
    """Append and persist a recent activity entry."""
    entries = load_recent_activity(path)
    entries.insert(0, entry)
    trimmed_entries = entries[:_MAX_ACTIVITY_ENTRIES]
    save_recent_activity(path, trimmed_entries)
    return trimmed_entries


def save_recent_activity(path: Path, entries: list[RecentActivityEntry]) -> None:
    """Persist recent activity to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            [asdict(entry) for entry in entries],
            file,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def _coerce_string(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _coerce_int(value: object) -> int:
    if isinstance(value, int):
        return value
    return 0


def _coerce_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _coerce_optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None
