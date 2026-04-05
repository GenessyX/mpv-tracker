"""Helpers for AnimeFillerList lookups and parsing."""

from __future__ import annotations

import re
from html import unescape
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

if TYPE_CHECKING:
    from mpv_tracker.models import AppSettings

_FILLER_EPISODES_PATTERN = re.compile(
    (
        r'<span[^>]*class="Label"[^>]*>\s*Filler Episodes:\s*</span>\s*'
        r'<span[^>]*class="Episodes"[^>]*>(?P<episodes>.*?)</span>'
    ),
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


class AnimeFillerDataError(RuntimeError):
    """Raised when AnimeFillerList data cannot be fetched or parsed."""


def normalize_animefiller_url(value: str | None) -> str:
    """Validate and normalize an AnimeFillerList show URL."""
    if value is None:
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        msg = "AnimeFillerList URL must start with http:// or https://."
        raise ValueError(msg)
    if parsed.netloc not in {
        "animefillerlist.com",
        "www.animefillerlist.com",
    }:
        msg = "AnimeFillerList URL must point to animefillerlist.com."
        raise ValueError(msg)
    if not parsed.path.startswith("/shows/"):
        msg = "AnimeFillerList URL must point to a show page."
        raise ValueError(msg)
    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://www.animefillerlist.com{parsed.path.rstrip('/')}{query}"


def resolve_series_filler_episodes(
    url: str,
    *,
    app_settings: AppSettings | None = None,
) -> tuple[int, ...]:
    """Fetch and parse filler episode numbers from AnimeFillerList."""
    normalized_url = normalize_animefiller_url(url)
    request = Request(  # noqa: S310
        normalized_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "mpv-tracker/1.0.0",
        },
    )
    opener = _build_url_opener(app_settings)
    try:
        with opener.open(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        msg = f"Failed to load AnimeFillerList page: HTTP {error.code} {error.reason}"
        raise AnimeFillerDataError(msg) from error
    except URLError as error:
        msg = f"Failed to load AnimeFillerList page: {error}"
        raise AnimeFillerDataError(msg) from error
    except OSError as error:
        msg = f"Failed to load AnimeFillerList page: {error}"
        raise AnimeFillerDataError(msg) from error
    return parse_filler_episode_numbers(html)


def parse_filler_episode_numbers(html: str) -> tuple[int, ...]:
    """Parse filler episode numbers from an AnimeFillerList show page."""
    content = unescape(html)
    match = _FILLER_EPISODES_PATTERN.search(content)
    if match is None:
        if "has no reported filler" in content.lower():
            return ()
        msg = "Could not find filler episode ranges on the AnimeFillerList page."
        raise AnimeFillerDataError(msg)
    ranges = _HTML_TAG_PATTERN.sub("", match.group("episodes"))
    return _parse_episode_ranges(ranges)


def _parse_episode_ranges(value: str) -> tuple[int, ...]:
    episodes: set[int] = set()
    for raw_chunk in value.split(","):
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, end_raw = chunk.split("-", maxsplit=1)
            if not start_raw.strip().isdigit() or not end_raw.strip().isdigit():
                msg = f"Invalid filler episode range: {chunk!r}"
                raise AnimeFillerDataError(msg)
            start = int(start_raw)
            end = int(end_raw)
            if start <= 0 or end < start:
                msg = f"Invalid filler episode range: {chunk!r}"
                raise AnimeFillerDataError(msg)
            episodes.update(range(start, end + 1))
            continue
        if not chunk.isdigit():
            msg = f"Invalid filler episode number: {chunk!r}"
            raise AnimeFillerDataError(msg)
        episode_number = int(chunk)
        if episode_number <= 0:
            msg = f"Invalid filler episode number: {chunk!r}"
            raise AnimeFillerDataError(msg)
        episodes.add(episode_number)
    return tuple(sorted(episodes))


def _build_url_opener(app_settings: AppSettings | None) -> OpenerDirector:
    proxies: dict[str, str] = {}
    if app_settings is not None:
        if app_settings.http_proxy:
            proxies["http"] = app_settings.http_proxy
        if app_settings.https_proxy:
            proxies["https"] = app_settings.https_proxy
    return build_opener(ProxyHandler(proxies))
