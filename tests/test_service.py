from __future__ import annotations

from typing import TYPE_CHECKING

from mpv_tracker.config import DEFAULT_MAL_CLIENT_ID
from mpv_tracker.library import LibraryRepository
from mpv_tracker.mal import build_authorization, parse_anime_reference, profile_url
from mpv_tracker.models import AppSettings, MALSettings
from mpv_tracker.mpv_client import (
    PlaybackSnapshot,
    _apply_end_file,
    _apply_property_change,
    _is_ipc_disconnect,
    _ObservedPlaybackState,
    _snapshot_from_observed_state,
)
from mpv_tracker.progress import (
    current_progress,
    discover_episodes,
    load_state,
    mark_episode_progress,
    reset_state,
    save_state,
    select_episode,
    transition_episode_progress,
    watched_count,
)
from mpv_tracker.service import TrackerService, _merge_previous_snapshot, slugify

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mpv_tracker.models import Episode


def test_slugify_normalizes_title() -> None:
    assert (
        slugify("  Frieren: Beyond Journey's End  ") == "frieren-beyond-journey-s-end"
    )


def test_add_and_list_progress(tmp_path: Path) -> None:
    series_dir = tmp_path / "frieren"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")
    (series_dir / "02.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Frieren", directory=series_dir, slug=None)

    progress_items = service.list_progress()

    assert len(progress_items) == 1
    assert progress_items[0].entry.slug == "frieren"
    assert progress_items[0].watched_count == 0
    assert progress_items[0].total_count == 2


def test_remove_series_removes_entry_from_library(tmp_path: Path) -> None:
    series_dir = tmp_path / "frieren"
    series_dir.mkdir()

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Frieren", directory=series_dir, slug="frieren")

    removed = service.remove_series("frieren")

    assert removed.slug == "frieren"
    assert service.list_progress() == []


def test_update_series_updates_title_slug_and_mal_anime(tmp_path: Path) -> None:
    series_dir = tmp_path / "frieren"
    series_dir.mkdir()

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Frieren", directory=series_dir, slug="frieren")

    updated = service.update_series(
        "frieren",
        title="Sousou no Frieren",
        directory=series_dir,
        slug="sousou-no-frieren",
        mal_anime="52991",
    )

    assert updated.title == "Sousou no Frieren"
    assert updated.slug == "sousou-no-frieren"
    assert updated.mal_anime_id == 52991
    assert service.resolve_entry("sousou-no-frieren") == updated


def test_add_series_parses_mal_anime_url(tmp_path: Path) -> None:
    series_dir = tmp_path / "frieren"
    series_dir.mkdir()

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)

    entry = service.add_series(
        title="Frieren",
        directory=series_dir,
        slug="frieren",
        mal_anime="https://myanimelist.net/anime/52991/Sousou_no_Frieren",
    )

    assert entry.mal_anime_id == 52991


def test_parse_anime_reference_accepts_id_or_url() -> None:
    assert parse_anime_reference("5114") == 5114
    assert parse_anime_reference("https://myanimelist.net/anime/5114/FMA") == 5114


def test_build_authorization_uses_local_callback() -> None:
    authorization = build_authorization("client-id")

    assert authorization.authorization_url.startswith(
        "https://myanimelist.net/v1/oauth2/authorize?",
    )
    assert "client_id=client-id" in authorization.authorization_url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1234%2Fcallback" in (
        authorization.authorization_url
    )
    assert "code_challenge_method=plain" in authorization.authorization_url
    assert authorization.code_verifier
    assert authorization.state


def test_save_and_load_mal_settings(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(
        repository=repository,
        mal_settings_path=tmp_path / "mal.json",
    )
    settings = MALSettings(
        client_id="client-id",
        access_token="access-token",
        refresh_token="refresh-token",
        user_name="genesis",
        user_picture="https://cdn.myanimelist.net/images/userimages/1.jpg",
    )

    service.save_mal_settings(settings)
    loaded = service.load_mal_settings()

    assert loaded == settings


def test_load_mal_settings_uses_default_client_id_when_missing(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(
        repository=repository,
        mal_settings_path=tmp_path / "mal.json",
    )

    loaded = service.load_mal_settings()

    assert loaded.client_id == DEFAULT_MAL_CLIENT_ID
    assert loaded.access_token == ""
    assert loaded.refresh_token == ""


def test_profile_url_uses_username() -> None:
    assert profile_url("genesis") == "https://myanimelist.net/profile/genesis"


def test_save_and_load_app_settings(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(
        repository=repository,
        app_settings_path=tmp_path / "settings.json",
    )
    settings = AppSettings(
        http_proxy="http://127.0.0.1:8080",
        https_proxy="http://127.0.0.1:8080",
    )

    service.save_app_settings(settings)
    loaded = service.load_app_settings()

    assert loaded == settings


def test_select_episode_prefers_resume_then_next_unwatched(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")
    (series_dir / "02.mkv").write_text("")
    episodes = discover_episodes(series_dir)

    state = {"current": {"episode": "02.mkv", "position_seconds": 42.0}, "episodes": {}}
    assert select_episode(episodes, state, selector=None).label == "02.mkv"

    resumed_state: dict[str, object] = {
        "current": None,
        "episodes": {"01.mkv": {"watched": True}},
    }
    assert select_episode(episodes, resumed_state, selector=None).label == "02.mkv"


def test_mark_episode_progress_updates_state(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    mark_episode_progress(
        state,
        "01.mkv",
        position_seconds=120.0,
        duration_seconds=1400.0,
        watched=False,
    )
    save_state(tmp_path, state)

    reloaded = load_state(tmp_path)
    current_episode, position_seconds = current_progress(reloaded)

    assert current_episode == "01.mkv"
    assert position_seconds == 120.0
    assert watched_count(reloaded, []) == 0


def test_choose_episode_uses_saved_position(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Series", directory=series_dir, slug="series")
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 31.0},
        "episodes": {"01.mkv": {"position_seconds": 31.0, "watched": False}},
    }
    save_state(series_dir, state)

    _, episode, start_position, playlist_start = service.choose_episode(
        "series",
        None,
    )

    assert episode.label == "01.mkv"
    assert start_position == 29.0
    assert playlist_start == 0


def test_choose_episode_prefers_current_resume_position(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Series", directory=series_dir, slug="series")
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 57.0},
        "episodes": {"01.mkv": {"watched": False}},
    }
    save_state(series_dir, state)

    _, episode, start_position, playlist_start = service.choose_episode(
        "series",
        None,
    )

    assert episode.label == "01.mkv"
    assert start_position == 55.0
    assert playlist_start == 0


def test_choose_episode_clamps_backtracked_position_to_zero(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Series", directory=series_dir, slug="series")
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 1.0},
        "episodes": {"01.mkv": {"position_seconds": 1.0, "watched": False}},
    }
    save_state(series_dir, state)

    _, episode, start_position, _ = service.choose_episode("series", None)

    assert episode.label == "01.mkv"
    assert start_position == 0.0


def test_get_series_detail_returns_episode_statuses(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")
    (series_dir / "02.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Series", directory=series_dir, slug="series")
    save_state(
        series_dir,
        {
            "current": {"episode": "02.mkv", "position_seconds": 42.0},
            "episodes": {
                "01.mkv": {"watched": True, "position_seconds": 0.0},
                "02.mkv": {"watched": False, "position_seconds": 42.0},
            },
        },
    )

    detail = service.get_series_detail("series")

    assert detail.watched_count == 1
    assert detail.total_count == 2
    assert detail.current_episode == "02.mkv"
    assert detail.suggested_episode is not None
    assert detail.suggested_episode.label == "02.mkv"
    assert detail.episodes[0].watched is True
    assert detail.episodes[1].is_current is True
    assert detail.episodes[1].position_seconds == 42.0


def test_is_ipc_disconnect_matches_broken_pipe() -> None:
    assert _is_ipc_disconnect(BrokenPipeError())


def test_transition_episode_progress_marks_finished_episode_watched() -> None:
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 120.0},
        "episodes": {},
    }

    transition_episode_progress(
        state,
        previous_snapshot=("01.mkv", 1499.0, 1500.0, True),
        snapshot=("02.mkv", 15.0, 1500.0, False),
    )

    episodes = state["episodes"]
    assert isinstance(episodes, dict)
    first_episode = episodes["01.mkv"]
    assert isinstance(first_episode, dict)
    assert first_episode["watched"] is True
    assert first_episode["position_seconds"] == 0.0
    assert state["current"] == {"episode": "02.mkv", "position_seconds": 15.0}


def test_transition_episode_progress_does_not_mark_manual_skip_watched() -> None:
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 120.0},
        "episodes": {"01.mkv": {"position_seconds": 120.0, "watched": False}},
    }

    transition_episode_progress(
        state,
        previous_snapshot=("01.mkv", 120.0, 1500.0, False),
        snapshot=("05.mkv", 10.0, 1500.0, False),
    )

    episodes = state["episodes"]
    assert isinstance(episodes, dict)
    first_episode = episodes["01.mkv"]
    assert isinstance(first_episode, dict)
    assert first_episode["watched"] is False
    assert state["current"] == {"episode": "05.mkv", "position_seconds": 10.0}


def test_apply_property_change_updates_episode_immediately() -> None:
    observed = _ObservedPlaybackState(
        episode_name="01.mkv",
        position_seconds=30.0,
        duration_seconds=1500.0,
        eof_reached=False,
    )

    snapshot = _apply_property_change(
        observed,
        {"event": "property-change", "name": "path", "data": "sample/05.mkv"},
    )

    assert snapshot is not None
    assert snapshot.episode_name == "05.mkv"
    assert snapshot.position_seconds == 0.0
    assert snapshot.watched is False


def test_apply_end_file_marks_episode_watched_on_eof() -> None:
    observed = _ObservedPlaybackState(
        episode_name="01.mkv",
        position_seconds=1499.0,
        duration_seconds=1500.0,
        eof_reached=False,
    )

    snapshot = _apply_end_file(observed, {"event": "end-file", "reason": "eof"})

    assert snapshot is not None
    assert snapshot.episode_name == "01.mkv"
    assert snapshot.watched is True


def test_transition_episode_progress_accumulates_multiple_watched_episodes() -> None:
    state: dict[str, object] = {"current": None, "episodes": {}}

    transition_episode_progress(
        state,
        previous_snapshot=None,
        snapshot=("01.mkv", 1499.0, 1500.0, True),
    )
    transition_episode_progress(
        state,
        previous_snapshot=("01.mkv", 1499.0, 1500.0, True),
        snapshot=("02.mkv", 1498.0, 1500.0, True),
    )

    episodes: Iterable[Episode] = [
        type("EpisodeStub", (), {"label": "01.mkv"})(),
        type("EpisodeStub", (), {"label": "02.mkv"})(),
    ]
    assert watched_count(state, episodes) == 2


def test_snapshot_from_observed_state_marks_near_end_as_watched() -> None:
    observed = _ObservedPlaybackState(
        episode_name="01.mkv",
        position_seconds=1490.0,
        duration_seconds=1500.0,
        eof_reached=False,
    )

    snapshot = _snapshot_from_observed_state(observed)

    assert snapshot.watched is True


def test_merge_previous_snapshot_keeps_watched_state_for_same_episode() -> None:
    merged = _merge_previous_snapshot(
        ("01.mkv", 1499.0, 1500.0, True),
        PlaybackSnapshot(
            episode_name="01.mkv",
            position_seconds=0.0,
            duration_seconds=None,
            watched=False,
        ),
    )

    assert merged == ("01.mkv", 1499.0, 1500.0, True)


def test_reset_state_clears_progress(tmp_path: Path) -> None:
    state = {
        "current": {"episode": "01.mkv", "position_seconds": 120.0},
        "episodes": {"01.mkv": {"position_seconds": 120.0, "watched": False}},
    }
    save_state(tmp_path, state)

    reset_state(tmp_path)

    assert load_state(tmp_path) == {"current": None, "episodes": {}}


def test_reset_progress_clears_series_history(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "01.mkv").write_text("")

    repository = LibraryRepository(tmp_path / "library.sqlite3")
    service = TrackerService(repository=repository)
    service.add_series(title="Series", directory=series_dir, slug="series")
    save_state(
        series_dir,
        {
            "current": {"episode": "01.mkv", "position_seconds": 120.0},
            "episodes": {"01.mkv": {"position_seconds": 120.0, "watched": False}},
        },
    )

    entry = service.reset_progress("series")

    assert entry.slug == "series"
    assert load_state(series_dir) == {"current": None, "episodes": {}}
