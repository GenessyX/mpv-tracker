"""Cyclopts CLI entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

from cyclopts import App

from mpv_tracker.service import TrackerService
from mpv_tracker.tui import run_tui

app = App(help="Track watched episodes in local series/anime directories.")


def _prompt(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        msg = f"{label} cannot be empty."
        raise ValueError(msg)
    return value


def _write(message: str) -> None:
    sys.stdout.write(f"{message}\n")


@app.command
def add(
    title: str | None = None,
    directory: Path | None = None,
    slug: str | None = None,
) -> None:
    """Add a series directory to the tracker."""
    service = TrackerService.create_default()
    effective_title = title or _prompt("Anime/series name")
    effective_directory = directory or Path(_prompt("Directory path"))
    slug_prompt = f"Slug [{effective_title.lower().replace(' ', '-')}]: "
    suggested_slug = slug or input(slug_prompt).strip() or None
    entry = service.add_series(
        title=effective_title,
        directory=effective_directory,
        slug=suggested_slug,
    )
    _write(f"Added {entry.title} as {entry.slug} -> {entry.directory}")


@app.command
def list() -> None:  # noqa: A001
    """List tracked series and progress."""
    service = TrackerService.create_default()
    progress_items = service.list_progress()
    if not progress_items:
        _write("No series tracked yet.")
        return

    for item in progress_items:
        current = ""
        if item.current_episode is not None:
            current = (
                " | current: "
                f"{item.current_episode} @ {item.current_position_seconds:.0f}s"
            )
        _write(
            f"{item.entry.slug:<20} {item.entry.title} "
            f"[{item.watched_count}/{item.total_count} watched]{current}",
        )


@app.command
def watch(slug: str, episode: str | None = None) -> None:
    """Watch the next or selected episode with MPV."""
    service = TrackerService.create_default()
    entry, chosen_episode, start_position, _ = service.choose_episode(slug, episode)
    _write(
        f"Starting {entry.title}: {chosen_episode.label}"
        + (f" from {start_position:.0f}s" if start_position > 0 else ""),
    )
    service.watch(slug, episode)


@app.command
def reset(slug: str) -> None:
    """Reset saved watch history for a tracked series."""
    service = TrackerService.create_default()
    entry = service.reset_progress(slug)
    _write(f"Reset watch history for {entry.title} ({entry.slug})")


def run() -> None:
    """Run the CLI application."""
    if len(sys.argv) == 1:
        run_tui()
        return
    app()
