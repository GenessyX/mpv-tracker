"""Textual application for browsing tracked series and launching playback."""

from __future__ import annotations

import os
import traceback
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, ListItem, ListView, Static

from mpv_tracker.mal import (
    MALAuthError,
    anime_url,
    authenticate,
    cache_avatar,
    profile_url,
)
from mpv_tracker.models import AppSettings, MALAnimeInfo, MALSettings
from mpv_tracker.service import TrackerService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mpv_tracker.models import EpisodeProgress, SeriesDetail, SeriesProgress

BINDING = Binding | tuple[str, str] | tuple[str, str, str]


def run_tui(*, debug: bool = False) -> None:
    """Launch the Textual interface."""
    with _textual_debug_features(enabled=debug):
        MPVTrackerApp(debug=debug).run()


class SeriesListItem(ListItem):
    """List row representing a tracked series."""

    def __init__(self, progress: SeriesProgress) -> None:
        self.slug = progress.entry.slug
        super().__init__(Static(_format_series_row(progress)))


class EpisodeListItem(ListItem):
    """List row representing a discovered episode."""

    def __init__(self, episode_progress: EpisodeProgress) -> None:
        self.episode_label = episode_progress.episode.label
        super().__init__(Static(_format_episode_row(episode_progress)))


class DirectoryMatchItem(ListItem):
    """List row for a matching filesystem directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(Static(str(path)))


class LibraryScreen(Screen[None]):
    """First screen showing all tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("a", "add_series", "Add"),
        ("e", "edit_series", "Edit"),
        ("d", "remove_series", "Remove"),
        ("m", "mal_login", "MAL"),
        ("s", "settings", "Settings"),
        ("enter", "open_selected", "Open"),
        ("r", "refresh", "Refresh"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="library-view"):
            yield Static("Tracked Series", id="title")
            yield Static("", id="library-status")
            yield ListView(id="series-list")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_series()

    def refresh_series(self) -> None:
        app = self._tracker_app()
        service = app.service
        progress_items = service.list_progress()
        list_view = self.query_one("#series-list", ListView)
        list_view.clear()
        status_message = app.consume_library_message()
        if not progress_items:
            self.query_one("#library-status", Static).update(
                status_message
                or "No series tracked yet. Press `a` to add a tracked series.",
            )
            return

        self.query_one("#library-status", Static).update(
            status_message or "Select a series and press Enter to view details.",
        )
        for item in progress_items:
            list_view.append(SeriesListItem(item))
        list_view.index = 0
        list_view.focus()

    def action_refresh(self) -> None:
        self.refresh_series()

    def action_add_series(self) -> None:
        self._tracker_app().push_screen(AddSeriesScreen())

    def action_mal_login(self) -> None:
        self._tracker_app().push_screen(MALSettingsScreen())

    def action_settings(self) -> None:
        self._tracker_app().push_screen(AppSettingsScreen())

    def action_edit_series(self) -> None:
        list_view = self.query_one("#series-list", ListView)
        highlighted = list_view.highlighted_child
        if isinstance(highlighted, SeriesListItem):
            self._tracker_app().push_screen(EditSeriesScreen(highlighted.slug))

    def action_remove_series(self) -> None:
        list_view = self.query_one("#series-list", ListView)
        highlighted = list_view.highlighted_child
        if isinstance(highlighted, SeriesListItem):
            self._tracker_app().push_screen(
                ConfirmRemoveSeriesScreen(highlighted.slug),
            )

    def action_open_selected(self) -> None:
        list_view = self.query_one("#series-list", ListView)
        highlighted = list_view.highlighted_child
        if isinstance(highlighted, SeriesListItem):
            self._tracker_app().push_screen(SeriesDetailScreen(highlighted.slug))

    @on(ListView.Selected, "#series-list")
    def handle_open_series(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SeriesListItem):
            self._tracker_app().push_screen(SeriesDetailScreen(event.item.slug))

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class AddSeriesScreen(Screen[None]):
    """Form screen for adding a tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
        ("down", "focus_directory_matches", "Directory Matches"),
        ("right", "descend_directory", "Enter Directory"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="add-series-view"):
            yield Static("Add Series", id="detail-title")
            yield Static(
                (
                    "Enter a title and directory. Slug and MAL anime reference "
                    "are optional. Directory matches appear below as you type."
                ),
                id="add-series-status",
            )
            yield Input(placeholder="Series title", id="add-title")
            yield Input(placeholder="/path/to/series", id="add-directory")
            yield ListView(id="directory-matches")
            yield Input(placeholder="optional-slug", id="add-slug")
            yield Input(
                placeholder="MAL anime ID or https://myanimelist.net/anime/...",
                id="add-mal-anime",
            )
            with Horizontal(id="detail-actions"):
                yield Button("Save", id="save-series", variant="primary")
                yield Button("Cancel", id="cancel-series")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#add-title", Input).focus()
        self._update_directory_matches("")

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._submit()

    def action_focus_directory_matches(self) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        if matches_view.children:
            self._activate_directory_matches_and_focus()

    def action_descend_directory(self) -> None:
        self._descend_into_highlighted_directory()

    @on(Button.Pressed, "#save-series")
    def handle_save_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel-series")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Input.Submitted)
    def handle_input_submitted(self, event: Input.Submitted) -> None:
        if (
            event.input.id == "add-directory"
            and self._apply_highlighted_directory_match()
        ):
            return
        if event.input.id == "add-mal-anime":
            self._submit()
            return
        self.focus_next()

    @on(Input.Changed, "#add-directory")
    def handle_directory_changed(self, event: Input.Changed) -> None:
        self._update_directory_matches(event.value)

    @on(ListView.Selected, "#directory-matches")
    def handle_directory_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, DirectoryMatchItem):
            self._apply_directory_match(event.item.path)

    def _submit(self) -> None:
        title = self.query_one("#add-title", Input).value.strip()
        directory = self.query_one("#add-directory", Input).value.strip()
        slug = self.query_one("#add-slug", Input).value.strip() or None
        mal_anime = self.query_one("#add-mal-anime", Input).value.strip() or None
        if not title:
            self._set_status("Title cannot be empty.")
            self.query_one("#add-title", Input).focus()
            return
        if not directory:
            self._set_status("Directory cannot be empty.")
            self.query_one("#add-directory", Input).focus()
            return

        try:
            entry = self._tracker_app().service.add_series(
                title=title,
                directory=Path(directory),
                slug=slug,
                mal_anime=mal_anime,
            )
        except ValueError as error:
            self._set_status(str(error))
            return

        app = self._tracker_app()
        app.library_message = f"Added {entry.title} ({entry.slug})."
        app.pop_screen()
        app.refresh_library()

    def _set_status(self, message: str) -> None:
        self.query_one("#add-series-status", Static).update(message)

    def _update_directory_matches(self, value: str) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        matches_view.index = None
        matches_view.clear()
        matches = _find_directory_matches(value)
        for path in matches:
            matches_view.append(DirectoryMatchItem(path))
        if matches:
            self.call_after_refresh(self._activate_directory_matches)

    def _apply_highlighted_directory_match(self) -> bool:
        matches_view = self.query_one("#directory-matches", ListView)
        highlighted = matches_view.highlighted_child
        if isinstance(highlighted, DirectoryMatchItem):
            self._apply_directory_match(highlighted.path)
            return True
        return False

    def _descend_into_highlighted_directory(self) -> bool:
        matches_view = self.query_one("#directory-matches", ListView)
        highlighted = matches_view.highlighted_child
        if not isinstance(highlighted, DirectoryMatchItem):
            return False

        path = highlighted.path
        directory_input = self.query_one("#add-directory", Input)
        directory_input.value = _directory_prefix(path)
        self._update_directory_matches(directory_input.value)
        if not _find_directory_matches(directory_input.value):
            directory_input.focus()
        else:
            self.call_after_refresh(self._activate_directory_matches_and_focus)
        return True

    def _apply_directory_match(self, path: Path) -> None:
        directory_input = self.query_one("#add-directory", Input)
        directory_input.value = str(path)
        self._update_directory_matches(str(path))
        directory_input.focus()

    def _activate_directory_matches(self) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        if not matches_view.children:
            return
        matches_view.index = None
        matches_view.index = 0

    def _activate_directory_matches_and_focus(self) -> None:
        self._activate_directory_matches()
        matches_view = self.query_one("#directory-matches", ListView)
        if matches_view.children:
            matches_view.focus()

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class EditSeriesScreen(Screen[None]):
    """Form screen for editing a tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
        ("down", "focus_directory_matches", "Directory Matches"),
        ("right", "descend_directory", "Enter Directory"),
    ]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="add-series-view"):
            yield Static("Edit Series", id="detail-title")
            yield Static(
                (
                    "Update the tracked series fields. Slug and MAL anime "
                    "reference are optional."
                ),
                id="add-series-status",
            )
            yield Input(placeholder="Series title", id="add-title")
            yield Input(placeholder="/path/to/series", id="add-directory")
            yield ListView(id="directory-matches")
            yield Input(placeholder="optional-slug", id="add-slug")
            yield Input(
                placeholder="MAL anime ID or https://myanimelist.net/anime/...",
                id="add-mal-anime",
            )
            with Horizontal(id="detail-actions"):
                yield Button("Save", id="save-series", variant="primary")
                yield Button("Cancel", id="cancel-series")
        yield Footer()

    def on_mount(self) -> None:
        entry = self._tracker_app().service.resolve_entry(self.slug)
        self.query_one("#add-title", Input).value = entry.title
        self.query_one("#add-directory", Input).value = str(entry.directory)
        self.query_one("#add-slug", Input).value = entry.slug
        mal_link = anime_url(entry.mal_anime_id) or ""
        self.query_one("#add-mal-anime", Input).value = mal_link
        self.query_one("#add-title", Input).focus()
        self._update_directory_matches(str(entry.directory))

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._submit()

    def action_focus_directory_matches(self) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        if matches_view.children:
            self._activate_directory_matches_and_focus()

    def action_descend_directory(self) -> None:
        self._descend_into_highlighted_directory()

    @on(Button.Pressed, "#save-series")
    def handle_save_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel-series")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Input.Submitted)
    def handle_input_submitted(self, event: Input.Submitted) -> None:
        if (
            event.input.id == "add-directory"
            and self._apply_highlighted_directory_match()
        ):
            return
        if event.input.id == "add-mal-anime":
            self._submit()
            return
        self.focus_next()

    @on(Input.Changed, "#add-directory")
    def handle_directory_changed(self, event: Input.Changed) -> None:
        self._update_directory_matches(event.value)

    @on(ListView.Selected, "#directory-matches")
    def handle_directory_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, DirectoryMatchItem):
            self._apply_directory_match(event.item.path)

    def _submit(self) -> None:
        title = self.query_one("#add-title", Input).value.strip()
        directory = self.query_one("#add-directory", Input).value.strip()
        slug = self.query_one("#add-slug", Input).value.strip() or None
        mal_anime = self.query_one("#add-mal-anime", Input).value.strip() or None
        if not title:
            self._set_status("Title cannot be empty.")
            self.query_one("#add-title", Input).focus()
            return
        if not directory:
            self._set_status("Directory cannot be empty.")
            self.query_one("#add-directory", Input).focus()
            return

        try:
            entry = self._tracker_app().service.update_series(
                self.slug,
                title=title,
                directory=Path(directory),
                slug=slug,
                mal_anime=mal_anime,
            )
        except ValueError as error:
            self._set_status(str(error))
            return

        app = self._tracker_app()
        app.library_message = f"Updated {entry.title} ({entry.slug})."
        app.pop_screen()
        app.refresh_library()

    def _set_status(self, message: str) -> None:
        self.query_one("#add-series-status", Static).update(message)

    def _update_directory_matches(self, value: str) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        matches_view.index = None
        matches_view.clear()
        matches = _find_directory_matches(value)
        for path in matches:
            matches_view.append(DirectoryMatchItem(path))
        if matches:
            self.call_after_refresh(self._activate_directory_matches)

    def _apply_highlighted_directory_match(self) -> bool:
        matches_view = self.query_one("#directory-matches", ListView)
        highlighted = matches_view.highlighted_child
        if isinstance(highlighted, DirectoryMatchItem):
            self._apply_directory_match(highlighted.path)
            return True
        return False

    def _descend_into_highlighted_directory(self) -> bool:
        matches_view = self.query_one("#directory-matches", ListView)
        highlighted = matches_view.highlighted_child
        if not isinstance(highlighted, DirectoryMatchItem):
            return False

        path = highlighted.path
        directory_input = self.query_one("#add-directory", Input)
        directory_input.value = _directory_prefix(path)
        self._update_directory_matches(directory_input.value)
        if not _find_directory_matches(directory_input.value):
            directory_input.focus()
        else:
            self.call_after_refresh(self._activate_directory_matches_and_focus)
        return True

    def _apply_directory_match(self, path: Path) -> None:
        directory_input = self.query_one("#add-directory", Input)
        directory_input.value = str(path)
        self._update_directory_matches(str(path))
        directory_input.focus()

    def _activate_directory_matches(self) -> None:
        matches_view = self.query_one("#directory-matches", ListView)
        if not matches_view.children:
            return
        matches_view.index = None
        matches_view.index = 0

    def _activate_directory_matches_and_focus(self) -> None:
        self._activate_directory_matches()
        matches_view = self.query_one("#directory-matches", ListView)
        if matches_view.children:
            matches_view.focus()

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class MALSettingsScreen(Screen[None]):
    """Screen for entering MyAnimeList API credentials."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
        ("r", "refresh_profile", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="mal-settings-view"):
            yield Static("MyAnimeList Login", id="detail-title")
            yield Static(
                (
                    "Set your MAL client ID, then authenticate in the browser. "
                    "The callback is currently fixed to http://localhost:1234/callback."
                ),
                id="mal-settings-status",
            )
            yield Input(placeholder="MAL client ID", id="mal-client-id")
            with Horizontal(id="detail-actions"):
                yield Button("Save Client ID", id="save-mal-client", variant="primary")
                yield Button("Authenticate", id="authenticate-mal")
                yield Button("Refresh Profile", id="refresh-mal-profile")
                yield Button("Cancel", id="cancel-mal")
            with Vertical(id="mal-account-layout"):
                yield Static("", id="mal-token-status")
                yield Static("", id="mal-account-status")
                yield Static("", id="mal-avatar", shrink=True)
        yield Footer()

    def on_mount(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        self.query_one("#mal-client-id", Input).value = settings.client_id
        self.query_one("#mal-client-id", Input).focus()
        self._update_account_status()
        self._refresh_avatar_preview()

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._save_client_id()

    def action_refresh_profile(self) -> None:
        self._refresh_profile()

    @on(Button.Pressed, "#save-mal-client")
    def handle_save_button(self) -> None:
        self._save_client_id()

    @on(Button.Pressed, "#authenticate-mal")
    def handle_authenticate_button(self) -> None:
        self._authenticate()

    @on(Button.Pressed, "#refresh-mal-profile")
    def handle_refresh_profile_button(self) -> None:
        self._refresh_profile()

    @on(Button.Pressed, "#cancel-mal")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Input.Submitted)
    def handle_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "mal-client-id":
            self._save_client_id()
            return
        self.focus_next()

    def _save_client_id(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        client_id = self.query_one("#mal-client-id", Input).value.strip()
        if not client_id:
            self._set_status("MAL client ID cannot be empty.")
            self.query_one("#mal-client-id", Input).focus()
            return

        self._tracker_app().service.save_mal_settings(
            MALSettings(
                client_id=client_id,
                access_token=settings.access_token,
                refresh_token=settings.refresh_token,
                user_name=settings.user_name,
                user_picture=settings.user_picture,
            ),
        )
        self._set_status("Saved MAL client ID.")
        self._update_account_status()

    @work(thread=True, exclusive=True)
    def _authenticate(self) -> None:
        client_id = self.query_one("#mal-client-id", Input).value.strip()
        if not client_id:
            self._tracker_app().call_from_thread(
                self._set_status,
                "MAL client ID cannot be empty.",
            )
            return
        self._tracker_app().call_from_thread(
            self._set_status,
            "Opening MAL authentication in the browser...",
        )
        existing_settings = self._tracker_app().service.load_mal_settings()
        app_settings = self._tracker_app().service.load_app_settings()
        self._tracker_app().service.save_mal_settings(
            MALSettings(
                client_id=client_id,
                access_token=existing_settings.access_token,
                refresh_token=existing_settings.refresh_token,
                user_name=existing_settings.user_name,
                user_picture=existing_settings.user_picture,
            ),
        )
        try:
            updated_settings = authenticate(
                client_id,
                app_settings=app_settings,
            )
        except MALAuthError as error:
            self._tracker_app().report_exception(error)
            self._tracker_app().call_from_thread(self._set_status, str(error))
            return
        self._tracker_app().service.save_mal_settings(updated_settings)
        self._tracker_app().call_from_thread(
            self._complete_authentication,
            updated_settings,
        )

    @work(thread=True, exclusive=True)
    def _refresh_profile(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        if not settings.access_token:
            self._tracker_app().call_from_thread(
                self._set_status,
                "Authenticate with MyAnimeList first.",
            )
            return

        self._tracker_app().call_from_thread(
            self._set_status,
            "Refreshing MyAnimeList account details...",
        )
        try:
            updated_settings = self._tracker_app().service.refresh_mal_current_user()
        except MALAuthError as error:
            self._tracker_app().report_exception(error)
            self._tracker_app().call_from_thread(self._set_status, str(error))
            return
        self._tracker_app().call_from_thread(
            self._complete_profile_refresh,
            updated_settings,
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#mal-settings-status", Static).update(message)

    def _update_account_status(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        self.query_one("#mal-token-status", Static).update(_mal_login_status(settings))
        self.query_one("#mal-account-status", Static).update(
            _mal_account_status(settings),
        )
        self._refresh_avatar_preview()

    def _complete_authentication(self, settings: MALSettings) -> None:
        self.query_one("#mal-client-id", Input).value = settings.client_id
        self._update_account_status()
        app = self._tracker_app()
        message = "Authenticated with MyAnimeList."
        if settings.user_name:
            message = f"Authenticated with MyAnimeList as {settings.user_name}."
        app.library_message = message
        self._set_status("MAL authentication completed successfully.")
        app.pop_screen()
        app.refresh_library()

    def _complete_profile_refresh(self, settings: MALSettings) -> None:
        self.query_one("#mal-client-id", Input).value = settings.client_id
        self._update_account_status()
        self._set_status("Refreshed MyAnimeList account details.")

    @work(thread=True, exclusive=True)
    def _refresh_avatar_preview(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        picture_url = settings.user_picture.strip()
        if not picture_url:
            self._tracker_app().call_from_thread(
                self._hide_avatar_widget,
            )
            return
        try:
            avatar_path = cache_avatar(
                picture_url,
                app_settings=self._tracker_app().service.load_app_settings(),
            )
        except MALAuthError as error:
            self._tracker_app().report_exception(error)
            self._tracker_app().call_from_thread(
                self._update_avatar_widget_text,
                f"Failed to load avatar.\n{error}",
            )
            return

        if avatar_path is None:
            self._tracker_app().call_from_thread(
                self._hide_avatar_widget,
            )
            return

        try:
            renderable = _avatar_renderable(avatar_path)
        except ImportError:
            self._tracker_app().call_from_thread(
                self._update_avatar_widget_text,
                "Avatar preview needs `rich-pixels`.",
            )
            return
        except Exception as error:  # noqa: BLE001
            self._tracker_app().report_exception(error)
            self._tracker_app().call_from_thread(
                self._update_avatar_widget_text,
                f"Failed to render avatar.\n{error}",
            )
            return

        self._tracker_app().call_from_thread(
            self._show_avatar_renderable,
            renderable,
        )

    def _update_avatar_widget_text(self, message: str) -> None:
        avatar = self.query_one("#mal-avatar", Static)
        avatar.display = True
        avatar.update(message)

    def _show_avatar_renderable(self, renderable: object) -> None:
        avatar = self.query_one("#mal-avatar", Static)
        avatar.display = True
        avatar.update(cast("Any", renderable))

    def _hide_avatar_widget(self) -> None:
        avatar = self.query_one("#mal-avatar", Static)
        avatar.update("")
        avatar.display = False

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class AppSettingsScreen(Screen[None]):
    """Screen for application-level settings such as proxies."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="app-settings-view"):
            yield Static("Settings", id="detail-title")
            yield Static(
                "General application settings.",
                id="app-settings-status",
            )
            yield Static("Proxy", id="settings-section-title")
            yield Static(
                "HTTP proxy is used for plain HTTP requests. "
                "HTTPS proxy is used for HTTPS requests.",
                id="settings-section-help",
            )
            yield Input(
                placeholder="HTTP proxy URL, for example http://127.0.0.1:8080",
                id="http-proxy",
            )
            yield Input(
                placeholder="HTTPS proxy URL, for example http://127.0.0.1:8080",
                id="https-proxy",
            )
            with Horizontal(id="detail-actions"):
                yield Button("Save", id="save-app-settings", variant="primary")
                yield Button("Cancel", id="cancel-app-settings")
        yield Footer()

    def on_mount(self) -> None:
        settings = self._tracker_app().service.load_app_settings()
        self.query_one("#http-proxy", Input).value = settings.http_proxy
        self.query_one("#https-proxy", Input).value = settings.https_proxy
        self.query_one("#http-proxy", Input).focus()

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._submit()

    @on(Button.Pressed, "#save-app-settings")
    def handle_save_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel-app-settings")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Input.Submitted)
    def handle_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "https-proxy":
            self._submit()
            return
        self.focus_next()

    def _submit(self) -> None:
        settings = AppSettings(
            http_proxy=self.query_one("#http-proxy", Input).value.strip(),
            https_proxy=self.query_one("#https-proxy", Input).value.strip(),
        )
        self._tracker_app().service.save_app_settings(settings)
        app = self._tracker_app()
        app.library_message = "Saved application settings."
        self.query_one("#app-settings-status", Static).update("Saved settings.")
        app.pop_screen()
        app.refresh_library()

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class ConfirmRemoveSeriesScreen(Screen[None]):
    """Confirmation dialog for removing a tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("left", "focus_previous_button", "Previous"),
        ("right", "focus_next_button", "Next"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="confirm-remove-view"):
            yield Static("Remove Series", id="detail-title")
            yield Static("", id="confirm-remove-message")
            yield Static(
                "This removes the series from the tracker list only.",
                id="confirm-remove-status",
            )
            with Horizontal(id="detail-actions"):
                yield Button("Remove", id="confirm-remove", variant="error")
                yield Button("Cancel", id="cancel-remove")
        yield Footer()

    def on_mount(self) -> None:
        entry = self._tracker_app().service.resolve_entry(self.slug)
        self.query_one("#confirm-remove-message", Static).update(
            f"Remove {entry.title} ({entry.slug}) from the tracked series list?",
        )
        self.call_after_refresh(self._focus_confirm_button)

    def action_confirm(self) -> None:
        self._confirm()

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_focus_next_button(self) -> None:
        focused = self.focused
        if focused is self.query_one("#confirm-remove", Button):
            self._focus_cancel_button()
            return
        self._focus_confirm_button()

    def action_focus_previous_button(self) -> None:
        focused = self.focused
        if focused is self.query_one("#cancel-remove", Button):
            self._focus_confirm_button()
            return
        self._focus_cancel_button()

    @on(Button.Pressed, "#confirm-remove")
    def handle_confirm_button(self) -> None:
        self._confirm()

    @on(Button.Pressed, "#cancel-remove")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    def _confirm(self) -> None:
        try:
            entry = self._tracker_app().service.remove_series(self.slug)
        except ValueError as error:
            self.query_one("#confirm-remove-status", Static).update(str(error))
            return

        app = self._tracker_app()
        app.library_message = f"Removed {entry.title} ({entry.slug})."
        app.pop_screen()
        app.refresh_library()

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)

    def _focus_confirm_button(self) -> None:
        self.query_one("#confirm-remove", Button).focus()

    def _focus_cancel_button(self) -> None:
        self.query_one("#cancel-remove", Button).focus()


class SeriesDetailScreen(Screen[None]):
    """Detail screen for a single tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "pop_screen", "Back"),
        ("e", "edit_series", "Edit"),
        ("left", "focus_previous_action", "Previous"),
        ("p", "play_selected", "Play"),
        ("right", "focus_next_action", "Next"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self._detail: SeriesDetail | None = None
        self._playing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-view"):
            yield Static("", id="detail-title")
            yield Static("", id="detail-summary")
            yield Static("", id="detail-directory")
            yield Static("", id="detail-mal")
            yield Static("", id="playback-status")
            with Horizontal(id="detail-actions"):
                yield Button("Play", id="play", variant="primary")
                yield Button("Edit", id="edit")
                yield Button("Back", id="back")
                yield Button("Refresh", id="refresh")
            yield ListView(id="episode-list")
        yield Footer()

    def on_mount(self) -> None:
        self.load_detail()

    def load_detail(self) -> None:
        self._detail = self._tracker_app().service.get_series_detail(self.slug)
        detail = self._detail
        self.query_one("#detail-title", Static).update(detail.entry.title)
        self.query_one("#detail-summary", Static).update(_format_detail_summary(detail))
        self.query_one("#detail-directory", Static).update(str(detail.entry.directory))
        mal_text = "MAL: not linked"
        if detail.entry.mal_anime_id is not None:
            mal_text = (
                f"MAL: {detail.entry.mal_anime_id} "
                f"({anime_url(detail.entry.mal_anime_id)})"
            )
            if detail.mal_anime_info is not None:
                mal_text = (
                    f"{mal_text}\n{_format_mal_anime_info(detail.mal_anime_info)}"
                )
        self.query_one("#detail-mal", Static).update(mal_text)
        playback_status = (
            "Choose an episode and press Play. Enter on a row also starts playback."
        )
        if not detail.episodes:
            playback_status = "No playable episode files were found in this directory."
        self.query_one("#playback-status", Static).update(playback_status)
        list_view = self.query_one("#episode-list", ListView)
        list_view.clear()
        for episode in detail.episodes:
            list_view.append(EpisodeListItem(episode))

        default_index = next(
            (
                index
                for index, episode in enumerate(detail.episodes)
                if detail.suggested_episode is not None
                and episode.episode.label == detail.suggested_episode.label
            ),
            0,
        )
        if detail.episodes:
            list_view.index = default_index
            list_view.focus()
        self._sync_play_button()

    def action_refresh(self) -> None:
        self.load_detail()

    def action_pop_screen(self) -> None:
        self._tracker_app().pop_screen()

    def action_edit_series(self) -> None:
        self._tracker_app().push_screen(EditSeriesScreen(self.slug))

    def action_focus_next_action(self) -> None:
        focused = self.focused
        play_button = self.query_one("#play", Button)
        edit_button = self.query_one("#edit", Button)
        back_button = self.query_one("#back", Button)
        refresh_button = self.query_one("#refresh", Button)
        if focused is play_button:
            edit_button.focus()
            return
        if focused is edit_button:
            back_button.focus()
            return
        if focused is back_button:
            refresh_button.focus()
            return
        if focused is refresh_button:
            play_button.focus()
            return

    def action_focus_previous_action(self) -> None:
        focused = self.focused
        play_button = self.query_one("#play", Button)
        edit_button = self.query_one("#edit", Button)
        back_button = self.query_one("#back", Button)
        refresh_button = self.query_one("#refresh", Button)
        if focused is play_button:
            refresh_button.focus()
            return
        if focused is edit_button:
            play_button.focus()
            return
        if focused is back_button:
            edit_button.focus()
            return
        if focused is refresh_button:
            back_button.focus()
            return

    def action_play_selected(self) -> None:
        if self._playing:
            return
        self._play(self._selected_episode_label())

    @on(Button.Pressed, "#play")
    def handle_play_button(self) -> None:
        self.action_play_selected()

    @on(Button.Pressed, "#edit")
    def handle_edit_button(self) -> None:
        self.action_edit_series()

    @on(Button.Pressed, "#back")
    def handle_back_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Button.Pressed, "#refresh")
    def handle_refresh_button(self) -> None:
        self.load_detail()

    @on(ListView.Selected, "#episode-list")
    def handle_episode_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, EpisodeListItem):
            self._play(event.item.episode_label)

    @work(thread=True, exclusive=True)
    def _play(self, selector: str | None) -> None:
        self._playing = True
        app = self._tracker_app()
        app.call_from_thread(self._sync_play_button)
        label = selector or "resume selection"
        app.call_from_thread(
            self.query_one("#playback-status", Static).update,
            f"Launching mpv for {label}...",
        )
        try:
            app.service.watch(self.slug, selector)
        except Exception as error:  # noqa: BLE001
            app.report_exception(error)
            app.call_from_thread(self._handle_playback_error, str(error))
            return
        app.call_from_thread(self._handle_playback_complete)

    def _handle_playback_complete(self) -> None:
        self._playing = False
        self.load_detail()
        self._tracker_app().refresh_library()
        self.query_one("#playback-status", Static).update("Playback finished.")

    def _handle_playback_error(self, message: str) -> None:
        self._playing = False
        self._sync_play_button()
        self.query_one("#playback-status", Static).update(f"Playback failed: {message}")

    def _sync_play_button(self) -> None:
        detail = self._detail
        self.query_one("#play", Button).disabled = self._playing or not (
            detail is not None and detail.episodes
        )

    def _selected_episode_label(self) -> str | None:
        list_view = self.query_one("#episode-list", ListView)
        highlighted = list_view.highlighted_child
        if isinstance(highlighted, EpisodeListItem):
            return highlighted.episode_label
        return None

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class MPVTrackerApp(App[None]):
    """Top-level Textual application."""

    CSS = """
    Screen {
        background: #10151c;
        color: #edf2f7;
    }

    #library-view, #detail-view {
        padding: 1 2;
    }

    #add-series-view, #confirm-remove-view, #mal-settings-view, #app-settings-view {
        padding: 1 2;
    }

    #mal-account-layout {
        height: auto;
        margin-bottom: 1;
        align-horizontal: left;
    }

    #mal-avatar {
        width: auto;
        height: auto;
        content-align: center middle;
        border: round #355070;
        padding: 1;
        margin-top: 1;
    }

    #title, #detail-title {
        text-style: bold;
        color: #f6bd60;
        margin-bottom: 1;
    }

    #library-status, #detail-summary, #detail-directory, #detail-mal,
    #playback-status, #add-series-status, #confirm-remove-message,
    #confirm-remove-status, #mal-settings-status, #app-settings-status,
    #settings-section-title, #settings-section-help {
        margin-bottom: 1;
    }

    #settings-section-title {
        text-style: bold;
        color: #f6bd60;
    }

    #settings-section-help {
        color: #9fb3c8;
    }

    #detail-directory, #detail-mal {
        color: #9fb3c8;
    }

    #detail-actions {
        height: auto;
        margin-bottom: 1;
    }

    ListView {
        border: round #355070;
        background: #16212b;
    }

    ListItem {
        padding: 0 1;
    }

    ListItem.--highlight {
        background: #264653;
        color: #f1faee;
    }

    Button {
        margin-right: 1;
    }

    Button:focus {
        border: round #f6bd60;
        background: #355070;
        color: #f1faee;
        text-style: bold;
    }

    Input {
        margin-bottom: 1;
    }

    #directory-matches {
        height: 8;
        margin-bottom: 1;
    }
    """

    BINDINGS: ClassVar[list[BINDING]] = [
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, *, debug: bool = False) -> None:
        super().__init__()
        self.service = TrackerService.create_default()
        self.library_message: str | None = None
        self.debug_mode = debug

    def on_mount(self) -> None:
        self.push_screen(LibraryScreen())

    def refresh_library(self) -> None:
        screen = self.screen
        if isinstance(screen, LibraryScreen):
            screen.refresh_series()
            return
        for stacked_screen in self.screen_stack:
            if isinstance(stacked_screen, LibraryScreen):
                stacked_screen.refresh_series()
                return

    def consume_library_message(self) -> str | None:
        message = self.library_message
        self.library_message = None
        return message

    def report_exception(self, error: BaseException) -> None:
        if self.debug_mode:
            traceback.print_exception(error, file=self.error_console.file)


def _format_series_row(progress: SeriesProgress) -> str:
    current = ""
    if progress.current_episode is not None:
        current = (
            f" | resume {progress.current_episode} @ "
            f"{_format_seconds(progress.current_position_seconds)}"
        )
    return (
        f"{progress.entry.title} [{progress.watched_count}/{progress.total_count}]"
        f"{current}"
    )


def _format_detail_summary(detail: SeriesDetail) -> str:
    suggested = "none"
    if detail.suggested_episode is not None:
        suggested = detail.suggested_episode.label
    current = "No resume position saved."
    if detail.current_episode is not None:
        current = (
            f"Current: {detail.current_episode} @ "
            f"{_format_seconds(detail.current_position_seconds)}"
        )
    return (
        f"{detail.watched_count}/{detail.total_count} watched"
        f" | suggested: {suggested}"
        f" | {current}"
    )


def _format_episode_row(episode_progress: EpisodeProgress) -> str:
    markers: list[str] = []
    if episode_progress.watched:
        markers.append("watched")
    elif episode_progress.is_current:
        markers.append(f"resume {_format_seconds(episode_progress.position_seconds)}")
    elif episode_progress.position_seconds > 0:
        markers.append(f"seen {_format_seconds(episode_progress.position_seconds)}")
    else:
        markers.append("unwatched")

    if episode_progress.duration_seconds is not None:
        markers.append(_format_seconds(episode_progress.duration_seconds))

    details = ", ".join(markers)
    return (
        f"{episode_progress.episode.index:>2}. "
        f"{episode_progress.episode.label} [{details}]"
    )


def _format_seconds(value: float) -> str:
    total_seconds = int(max(value, 0))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def _mal_login_status(settings: MALSettings) -> str:
    if settings.access_token:
        return "MyAnimeList login is saved."
    return "No MyAnimeList login saved."


def _mal_account_status(settings: MALSettings) -> str:
    if not settings.access_token:
        return "Account: not authenticated."

    lines = ["Account: authenticated."]
    if settings.user_name:
        lines.append(f"User: {settings.user_name}")
        profile = profile_url(settings.user_name)
        if profile is not None:
            lines.append(f"Profile: {profile}")
    else:
        lines.append("User: not loaded yet. Press Refresh to fetch account details.")

    if settings.user_picture:
        lines.append(f"Avatar: {settings.user_picture}")
    return "\n".join(lines)


def _format_mal_anime_info(info: MALAnimeInfo) -> str:
    score = "n/a" if info.score is None else f"{info.score:.2f}"
    rank = "n/a" if info.rank is None else str(info.rank)
    popularity = "n/a" if info.popularity is None else str(info.popularity)
    return f"Score: {score} | Ranked: {rank} | Popularity: {popularity}"


def _avatar_renderable(path: Path) -> object:
    rich_pixels = import_module("rich_pixels")
    image_module = import_module("PIL.Image")
    image = image_module.open(path)
    try:
        image.thumbnail((72, 20))
        return rich_pixels.Pixels.from_image(image)
    finally:
        image.close()


@contextmanager
def _textual_debug_features(*, enabled: bool) -> Iterator[None]:
    previous = os.environ.get("TEXTUAL")
    if enabled:
        features = (
            {feature.strip() for feature in previous.split(",")} if previous else set()
        )
        features.update({"debug", "devtools"})
        os.environ["TEXTUAL"] = ",".join(
            sorted(feature for feature in features if feature),
        )
    try:
        yield
    finally:
        if enabled:
            if previous is None:
                os.environ.pop("TEXTUAL", None)
            else:
                os.environ["TEXTUAL"] = previous


def _find_directory_matches(value: str) -> list[Path]:
    if not value:
        return []

    expanded = Path(value).expanduser()
    parent = expanded.parent
    prefix = expanded.name

    if value.endswith((Path("/").anchor, "/")):
        parent = expanded
        prefix = ""

    if not parent.exists() or not parent.is_dir():
        return []

    try:
        matches = sorted(
            child
            for child in parent.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name.startswith(prefix)
        )
    except OSError:
        return []

    return matches


def _directory_prefix(path: Path) -> str:
    path_text = str(path)
    if path_text.endswith(os.sep):
        return path_text
    return f"{path_text}{os.sep}"
