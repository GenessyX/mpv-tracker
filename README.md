# MPV Tracker

CLI tools that allows to track episodes in a directory

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
