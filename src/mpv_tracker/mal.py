"""Helpers for MyAnimeList settings, profile data, and anime references."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import ParseResult, parse_qs, quote, urlencode, urlparse
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from mpv_tracker.config import (
    AVATAR_CACHE_DIR_NAME,
    DEFAULT_MAL_CLIENT_ID,
    MAL_ANIME_CACHE_TTL_SECONDS,
    default_data_dir,
)
from mpv_tracker.models import AppSettings, MALAnimeInfo, MALCurrentUser, MALSettings

if TYPE_CHECKING:
    from pathlib import Path

_MAL_ANIME_PATH = re.compile(r"^/anime/(?P<anime_id>\d+)(?:/|$)")
_AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
_OAUTH_TOKEN_ENDPOINT = "https://myanimelist.net/v1/oauth2/token"  # noqa: S105
_CURRENT_USER_ENDPOINT = "https://api.myanimelist.net/v2/users/@me?fields=picture"
_MY_LIST_STATUS_ENDPOINT_TEMPLATE = (
    "https://api.myanimelist.net/v2/anime/{anime_id}/my_list_status"
)
_ANIME_DETAILS_ENDPOINT_TEMPLATE = (
    "https://api.myanimelist.net/v2/anime/{anime_id}?fields=mean,rank,popularity"
)
_DEFAULT_REDIRECT_URI = "http://localhost:1234/callback"
_TOKEN_EXCHANGE_ATTEMPTS = 3
_TOKEN_EXCHANGE_BACKOFF_SECONDS = 1.0
_MAX_FILE_SUFFIX_LENGTH = 5
_CALLBACK_SUCCESS_HTML = """
<html>
  <body>
    <h1>mpv-tracker</h1>
    <p>Authentication completed. You can close this tab and return to the TUI.</p>
  </body>
</html>
""".strip()


class MALAuthError(RuntimeError):
    """Raised when MAL OAuth authentication fails."""


class MALSyncError(RuntimeError):
    """Raised when MAL list synchronization fails."""


class MALDataError(RuntimeError):
    """Raised when MAL public anime metadata fetch fails."""


@dataclass(slots=True, frozen=True)
class OAuthAuthorization:
    """OAuth authorization request values for MAL."""

    authorization_url: str
    code_verifier: str
    state: str


def load_settings(path: Path) -> MALSettings:
    """Load MAL settings from disk or return an empty configuration."""
    if not path.exists():
        return MALSettings(
            client_id=DEFAULT_MAL_CLIENT_ID,
            access_token="",
            refresh_token="",
        )
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        return MALSettings(
            client_id=DEFAULT_MAL_CLIENT_ID,
            access_token="",
            refresh_token="",
        )
    client_id = _coerce_string(payload.get("client_id")).strip()
    if not client_id:
        client_id = DEFAULT_MAL_CLIENT_ID
    return MALSettings(
        client_id=client_id,
        access_token=_coerce_string(payload.get("access_token")),
        refresh_token=_coerce_string(payload.get("refresh_token")),
        user_name=_coerce_string(payload.get("user_name")),
        user_picture=_coerce_string(payload.get("user_picture")),
    )


def save_settings(path: Path, settings: MALSettings) -> None:
    """Persist MAL settings to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "client_id": settings.client_id,
                "access_token": settings.access_token,
                "refresh_token": settings.refresh_token,
                "user_name": settings.user_name,
                "user_picture": settings.user_picture,
            },
            file,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def build_authorization(
    client_id: str,
    *,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> OAuthAuthorization:
    """Create the MAL OAuth authorization URL and PKCE values."""
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)[:128]
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_verifier,
            "code_challenge_method": "plain",
        },
    )
    return OAuthAuthorization(
        authorization_url=f"{_AUTH_URL}?{query}",
        code_verifier=code_verifier,
        state=state,
    )


def authenticate(
    client_id: str,
    *,
    app_settings: AppSettings | None = None,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
    timeout_seconds: float = 300.0,
) -> MALSettings:
    """Run the MAL OAuth flow and return persisted tokens."""
    authorization = build_authorization(client_id, redirect_uri=redirect_uri)
    callback = _wait_for_callback(
        authorization=authorization,
        redirect_uri=redirect_uri,
        timeout_seconds=timeout_seconds,
    )
    tokens = exchange_code_for_tokens(
        client_id=client_id,
        code=callback.code,
        code_verifier=authorization.code_verifier,
        app_settings=app_settings,
        redirect_uri=redirect_uri,
    )
    settings = MALSettings(
        client_id=client_id,
        access_token=_coerce_string(tokens.get("access_token")),
        refresh_token=_coerce_string(tokens.get("refresh_token")),
    )
    return hydrate_current_user(settings, app_settings=app_settings)


def exchange_code_for_tokens(
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    app_settings: AppSettings | None = None,
    redirect_uri: str = _DEFAULT_REDIRECT_URI,
) -> dict[str, object]:
    """Exchange a MAL OAuth authorization code for access tokens."""
    payload = urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    ).encode("utf-8")
    request = Request(  # noqa: S310
        _OAUTH_TOKEN_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "mpv-tracker/0.1.0",
        },
        method="POST",
    )
    opener = _build_url_opener(app_settings)
    for attempt in range(1, _TOKEN_EXCHANGE_ATTEMPTS + 1):
        try:
            with opener.open(request, timeout=30) as response:
                parsed = json.load(response)
            break
        except HTTPError as error:
            response_body = _read_http_error_body(error)
            msg = (
                "Failed to exchange MAL authorization code: "
                f"HTTP {error.code} {error.reason}"
            )
            if response_body:
                msg = f"{msg} | response={response_body}"
            raise MALAuthError(msg) from error
        except URLError as error:
            if attempt == _TOKEN_EXCHANGE_ATTEMPTS:
                msg = (
                    "Failed to exchange MAL authorization code after "
                    f"{_TOKEN_EXCHANGE_ATTEMPTS} attempts: {error}"
                )
                raise MALAuthError(msg) from error
            time.sleep(_TOKEN_EXCHANGE_BACKOFF_SECONDS * attempt)
        except OSError as error:
            if attempt == _TOKEN_EXCHANGE_ATTEMPTS:
                msg = (
                    "Failed to exchange MAL authorization code after "
                    f"{_TOKEN_EXCHANGE_ATTEMPTS} attempts: {error}"
                )
                raise MALAuthError(msg) from error
            time.sleep(_TOKEN_EXCHANGE_BACKOFF_SECONDS * attempt)
    if not isinstance(parsed, dict):
        msg = "MAL returned an invalid token response."
        raise MALAuthError(msg)
    return {str(key): value for key, value in parsed.items()}


def parse_anime_reference(value: str | None) -> int | None:
    """Parse a MAL anime ID from a raw ID or MyAnimeList URL."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        return int(stripped)

    parsed = urlparse(stripped)
    match = _MAL_ANIME_PATH.match(parsed.path)
    if match is None:
        msg = "MAL anime reference must be an ID or a myanimelist.net anime URL."
        raise ValueError(msg)
    return int(match.group("anime_id"))


def anime_url(anime_id: int | None) -> str | None:
    """Return a MAL anime URL for a stored MAL anime ID."""
    if anime_id is None:
        return None
    return f"https://myanimelist.net/anime/{anime_id}"


def profile_url(user_name: str | None) -> str | None:
    """Return a MAL profile URL for the authenticated user."""
    if user_name is None:
        return None
    stripped = user_name.strip()
    if not stripped:
        return None
    return f"https://myanimelist.net/profile/{quote(stripped)}"


def fetch_current_user(
    access_token: str,
    *,
    app_settings: AppSettings | None = None,
) -> MALCurrentUser:
    """Fetch the currently authenticated MAL user."""
    request = Request(  # noqa: S310
        _CURRENT_USER_ENDPOINT,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "mpv-tracker/0.1.0",
        },
    )
    opener = _build_url_opener(app_settings)
    try:
        with opener.open(request, timeout=30) as response:
            parsed = json.load(response)
    except HTTPError as error:
        response_body = _read_http_error_body(error)
        msg = (
            "Failed to fetch current MyAnimeList user: "
            f"HTTP {error.code} {error.reason}"
        )
        if response_body:
            msg = f"{msg} | response={response_body}"
        raise MALAuthError(msg) from error
    except (URLError, OSError) as error:
        msg = f"Failed to fetch current MyAnimeList user: {error}"
        raise MALAuthError(msg) from error

    if not isinstance(parsed, dict):
        msg = "MyAnimeList returned an invalid user response."
        raise MALAuthError(msg)

    user_name = _coerce_string(parsed.get("name")).strip()
    if not user_name:
        msg = "MyAnimeList user response did not include a user name."
        raise MALAuthError(msg)

    return MALCurrentUser(
        name=user_name,
        picture=_extract_user_picture(parsed),
    )


def hydrate_current_user(
    settings: MALSettings,
    *,
    app_settings: AppSettings | None = None,
) -> MALSettings:
    """Return MAL settings updated with the current account summary."""
    if not settings.access_token:
        return MALSettings(
            client_id=settings.client_id,
            access_token=settings.access_token,
            refresh_token=settings.refresh_token,
            user_name="",
            user_picture="",
        )

    current_user = fetch_current_user(
        settings.access_token,
        app_settings=app_settings,
    )
    return MALSettings(
        client_id=settings.client_id,
        access_token=settings.access_token,
        refresh_token=settings.refresh_token,
        user_name=current_user.name,
        user_picture=current_user.picture,
    )


def cache_avatar(
    picture_url: str,
    *,
    app_settings: AppSettings | None = None,
) -> Path | None:
    """Download the MAL avatar to the local cache and return its path."""
    normalized_url = picture_url.strip()
    if not normalized_url:
        return None

    parsed = urlparse(normalized_url)
    suffix = ""
    if "." in parsed.path.rsplit("/", maxsplit=1)[-1]:
        suffix = "." + parsed.path.rsplit(".", maxsplit=1)[-1].lower()
    if len(suffix) > _MAX_FILE_SUFFIX_LENGTH:
        suffix = ""

    cache_dir = default_data_dir() / AVATAR_CACHE_DIR_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_name = f"{sha256(normalized_url.encode('utf-8')).hexdigest()}{suffix}"
    cache_path = cache_dir / cache_name
    if cache_path.exists():
        return cache_path

    request = Request(  # noqa: S310
        normalized_url,
        headers={
            "Accept": "image/*",
            "User-Agent": "mpv-tracker/0.1.0",
        },
    )
    opener = _build_url_opener(app_settings)
    try:
        with opener.open(request, timeout=30) as response:
            payload = response.read()
    except HTTPError as error:
        response_body = _read_http_error_body(error)
        msg = f"Failed to download MAL avatar: HTTP {error.code} {error.reason}"
        if response_body:
            msg = f"{msg} | response={response_body}"
        raise MALAuthError(msg) from error
    except (URLError, OSError) as error:
        msg = f"Failed to download MAL avatar: {error}"
        raise MALAuthError(msg) from error

    cache_path.write_bytes(payload)
    return cache_path


def update_anime_progress(
    *,
    anime_id: int,
    access_token: str,
    num_watched_episodes: int,
    status: str | None = None,
    app_settings: AppSettings | None = None,
) -> None:
    """Update an anime entry in the authenticated user's MAL list."""
    payload_data: dict[str, str | int] = {
        "num_watched_episodes": num_watched_episodes,
    }
    if status:
        payload_data["status"] = status
    payload = urlencode(payload_data).encode("utf-8")
    request = Request(  # noqa: S310
        _MY_LIST_STATUS_ENDPOINT_TEMPLATE.format(anime_id=anime_id),
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "mpv-tracker/0.1.0",
        },
        method="PUT",
    )
    opener = _build_url_opener(app_settings)
    try:
        with opener.open(request, timeout=30):
            return
    except HTTPError as error:
        response_body = _read_http_error_body(error)
        msg = f"Failed to sync MyAnimeList progress: HTTP {error.code} {error.reason}"
        if response_body:
            msg = f"{msg} | response={response_body}"
        raise MALSyncError(msg) from error
    except (URLError, OSError) as error:
        msg = f"Failed to sync MyAnimeList progress: {error}"
        raise MALSyncError(msg) from error


def fetch_anime_info(
    anime_id: int,
    *,
    client_id: str,
    app_settings: AppSettings | None = None,
) -> MALAnimeInfo:
    """Fetch public anime metadata from MAL."""
    request = Request(  # noqa: S310
        _ANIME_DETAILS_ENDPOINT_TEMPLATE.format(anime_id=anime_id),
        headers={
            "X-MAL-CLIENT-ID": client_id,
            "Accept": "application/json",
            "User-Agent": "mpv-tracker/0.1.0",
        },
    )
    opener = _build_url_opener(app_settings)
    try:
        with opener.open(request, timeout=30) as response:
            parsed = json.load(response)
    except HTTPError as error:
        response_body = _read_http_error_body(error)
        msg = f"Failed to fetch MAL anime metadata: HTTP {error.code} {error.reason}"
        if response_body:
            msg = f"{msg} | response={response_body}"
        raise MALDataError(msg) from error
    except (URLError, OSError) as error:
        msg = f"Failed to fetch MAL anime metadata: {error}"
        raise MALDataError(msg) from error

    if not isinstance(parsed, dict):
        msg = "MyAnimeList returned an invalid anime metadata response."
        raise MALDataError(msg)

    return MALAnimeInfo(
        anime_id=anime_id,
        score=_coerce_optional_float(parsed.get("mean")),
        rank=_coerce_optional_int(parsed.get("rank")),
        popularity=_coerce_optional_int(parsed.get("popularity")),
    )


def load_anime_cache(path: Path) -> dict[int, tuple[float, MALAnimeInfo]]:
    """Load cached MAL anime metadata."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        return {}

    cache: dict[int, tuple[float, MALAnimeInfo]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.isdigit() or not isinstance(value, dict):
            continue
        fetched_at = value.get("fetched_at")
        if not isinstance(fetched_at, int | float):
            continue
        anime_id = int(key)
        cache[anime_id] = (
            float(fetched_at),
            MALAnimeInfo(
                anime_id=anime_id,
                score=_coerce_optional_float(value.get("score")),
                rank=_coerce_optional_int(value.get("rank")),
                popularity=_coerce_optional_int(value.get("popularity")),
            ),
        )
    return cache


def save_anime_cache(path: Path, cache: dict[int, tuple[float, MALAnimeInfo]]) -> None:
    """Persist cached MAL anime metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        str(anime_id): {
            "fetched_at": fetched_at,
            "score": info.score,
            "rank": info.rank,
            "popularity": info.popularity,
        }
        for anime_id, (fetched_at, info) in cache.items()
    }
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def resolve_cached_anime_info(
    anime_id: int,
    *,
    client_id: str,
    cache_path: Path,
    app_settings: AppSettings | None = None,
    now: float | None = None,
) -> MALAnimeInfo:
    """Return cached MAL anime metadata, refreshing stale entries."""
    current_time = time.time() if now is None else now
    cache = load_anime_cache(cache_path)
    cached = cache.get(anime_id)
    if cached is not None:
        fetched_at, info = cached
        if current_time - fetched_at < MAL_ANIME_CACHE_TTL_SECONDS:
            return info

    info = fetch_anime_info(
        anime_id,
        client_id=client_id,
        app_settings=app_settings,
    )
    cache[anime_id] = (current_time, info)
    save_anime_cache(cache_path, cache)
    return info


def _coerce_string(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _extract_user_picture(payload: dict[str, object]) -> str:
    for key in ("picture", "main_picture"):
        value = payload.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        if isinstance(value, dict):
            for nested_key in ("large", "medium", "small"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    stripped = nested.strip()
                    if stripped:
                        return stripped
    return ""


def _coerce_optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


@dataclass(slots=True, frozen=True)
class _OAuthCallback:
    code: str
    state: str


def _wait_for_callback(
    *,
    authorization: OAuthAuthorization,
    redirect_uri: str,
    timeout_seconds: float,
) -> _OAuthCallback:
    parsed_redirect = _validate_redirect_uri(redirect_uri)

    callback_data: dict[str, str] = {}
    callback_ready = threading.Event()
    host = parsed_redirect.hostname
    port = parsed_redirect.port
    if host is None or port is None:
        msg = "Redirect URI host and port must be present."
        raise MALAuthError(msg)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != parsed_redirect.path:
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            callback_data["code"] = query.get("code", [""])[0]
            callback_data["state"] = query.get("state", [""])[0]
            callback_data["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_CALLBACK_SUCCESS_HTML.encode("utf-8"))
            callback_ready.set()

        def log_message(self, _message: str, *_args: object) -> None:
            return

    httpd = HTTPServer(
        (host, port),
        CallbackHandler,
    )
    httpd.timeout = 0.5
    deadline = time.monotonic() + timeout_seconds
    try:
        if not webbrowser.open(authorization.authorization_url):
            msg = "Failed to open a browser for MAL authentication."
            raise MALAuthError(msg)
        while not callback_ready.wait(httpd.timeout):
            if time.monotonic() >= deadline:
                msg = "Timed out waiting for the MAL authentication callback."
                raise MALAuthError(msg)
            httpd.handle_request()
        if not callback_data.get("code"):
            error_name = callback_data.get("error", "unknown_error")
            msg = f"MAL authentication was not completed: {error_name}"
            raise MALAuthError(msg)
        if callback_data.get("state") != authorization.state:
            msg = "MAL authentication state mismatch."
            raise MALAuthError(msg)
        return _OAuthCallback(
            code=callback_data["code"],
            state=callback_data["state"],
        )
    finally:
        httpd.server_close()


def _validate_redirect_uri(redirect_uri: str) -> ParseResult:
    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
        msg = "Only localhost redirect URIs are currently supported."
        raise MALAuthError(msg)
    if parsed_redirect.port is None:
        msg = "Redirect URI must include an explicit port."
        raise MALAuthError(msg)
    return parsed_redirect


def _read_http_error_body(error: HTTPError) -> str:
    try:
        payload = error.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    return payload


def _build_url_opener(app_settings: AppSettings | None) -> OpenerDirector:
    proxies: dict[str, str] = {}
    if app_settings is not None:
        if app_settings.http_proxy:
            proxies["http"] = app_settings.http_proxy
        if app_settings.https_proxy:
            proxies["https"] = app_settings.https_proxy
    if not proxies:
        return build_opener()
    return build_opener(ProxyHandler(proxies))
