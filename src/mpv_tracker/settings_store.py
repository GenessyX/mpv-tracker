"""Helpers for persisted application settings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mpv_tracker.models import AppSettings

if TYPE_CHECKING:
    from pathlib import Path


def load_settings(path: Path) -> AppSettings:
    """Load app settings from disk or return defaults."""
    if not path.exists():
        return AppSettings()
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        return AppSettings()
    return AppSettings(
        http_proxy=_coerce_string(payload.get("http_proxy")),
        https_proxy=_coerce_string(payload.get("https_proxy")),
    )


def save_settings(path: Path, settings: AppSettings) -> None:
    """Persist application settings to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "http_proxy": settings.http_proxy,
                "https_proxy": settings.https_proxy,
            },
            file,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def _coerce_string(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""
