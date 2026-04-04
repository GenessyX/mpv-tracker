# MPV Tracker

Terminal UI and CLI tool for tracking watched episodes in local series or anime
directories.

It keeps a global library index in SQLite and writes per-series playback state
inside the tracked directory itself in `.mpv-tracker.json`.

## TUI

Run `mpv-tracker` with no arguments to open the Textual interface.

- The first screen lists all tracked series with watched counts and resume data.
- Press `Enter` to open a series detail view.
- In the detail view, select an episode and press `Play` or `Enter` to launch
  `mpv`.
- Progress is persisted through the same `.mpv-tracker.json` state file used by
  the CLI.

## CLI Commands

`add`

- Prompts for title, directory, and optional slug when not passed as arguments.
- Stores the tracked series in a SQLite library database.

`list`

- Shows every tracked series with watched episode count vs total discovered files.
- Shows the currently resumed episode and time offset when present.

`watch <slug> [episode]`

- Resolves the tracked series by slug.
- Starts `mpv` on the tracked directory as a playlist and jumps to the resumed
  episode, next unwatched episode, or the explicitly selected episode.
- Polls MPV over its IPC socket and updates `.mpv-tracker.json` roughly once per
  second so resume data survives abrupt closes.

Episode discovery currently scans only the top level of the tracked directory
and sorts video files by filename.

## Development

This project uses [uv](https://github.com/astral-sh/uv) for dependency and virtualenv management. Useful commands:

- Create/activate a virtualenv and install dependencies: `uv sync`
- Run the linter: `uv run ruff check`
- Run type checks: `uv run mypy`
- Install local helpers: `uv sync --group local`
- Run everything in one go: `uv run ruff check && uv run mypy`

## Pre-commit

```bash
uv run prek install
```

## Packaging

Builds are handled by `hatchling` through `uv`:

```bash
uv build
```
