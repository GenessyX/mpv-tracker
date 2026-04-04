"""Helpers for storing and updating per-directory watch state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mpv_tracker.config import STATE_FILE_NAME, VIDEO_EXTENSIONS, WATCHED_THRESHOLD
from mpv_tracker.models import Episode

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def discover_episodes(directory: Path) -> list[Episode]:
    """Return video files in a stable sorted order."""
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    files.sort(key=lambda path: path.name.lower())
    return [Episode(index=index + 1, path=path) for index, path in enumerate(files)]


def state_file_path(directory: Path) -> Path:
    """Return the hidden state file path for a tracked directory."""
    return directory / STATE_FILE_NAME


def load_state(directory: Path) -> dict[str, Any]:
    """Load the state file or return an empty default state."""
    path = state_file_path(directory)
    if not path.exists():
        return {"current": None, "episodes": {}}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    episodes = data.get("episodes", {})
    current = data.get("current")
    if not isinstance(episodes, dict):
        episodes = {}
    if current is not None and not isinstance(current, dict):
        current = None
    return {"current": current, "episodes": episodes}


def save_state(directory: Path, state: dict[str, Any]) -> None:
    """Persist the state file atomically enough for local CLI usage."""
    path = state_file_path(directory)
    with path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write("\n")


def reset_state(directory: Path) -> None:
    """Reset the per-directory watch state."""
    save_state(directory, {"current": None, "episodes": {}})


def mark_episode_progress(
    state: dict[str, Any],
    episode_name: str,
    *,
    position_seconds: float,
    duration_seconds: float | None,
    watched: bool,
) -> dict[str, Any]:
    """Update episode progress in-memory."""
    episodes = state.setdefault("episodes", {})
    episode_state = episodes.setdefault(episode_name, {})
    effective_watched = bool(episode_state.get("watched")) or watched
    episode_state["position_seconds"] = max(position_seconds, 0.0)
    if duration_seconds is not None:
        episode_state["duration_seconds"] = max(duration_seconds, 0.0)
    episode_state["watched"] = effective_watched
    state["current"] = {
        "episode": episode_name,
        "position_seconds": episode_state["position_seconds"],
    }
    if effective_watched:
        state["current"] = None
        episode_state["position_seconds"] = 0.0
    return state


def transition_episode_progress(
    state: dict[str, Any],
    *,
    previous_snapshot: tuple[str, float, float | None, bool] | None,
    snapshot: tuple[str, float, float | None, bool],
) -> dict[str, Any]:
    """Update state across playlist transitions."""
    current_episode_name, position_seconds, duration_seconds, watched = snapshot
    if previous_snapshot is not None:
        (
            previous_episode_name,
            previous_position_seconds,
            previous_duration_seconds,
            previous_watched,
        ) = previous_snapshot
    else:
        previous_episode_name = None
        previous_position_seconds = 0.0
        previous_duration_seconds = None
        previous_watched = False

    if (
        previous_episode_name is not None
        and previous_episode_name != current_episode_name
        and _snapshot_is_watched(
            (
                previous_position_seconds,
                previous_duration_seconds,
                previous_watched,
            ),
        )
    ):
        mark_episode_progress(
            state,
            previous_episode_name,
            position_seconds=0.0,
            duration_seconds=None,
            watched=True,
        )
    return mark_episode_progress(
        state,
        current_episode_name,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        watched=watched,
    )


def _snapshot_is_watched(
    snapshot: tuple[float, float | None, bool],
) -> bool:
    position_seconds, duration_seconds, watched = snapshot
    if watched:
        return True
    if duration_seconds is None or duration_seconds <= 0:
        return False
    return (position_seconds / duration_seconds) >= WATCHED_THRESHOLD


def watched_count(state: dict[str, Any], episodes: Iterable[Episode]) -> int:
    """Count watched episodes for discovered files only."""
    episode_list = list(episodes)
    episode_states = state.get("episodes", {})
    explicit_watched_count = sum(
        1
        for episode in episode_list
        if bool(episode_states.get(episode.label, {}).get("watched"))
    )
    current_episode, _ = current_progress(state)
    if current_episode is None:
        return explicit_watched_count

    inferred_watched_count = explicit_watched_count
    for episode in episode_list:
        if episode.label == current_episode:
            inferred_watched_count = max(explicit_watched_count, episode.index - 1)
            break
    return inferred_watched_count


def current_progress(state: dict[str, Any]) -> tuple[str | None, float]:
    """Return the current episode and playback position."""
    current = state.get("current")
    if not isinstance(current, dict):
        return None, 0.0
    episode_name = current.get("episode")
    position_seconds = current.get("position_seconds", 0.0)
    if not isinstance(episode_name, str):
        return None, 0.0
    if not isinstance(position_seconds, int | float):
        position_seconds = 0.0
    return episode_name, float(position_seconds)


def select_episode(
    episodes: list[Episode],
    state: dict[str, Any],
    *,
    selector: str | None,
) -> Episode:
    """Choose an episode by explicit selector or resume/next heuristics."""
    if not episodes:
        msg = "No playable episode files were found in the tracked directory."
        raise ValueError(msg)

    if selector is not None:
        selected = _match_explicit_selector(episodes, selector)
        if selected is not None:
            return selected
        msg = f"Episode selector {selector!r} did not match any discovered file."
        raise ValueError(msg)

    resumed = _match_current_episode(episodes, state)
    if resumed is not None:
        return resumed

    next_episode = _first_unwatched_episode(episodes, state)
    if next_episode is not None:
        return next_episode
    return episodes[-1]


def _match_explicit_selector(
    episodes: list[Episode],
    selector: str,
) -> Episode | None:
    if selector.isdigit():
        selected_index = int(selector)
        for episode in episodes:
            if episode.index == selected_index:
                return episode
        return None

    selector_lower = selector.lower()
    for episode in episodes:
        if selector_lower in episode.label.lower():
            return episode
    return None


def _match_current_episode(
    episodes: list[Episode],
    state: dict[str, Any],
) -> Episode | None:
    current_episode, _ = current_progress(state)
    if current_episode is None:
        return None
    for episode in episodes:
        if episode.label == current_episode:
            return episode
    return None


def _first_unwatched_episode(
    episodes: list[Episode],
    state: dict[str, Any],
) -> Episode | None:
    episode_states = state.get("episodes", {})
    for episode in episodes:
        if not bool(episode_states.get(episode.label, {}).get("watched")):
            return episode
    return None
