"""Textual application for browsing tracked series and launching playback."""

from __future__ import annotations

import os
import traceback
import webbrowser
from contextlib import contextmanager
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
)

from mpv_tracker.mal import (
    MALAuthError,
    anime_url,
    authenticate,
    cache_avatar,
    profile_url,
)
from mpv_tracker.models import AppSettings, MALAnimeInfo, MALSettings, MediaTrackOption
from mpv_tracker.progress import discover_episodes
from mpv_tracker.service import TrackerService

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mpv_tracker.models import (
        EpisodeProgress,
        LibraryEntry,
        RecentActivityEntry,
        SeriesDetail,
        SeriesProgress,
    )

BINDING = Binding | tuple[str, str] | tuple[str, str, str]
SPEED_PRESETS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


def run_tui(*, debug: bool = False) -> None:
    """Launch the Textual interface."""
    with _textual_debug_features(enabled=debug):
        MPVTrackerApp(debug=debug).run()


class SeriesListItem(ListItem):
    """List row representing a tracked series."""

    def __init__(self, progress: SeriesProgress) -> None:
        self.slug = progress.entry.slug
        super().__init__(Static(_series_row_renderable(progress)))


class EpisodeListItem(ListItem):
    """List row representing a discovered episode."""

    def __init__(
        self,
        episode_progress: EpisodeProgress,
        *,
        filler_episode_numbers: set[int],
    ) -> None:
        self.episode_label = episode_progress.episode.label
        super().__init__(
            Static(
                _episode_row_renderable(
                    episode_progress,
                    filler_episode_numbers=filler_episode_numbers,
                ),
            ),
        )


class DirectoryMatchItem(ListItem):
    """List row for a matching filesystem directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(Static(str(path)))


class RecentActivityListItem(ListItem):
    """List row for a recent playback entry."""

    def __init__(self, entry: "RecentActivityEntry") -> None:
        self.slug = entry.slug
        super().__init__(Static(_format_recent_activity_row(entry)))


class LibraryScreen(Screen[None]):
    """First screen showing all tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("/", "focus_search", "Search"),
        ("a", "add_series", "Add"),
        ("down", "focus_series_list", "List"),
        ("e", "edit_series", "Edit"),
        ("d", "remove_series", "Remove"),
        ("escape", "clear_search_focus", "Unfocus"),
        ("h", "help", "Help"),
        ("m", "mal_login", "MAL"),
        Binding("n", "sort_by_name", "Sort Name", show=False),
        ("question_mark", "help", "Help"),
        ("s", "settings", "Settings"),
        ("y", "recent_activity", "Recent"),
        Binding("t", "sort_by_added", "Sort Added", show=False),
        ("enter", "open_selected", "Open"),
        ("r", "refresh", "Refresh"),
        ("q", "app.quit", "Quit"),
        Binding("v", "toggle_sort_direction", "Reverse", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sort_field = "added"
        self._sort_descending = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="library-view"):
            yield Static("Tracked Series", id="title")
            yield Static("", id="library-status")
            yield Static("", id="library-sort-status")
            yield Input(
                placeholder="Search by title, slug, or current episode",
                id="series-search",
            )
            with Horizontal(id="series-table-header"):
                yield Static("", id="sort-title", classes="series-header-button")
                yield Static("Progress", classes="series-header-cell progress-column")
                yield Static(
                    "Current Episode",
                    classes="series-header-cell current-column",
                )
                yield Static("Resume", classes="series-header-cell resume-column")
                yield Static(
                    "",
                    id="sort-added",
                    classes="series-header-button added-column",
                )
            yield ListView(id="series-list")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_series()

    def action_focus_search(self) -> None:
        self.query_one("#series-search", Input).focus()

    def action_recent_activity(self) -> None:
        self._tracker_app().push_screen(RecentActivityScreen())

    def action_sort_by_name(self) -> None:
        self._apply_sort("title")

    def action_sort_by_added(self) -> None:
        self._apply_sort("added")

    def action_toggle_sort_direction(self) -> None:
        self._sort_descending = not self._sort_descending
        self.refresh_series()

    def action_focus_series_list(self) -> None:
        list_view = self.query_one("#series-list", ListView)
        if list_view.children:
            list_view.index = None
            list_view.index = 0
            self.call_after_refresh(self._focus_series_list)

    def _focus_series_list(self) -> None:
        list_view = self.query_one("#series-list", ListView)
        if list_view.children:
            list_view.focus()

    def action_clear_search_focus(self) -> None:
        search_input = self.query_one("#series-search", Input)
        if self.focused is search_input:
            self.action_focus_series_list()

    def refresh_series(self) -> None:
        app = self._tracker_app()
        service = app.service
        search_input = self.query_one("#series-search", Input)
        list_view = self.query_one("#series-list", ListView)
        keep_search_focus = self.focused is search_input
        highlighted = list_view.highlighted_child
        selected_slug = (
            highlighted.slug if isinstance(highlighted, SeriesListItem) else None
        )
        all_items = service.list_progress()
        filtered_items = _filter_series_progress(all_items, search_input.value)
        progress_items = _sort_series_progress(
            filtered_items,
            sort_field=self._sort_field,
            descending=self._sort_descending,
        )
        list_view = self.query_one("#series-list", ListView)
        list_view.clear()
        status_message = app.consume_library_message()
        self._update_sort_header()
        self.query_one("#library-sort-status", Static).update(
            _library_sort_status(
                sort_field=self._sort_field,
                descending=self._sort_descending,
            ),
        )
        if not all_items:
            self.query_one("#library-status", Static).update(
                status_message
                or "No series tracked yet. Press `a` to add a tracked series.",
            )
            return
        if not progress_items:
            self.query_one("#library-status", Static).update(
                status_message or "No series match the current search.",
            )
            return

        self.query_one("#library-status", Static).update(
            status_message or "Select a series and press Enter to view details.",
        )
        for item in progress_items:
            list_view.append(SeriesListItem(item))
        default_index = next(
            (
                index
                for index, item in enumerate(progress_items)
                if item.entry.slug == selected_slug
            ),
            0,
        )
        list_view.index = default_index
        if keep_search_focus:
            search_input.focus()
        else:
            list_view.focus()

    def action_refresh(self) -> None:
        self.refresh_series()

    def action_add_series(self) -> None:
        self._tracker_app().push_screen(AddSeriesScreen())

    def action_mal_login(self) -> None:
        self._tracker_app().push_screen(MALSettingsScreen())

    def action_settings(self) -> None:
        self._tracker_app().push_screen(AppSettingsScreen())

    def action_help(self) -> None:
        self._tracker_app().push_screen(HelpScreen())

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

    @on(Input.Changed, "#series-search")
    def handle_search_changed(self) -> None:
        self.refresh_series()

    @on(events.Click, "#sort-title")
    def handle_sort_title(self) -> None:
        self._apply_sort("title")

    @on(events.Click, "#sort-added")
    def handle_sort_added(self) -> None:
        self._apply_sort("added")

    def _apply_sort(self, field: str) -> None:
        if self._sort_field == field:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_field = field
            self._sort_descending = False
        self.refresh_series()

    def _update_sort_header(self) -> None:
        self.query_one("#sort-title", Static).update(
            _sortable_header_label(
                "Title",
                is_active=self._sort_field == "title",
                descending=self._sort_descending,
            ),
        )
        self.query_one("#sort-added", Static).update(
            _sortable_header_label(
                "Added",
                is_active=self._sort_field == "added",
                descending=self._sort_descending,
            ),
        )

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

    AUTO_FOCUS = ""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
        ("o", "open_profile", "Open"),
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
                yield Button("Open Profile", id="open-mal-profile")
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
        self._update_account_status()
        self._refresh_avatar_preview()

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._save_client_id()

    def action_refresh_profile(self) -> None:
        self._refresh_profile()

    def action_open_profile(self) -> None:
        self._open_profile()

    @on(Button.Pressed, "#save-mal-client")
    def handle_save_button(self) -> None:
        self._save_client_id()

    @on(Button.Pressed, "#authenticate-mal")
    def handle_authenticate_button(self) -> None:
        self._authenticate()

    @on(Button.Pressed, "#refresh-mal-profile")
    def handle_refresh_profile_button(self) -> None:
        self._refresh_profile()

    @on(Button.Pressed, "#open-mal-profile")
    def handle_open_profile_button(self) -> None:
        self._open_profile()

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

    def _open_profile(self) -> None:
        settings = self._tracker_app().service.load_mal_settings()
        if not settings.user_name:
            self._set_status("No MAL profile is loaded yet.")
            return
        profile = profile_url(settings.user_name)
        if profile is None:
            self._set_status("No MAL profile is loaded yet.")
            return
        if not webbrowser.open(profile):
            self._set_status("Failed to open MAL profile in the browser.")
            return
        self._set_status("Opened MAL profile in the browser.")

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
                "Avatar preview needs `textual-image` or `rich-pixels`.",
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
        ("i", "show_info", "Info"),
        ("m", "open_mal", "Open MAL"),
        ("o", "show_preferences", "Prefs"),
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
        self._mal_linked = False
        self._mal_rating_enabled = False
        self._selected_mal_score: int | None = None
        self._episode_label_by_row_key: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-view"):
            yield Static("", id="detail-title")
            yield Static("", id="detail-summary")
            yield Static("", id="detail-directory")
            yield Static("", id="detail-mal")
            yield Static("Rating", id="detail-rating-title")
            with Horizontal(id="mal-rating-actions"):
                yield Button("1", id="rate-1", classes="mal-rate-button")
                yield Button("2", id="rate-2", classes="mal-rate-button")
                yield Button("3", id="rate-3", classes="mal-rate-button")
                yield Button("4", id="rate-4", classes="mal-rate-button")
                yield Button("5", id="rate-5", classes="mal-rate-button")
                yield Button("6", id="rate-6", classes="mal-rate-button")
                yield Button("7", id="rate-7", classes="mal-rate-button")
                yield Button("8", id="rate-8", classes="mal-rate-button")
                yield Button("9", id="rate-9", classes="mal-rate-button")
                yield Button("10", id="rate-10", classes="mal-rate-button")
            yield Static("", id="detail-preferences")
            yield Static("", id="playback-status")
            with Horizontal(id="detail-actions"):
                yield Button("Play", id="play", variant="primary")
                yield Button("Info", id="info")
                yield Button("Open MAL", id="open-mal")
                yield Button("Prefs", id="preferences")
                yield Button("Edit", id="edit")
                yield Button("Back", id="back")
                yield Button("Refresh", id="refresh")
            yield DataTable(
                id="episode-list",
                cursor_type="row",
                zebra_stripes=True,
            )
        yield Footer()

    def on_mount(self) -> None:
        self.load_detail()

    def load_detail(self) -> None:  # noqa: PLR0915
        self._detail = self._tracker_app().service.get_series_detail(self.slug)
        detail = self._detail
        self._mal_linked = detail.entry.mal_anime_id is not None
        self._mal_rating_enabled = self._mal_linked and bool(
            self._tracker_app().service.load_mal_settings().access_token,
        )
        self.query_one("#detail-title", Static).update(detail.entry.title)
        self.query_one("#detail-summary", Static).update(_format_detail_summary(detail))
        self.query_one("#detail-directory", Static).update(str(detail.entry.directory))
        self.query_one("#detail-mal", Static).update(_format_detail_mal_text(detail))
        self.query_one("#open-mal", Button).display = self._mal_linked
        rating_title = self.query_one("#detail-rating-title", Static)
        rating_actions = self.query_one("#mal-rating-actions", Horizontal)
        rating_title.display = self._mal_linked
        rating_actions.display = self._mal_linked
        score_value = None
        if (
            detail.mal_anime_info is not None
            and detail.mal_anime_info.score is not None
        ):
            score_value = max(1, min(round(detail.mal_anime_info.score), 10))
        self._selected_mal_score = score_value
        self._sync_mal_rating_buttons()
        preferences_text = "Preferences: default playback"
        preference_parts: list[str] = []
        if detail.entry.start_chapter_index is not None:
            preference_parts.append(
                f"start fresh episodes from chapter "
                f"{detail.entry.start_chapter_index + 1}",
            )
        if detail.entry.preferred_playback_speed != 1.0:
            preference_parts.append(
                f"default speed {detail.entry.preferred_playback_speed:.2f}x",
            )
        if preference_parts:
            preferences_text = "Preferences: " + ", ".join(preference_parts)
        self.query_one("#detail-preferences", Static).update(preferences_text)
        playback_status = (
            "Choose an episode and press Play. Enter on a row also starts playback."
        )
        if not detail.episodes:
            playback_status = "No playable episode files were found in this directory."
        self.query_one("#playback-status", Static).update(playback_status)
        table = self.query_one("#episode-list", DataTable)
        table.clear(columns=True)
        table_width = max(table.size.width, self.size.width - 4)
        index_width = 5
        watched_width = 7
        seen_width = 10
        canon_width = 5
        # Leave room for padding, borders, and the scrollbar gutter.
        name_width = max(
            table_width - index_width - watched_width - seen_width - canon_width - 14,
            20,
        )
        table.add_column("Index", width=index_width)
        table.add_column("Name", width=name_width)
        table.add_column("Watched", width=watched_width)
        table.add_column("Last Seen", width=seen_width)
        table.add_column("Canon", width=canon_width)
        self._episode_label_by_row_key.clear()
        filler_episode_numbers = set(detail.entry.filler_episode_numbers)
        for episode in detail.episodes:
            row_key = table.add_row(
                str(episode.episode.index),
                episode.episode.label,
                _episode_watched_marker(episode),
                _episode_seen_time(episode),
                _episode_canon_marker(
                    episode,
                    filler_episode_numbers=filler_episode_numbers,
                ),
                key=episode.episode.label,
            )
            self._episode_label_by_row_key[row_key.value or episode.episode.label] = (
                episode.episode.label
            )

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
            table.move_cursor(row=default_index, column=0, animate=False, scroll=True)
            table.focus()
        self._sync_play_button()

    def action_refresh(self) -> None:
        self.load_detail()

    def action_pop_screen(self) -> None:
        self._tracker_app().pop_screen()

    def action_edit_series(self) -> None:
        self._tracker_app().push_screen(EditSeriesScreen(self.slug))

    def action_show_preferences(self) -> None:
        self._tracker_app().push_screen(SeriesPreferencesScreen(self.slug))

    def action_open_mal(self) -> None:
        if self._detail is None:
            return
        mal_url = anime_url(self._detail.entry.mal_anime_id)
        if mal_url is None:
            self.query_one("#playback-status", Static).update(
                "Series is not linked to MAL.",
            )
            return
        if not webbrowser.open(mal_url):
            self.query_one("#playback-status", Static).update(
                "Failed to open MAL page in the browser.",
            )
            return
        self.query_one("#playback-status", Static).update(
            "Opened MAL page in the browser.",
        )

    def action_show_info(self) -> None:
        if self._detail is None or self._detail.mal_anime_info is None:
            return
        self._tracker_app().push_screen(
            SeriesInfoScreen(
                slug=self.slug,
                title=self._detail.entry.title,
                anime_info=self._detail.mal_anime_info,
            ),
        )

    def action_focus_next_action(self) -> None:
        buttons = [
            self.query_one("#play", Button),
            self.query_one("#info", Button),
            self.query_one("#preferences", Button),
            self.query_one("#edit", Button),
            self.query_one("#back", Button),
            self.query_one("#refresh", Button),
        ]
        if self._mal_linked:
            buttons.insert(2, self.query_one("#open-mal", Button))
        self._focus_adjacent_action(buttons, 1)

    def action_focus_previous_action(self) -> None:
        buttons = [
            self.query_one("#play", Button),
            self.query_one("#info", Button),
            self.query_one("#preferences", Button),
            self.query_one("#edit", Button),
            self.query_one("#back", Button),
            self.query_one("#refresh", Button),
        ]
        if self._mal_linked:
            buttons.insert(2, self.query_one("#open-mal", Button))
        self._focus_adjacent_action(buttons, -1)

    def action_play_selected(self) -> None:
        if self._playing:
            return
        self._play(self._selected_episode_label())

    @on(Button.Pressed, ".mal-rate-button")
    def handle_rating_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("rate-"):
            return
        self._rate_on_mal(int(button_id.removeprefix("rate-")))

    @on(Button.Pressed, "#play")
    def handle_play_button(self) -> None:
        self.action_play_selected()

    @on(Button.Pressed, "#info")
    def handle_info_button(self) -> None:
        self.action_show_info()

    @on(Button.Pressed, "#open-mal")
    def handle_open_mal_button(self) -> None:
        self.action_open_mal()

    @on(Button.Pressed, "#preferences")
    def handle_preferences_button(self) -> None:
        self.action_show_preferences()

    @on(Button.Pressed, "#edit")
    def handle_edit_button(self) -> None:
        self.action_edit_series()

    @on(Button.Pressed, "#back")
    def handle_back_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Button.Pressed, "#refresh")
    def handle_refresh_button(self) -> None:
        self.load_detail()

    @on(DataTable.RowSelected, "#episode-list")
    def handle_episode_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key.value
        if row_key is None:
            return
        label = self._episode_label_by_row_key.get(row_key)
        if label is not None:
            self._play(label)

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
        table = self.query_one("#episode-list", DataTable)
        row_index = table.cursor_row
        if row_index < 0 or row_index >= table.row_count:
            return None
        row_key = table.ordered_rows[row_index].key.value
        if row_key is None:
            return None
        return self._episode_label_by_row_key.get(row_key)

    def _focus_adjacent_action(self, buttons: list[Button], step: int) -> None:
        focused = self.focused
        try:
            index = buttons.index(cast("Button", focused))
        except ValueError:
            return
        buttons[(index + step) % len(buttons)].focus()

    @work(thread=True, exclusive=False)
    def _rate_on_mal(self, score: int) -> None:
        app = self._tracker_app()
        app.call_from_thread(
            self.query_one("#playback-status", Static).update,
            f"Saving MAL score {score}...",
        )
        try:
            app.service.rate_series_on_mal(self.slug, score=score)
        except ValueError as error:
            app.call_from_thread(
                self.query_one("#playback-status", Static).update,
                str(error),
            )
            return
        except Exception as error:  # noqa: BLE001
            app.report_exception(error)
            app.call_from_thread(
                self.query_one("#playback-status", Static).update,
                f"Failed to save MAL rating: {error}",
            )
            return
        app.call_from_thread(self._apply_saved_mal_score, score)

    def _apply_saved_mal_score(self, score: int) -> None:
        self._selected_mal_score = score
        self._sync_mal_rating_buttons()
        if self._detail is not None and self._detail.mal_anime_info is not None:
            self._detail = dataclass_replace(
                self._detail,
                mal_anime_info=dataclass_replace(
                    self._detail.mal_anime_info,
                    score=float(score),
                ),
            )
            self.query_one("#detail-mal", Static).update(
                _format_detail_mal_text(self._detail),
            )
        self.query_one("#playback-status", Static).update(
            f"Saved MAL score {score}.",
        )

    def _sync_mal_rating_buttons(self) -> None:
        for score in range(1, 11):
            button = self.query_one(f"#rate-{score}", Button)
            button.disabled = not self._mal_rating_enabled
            button.variant = (
                "primary"
                if self._selected_mal_score == score and self._mal_rating_enabled
                else "default"
            )

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class SeriesInfoScreen(Screen[None]):
    """Expanded MAL info screen for a linked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "close_screen", "Back"),
        ("r", "refresh_info", "Refresh"),
    ]

    def __init__(self, *, slug: str, title: str, anime_info: MALAnimeInfo) -> None:
        super().__init__()
        self.slug = slug
        self.title = title
        self.anime_info = anime_info

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-view"):
            yield Static(f"{self.title} Info", id="detail-title")
            yield Static("", id="series-info-status")
            with Horizontal(id="detail-actions"):
                yield Button("Refresh", id="refresh-series-info")
                yield Button("Back", id="close-series-info")
            with VerticalScroll(id="series-info-scroll"):
                yield Static(_format_mal_info_screen(self.anime_info), id="series-info")
        yield Footer()

    def action_close_screen(self) -> None:
        self.app.pop_screen()

    def action_refresh_info(self) -> None:
        self._refresh_info()

    @on(Button.Pressed, "#refresh-series-info")
    def handle_refresh_button(self) -> None:
        self._refresh_info()

    @on(Button.Pressed, "#close-series-info")
    def handle_close_button(self) -> None:
        self.app.pop_screen()

    @work(thread=True, exclusive=True)
    def _refresh_info(self) -> None:
        app = self._tracker_app()
        app.call_from_thread(
            self.query_one("#series-info-status", Static).update,
            "Refreshing MAL metadata...",
        )
        refreshed = app.service.refresh_series_mal_anime_info(self.slug)
        if refreshed is None:
            app.call_from_thread(
                self.query_one("#series-info-status", Static).update,
                "MAL metadata could not be refreshed.",
            )
            return
        app.call_from_thread(self._apply_refreshed_info, refreshed)

    def _apply_refreshed_info(self, anime_info: MALAnimeInfo) -> None:
        self.anime_info = anime_info
        self.query_one("#series-info", Static).update(
            _format_mal_info_screen(anime_info),
        )
        self.query_one("#series-info-status", Static).update(
            "Refreshed MAL metadata.",
        )

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class SpeedSettingsModal(ModalScreen[float | None]):
    """Modal speed picker similar to a focused playback-speed panel."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Close"),
    ]

    def __init__(self, initial_speed: float) -> None:
        super().__init__()
        self._speed = initial_speed

    def compose(self) -> ComposeResult:
        with Grid(id="speed-modal"), Vertical(id="speed-modal-panel"):
            yield Static("Playback Speed", id="speed-modal-title")
            yield Static("", id="speed-modal-value")
            with Grid(id="speed-modal-adjust-row"):
                yield Button(
                    "-",
                    id="speed-modal-decrease",
                    classes="speed-modal-step-button",
                )
                yield ProgressBar(
                    total=150,
                    show_percentage=False,
                    show_eta=False,
                    id="speed-modal-bar",
                )
                yield Button(
                    "+",
                    id="speed-modal-increase",
                    classes="speed-modal-step-button",
                )
            with Grid(id="speed-modal-presets"):
                for speed in SPEED_PRESETS:
                    yield Button(
                        f"{speed:.2f}".rstrip("0").rstrip("."),
                        id=f"speed-modal-preset-{str(speed).replace('.', '-')}",
                        classes="speed-modal-preset-button",
                    )
            with Horizontal(id="speed-modal-actions"):
                yield Button("Apply", id="speed-modal-apply", variant="primary")
                yield Button("Cancel", id="speed-modal-cancel")

    def on_mount(self) -> None:
        self._update_controls()
        self.query_one("#speed-modal-apply", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#speed-modal-decrease")
    def handle_decrease(self) -> None:
        self._set_speed(self._speed - 0.01)

    @on(Button.Pressed, "#speed-modal-increase")
    def handle_increase(self) -> None:
        self._set_speed(self._speed + 0.01)

    @on(Button.Pressed, "#speed-modal-apply")
    def handle_apply(self) -> None:
        self.dismiss(self._speed)

    @on(Button.Pressed, "#speed-modal-cancel")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, ".speed-modal-preset-button")
    def handle_preset(self, event: Button.Pressed) -> None:
        if event.button.id is None or not event.button.id.startswith(
            "speed-modal-preset-",
        ):
            return
        speed = float(
            event.button.id.removeprefix("speed-modal-preset-").replace("-", "."),
        )
        self._set_speed(speed)

    def _set_speed(self, value: float) -> None:
        self._speed = max(0.5, min(round(value, 2), 2.0))
        self._update_controls()

    def _update_controls(self) -> None:
        self.query_one("#speed-modal-value", Static).update(f"{self._speed:.2f}x")
        self.query_one("#speed-modal-bar", ProgressBar).update(
            progress=_speed_progress_value(self._speed),
            total=150,
        )
        for speed in SPEED_PRESETS:
            button = self.query_one(
                f"#speed-modal-preset-{str(speed).replace('.', '-')}",
                Button,
            )
            button.variant = "primary" if round(speed, 2) == self._speed else "default"


class SeriesPreferencesScreen(Screen[None]):
    """Per-series preference editor."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Save"),
    ]

    def __init__(self, slug: str) -> None:
        super().__init__()
        self.slug = slug
        self._audio_choice_by_id: dict[str, int | None] = {}
        self._subtitle_choice_by_id: dict[str, int | None] = {}
        self._show_filler_details = False
        self._entry: LibraryEntry | None = None
        self._preferred_playback_speed = 1.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="app-settings-view"):
            with VerticalScroll(id="series-preferences-scroll"):
                yield Static("Series Preferences", id="detail-title")
                yield Static(
                    (
                        "Configure how fresh episodes should start. "
                        "Leave empty to start from the beginning."
                    ),
                    id="app-settings-status",
                )
                yield Input(
                    placeholder="Start chapter, for example 2",
                    id="series-start-chapter",
                )
                yield Static("Playback Speed", id="settings-section-title")
                yield Static(
                    (
                        "Choose a default playback speed for this series. "
                        "Open the speed picker to change the default playback speed."
                    ),
                    id="settings-section-help",
                )
                yield Static("", id="series-speed-display")
                yield Button("Adjust Speed", id="open-speed-modal")
                yield Static("Default Audio", id="audio-settings-section-title")
                yield Static(
                    "Choose the audio track to apply when playback starts.",
                    id="audio-settings-section-help",
                )
                yield Vertical(id="series-audio-options")
                yield Static("Default Subtitles", id="subtitle-settings-section-title")
                yield Static(
                    "Choose the subtitle track to apply when playback starts.",
                    id="subtitle-settings-section-help",
                )
                yield Vertical(id="series-subtitle-options")
                yield Static("AnimeFillerList", id="animefiller-section-title")
                yield Static(
                    (
                        "Paste an AnimeFillerList show URL to cache filler episodes. "
                        "You can then skip filler episodes automatically during "
                        "playback."
                    ),
                    id="animefiller-section-help",
                )
                yield Input(
                    placeholder="https://www.animefillerlist.com/shows/dragon-ball",
                    id="series-animefiller-url",
                )
                yield Static("", id="series-animefiller-status")
                yield Button(
                    "Show",
                    id="show-filler-details",
                    classes="compact-secondary-button",
                )
                yield Static("", id="series-filler-details")
                yield Vertical(id="series-filler-options")
            with Horizontal(id="detail-actions"):
                yield Button("Save", id="save-series-preferences", variant="primary")
                yield Button("Cancel", id="cancel-series-preferences")
        yield Footer()

    def on_mount(self) -> None:
        entry = self._tracker_app().service.resolve_entry(self.slug)
        self._entry = entry
        self._preferred_playback_speed = entry.preferred_playback_speed
        if entry.start_chapter_index is not None:
            self.query_one("#series-start-chapter", Input).value = str(
                entry.start_chapter_index + 1,
            )
        self.query_one("#series-animefiller-url", Input).value = entry.animefiller_url
        self._mount_track_sets(entry)
        self._mount_filler_toggle(entry)
        self._update_filler_status(entry)
        self._update_speed_controls()
        self.query_one("#series-preferences-scroll", VerticalScroll).refresh(
            layout=True,
        )
        self.query_one("#series-start-chapter", Input).focus()

    def action_cancel(self) -> None:
        self._tracker_app().pop_screen()

    def action_submit(self) -> None:
        self._submit()

    @on(Button.Pressed, "#save-series-preferences")
    def handle_save_button(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel-series-preferences")
    def handle_cancel_button(self) -> None:
        self._tracker_app().pop_screen()

    @on(Button.Pressed, "#show-filler-details")
    def handle_show_filler_details_button(self) -> None:
        self._show_filler_details = not self._show_filler_details
        self._update_filler_details()
        self.query_one("#series-preferences-scroll", VerticalScroll).refresh(
            layout=True,
        )

    @on(Button.Pressed, "#open-speed-modal")
    def handle_open_speed_modal(self) -> None:
        self.app.push_screen(
            SpeedSettingsModal(self._preferred_playback_speed),
            callback=self._apply_selected_speed,
        )

    @on(Input.Submitted, "#series-start-chapter")
    def handle_input_submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        raw_value = self.query_one("#series-start-chapter", Input).value.strip()
        start_chapter: int | None = None
        if raw_value:
            if not raw_value.isdigit():
                self.query_one("#app-settings-status", Static).update(
                    "Start chapter must be a positive integer.",
                )
                return
            start_chapter = int(raw_value)
        try:
            audio_choice = self._selected_track_choice(
                "#series-audio-set",
                self._audio_choice_by_id,
            )
            subtitle_choice = self._selected_track_choice(
                "#series-subtitle-set",
                self._subtitle_choice_by_id,
            )
            entry = self._tracker_app().service.update_series_preferences(
                self.slug,
                start_chapter=start_chapter,
                preferred_audio_track_id=audio_choice,
                preferred_subtitle_track_id=subtitle_choice,
                preferred_playback_speed=self._preferred_playback_speed,
                animefiller_url=self.query_one(
                    "#series-animefiller-url",
                    Input,
                ).value,
                skip_fillers=self._selected_skip_fillers(),
            )
        except ValueError as error:
            self.query_one("#app-settings-status", Static).update(str(error))
            return

        app = self._tracker_app()
        message = "Updated series preferences."
        if (
            entry.start_chapter_index is not None
            or entry.preferred_audio_track_id is not None
            or entry.preferred_subtitle_track_id is not None
            or entry.preferred_playback_speed != 1.0
            or entry.animefiller_url
            or entry.skip_fillers
        ):
            message = f"Updated preferences for {entry.title}."
        app.library_message = message
        app.pop_screen()
        if isinstance(app.screen, SeriesDetailScreen):
            app.screen.load_detail()
        app.refresh_library()

    def _mount_track_sets(self, entry: "LibraryEntry") -> None:
        service = self._tracker_app().service
        try:
            audio_tracks, subtitle_tracks = service.get_series_track_options(self.slug)
        except Exception as error:  # noqa: BLE001
            self.query_one("#app-settings-status", Static).update(
                f"Failed to probe tracks: {error}",
            )
            audio_tracks, subtitle_tracks = ([], [])

        audio_set = self._build_track_set(
            options=audio_tracks,
            current_value=entry.preferred_audio_track_id,
            radio_set_id="series-audio-set",
            choice_by_id=self._audio_choice_by_id,
        )
        subtitle_set = self._build_track_set(
            options=subtitle_tracks,
            current_value=entry.preferred_subtitle_track_id,
            radio_set_id="series-subtitle-set",
            choice_by_id=self._subtitle_choice_by_id,
        )
        self.query_one("#series-audio-options", Vertical).mount(audio_set)
        self.query_one("#series-subtitle-options", Vertical).mount(subtitle_set)

    def _mount_filler_toggle(self, entry: "LibraryEntry") -> None:
        filler_set = RadioSet(
            RadioButton("Do not skip filler", value=not entry.skip_fillers),
            RadioButton("Skip filler episodes", value=entry.skip_fillers),
            id="series-filler-set",
            compact=True,
        )
        self.query_one("#series-filler-options", Vertical).mount(filler_set)

    def _build_track_set(
        self,
        *,
        options: list[MediaTrackOption],
        current_value: int | None,
        radio_set_id: str,
        choice_by_id: dict[str, int | None],
    ) -> RadioSet:
        buttons: list[RadioButton] = []
        choices = [
            ("Default", None),
            ("Disabled", 0),
            *[(option.label, option.track_id) for option in options],
        ]
        selected_value = current_value
        for index, (label, value) in enumerate(choices):
            button_id = f"{radio_set_id}-{index}"
            choice_by_id[button_id] = value
            buttons.append(
                RadioButton(
                    label,
                    value=value == selected_value,
                    id=button_id,
                ),
            )
        return RadioSet(*buttons, id=radio_set_id, compact=True)

    def _selected_track_choice(
        self,
        selector: str,
        choice_by_id: dict[str, int | None],
    ) -> int | None:
        radio_set = self.query_one(selector, RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.id is None:
            return None
        return choice_by_id.get(pressed.id)

    def _selected_skip_fillers(self) -> bool:
        radio_set = self.query_one("#series-filler-set", RadioSet)
        pressed = radio_set.pressed_button
        if pressed is None or pressed.label is None:
            return False
        return str(pressed.label) == "Skip filler episodes"

    def _update_filler_status(self, entry: "LibraryEntry") -> None:
        if not entry.animefiller_url:
            message = "AnimeFillerList URL is not configured."
        elif not entry.filler_episode_numbers:
            message = "Cached filler episodes: none reported."
        else:
            refreshed_at = _format_activity_timestamp(entry.filler_updated_at)
            message = (
                f"Cached filler episodes: {len(entry.filler_episode_numbers)} "
                f"(last refreshed {refreshed_at})"
            )
        self.query_one("#series-animefiller-status", Static).update(message)
        self._update_filler_details()

    def _update_filler_details(self) -> None:
        button = self.query_one("#show-filler-details", Button)
        details = self.query_one("#series-filler-details", Static)
        if not self._show_filler_details:
            button.label = "Show"
            details.update("")
            return

        button.label = "Hide"
        entry = self._entry
        if entry is None or not entry.animefiller_url:
            details.update("Configure an AnimeFillerList URL to view episode lists.")
            return

        episodes = discover_episodes(entry.directory)
        if not episodes:
            details.update("No local episodes were found for this series.")
            return

        filler_numbers = sorted(set(entry.filler_episode_numbers))
        canon_numbers = [
            episode.index
            for episode in episodes
            if episode.index not in set(filler_numbers)
        ]
        filler_text = _format_episode_number_ranges(filler_numbers)
        canon_text = _format_episode_number_ranges(canon_numbers)
        details.update(f"Filler: {filler_text}\nCanon: {canon_text}")

    def _set_playback_speed(self, value: float) -> None:
        self._preferred_playback_speed = max(0.5, min(round(value, 2), 2.0))
        self._update_speed_controls()

    def _apply_selected_speed(self, value: float | None) -> None:
        if value is None:
            return
        self._set_playback_speed(value)

    def _update_speed_controls(self) -> None:
        self.query_one("#series-speed-display", Static).update(
            f"Default speed: {self._preferred_playback_speed:.2f}x",
        )

    def _tracker_app(self) -> MPVTrackerApp:
        return cast("MPVTrackerApp", self.app)


class HelpScreen(Screen[None]):
    """Quick reference for the TUI."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "close_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-view"):
            yield Static("Help", id="detail-title")
            with VerticalScroll(id="series-info-scroll"):
                yield Static(_help_text(), id="series-info")
        yield Footer()

    def action_close_screen(self) -> None:
        self.app.pop_screen()


class RecentActivityScreen(Screen[None]):
    """Recently watched playback sessions."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "close_screen", "Back"),
        ("enter", "open_selected", "Open"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="detail-view"):
            yield Static("Recent Activity", id="detail-title")
            yield Static("", id="series-info-status")
            yield ListView(id="recent-activity-list")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_activity()

    def action_close_screen(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self.refresh_activity()

    def action_open_selected(self) -> None:
        list_view = self.query_one("#recent-activity-list", ListView)
        highlighted = list_view.highlighted_child
        if isinstance(highlighted, RecentActivityListItem):
            self.app.push_screen(SeriesDetailScreen(highlighted.slug))

    @on(ListView.Selected, "#recent-activity-list")
    def handle_open_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, RecentActivityListItem):
            self.app.push_screen(SeriesDetailScreen(event.item.slug))

    def refresh_activity(self) -> None:
        entries = self._tracker_app().service.list_recent_activity()
        status = self.query_one("#series-info-status", Static)
        list_view = self.query_one("#recent-activity-list", ListView)
        list_view.clear()
        if not entries:
            status.update("No recent activity yet.")
            return
        status.update("Most recent playback sessions, newest first.")
        for entry in entries:
            list_view.append(RecentActivityListItem(entry))
        list_view.index = 0
        list_view.focus()

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

    #app-settings-view {
        height: 1fr;
    }

    #series-preferences-scroll {
        height: 1fr;
        margin-bottom: 1;
    }

    #series-audio-options, #series-subtitle-options, #series-filler-options,
    #series-filler-details {
        height: auto;
        margin-bottom: 1;
    }

    #speed-modal {
        width: 100%;
        height: 100%;
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }

    #speed-modal-panel {
        width: 64;
        height: auto;
        border: round #355070;
        background: #16212b;
        padding: 1 2;
    }

    #speed-modal-title {
        text-style: bold;
        color: #f6bd60;
        margin-bottom: 1;
    }

    #speed-modal-value {
        color: #edf2f7;
        text-style: bold;
        margin-bottom: 1;
    }

    #speed-modal-adjust-row {
        grid-size: 3;
        grid-columns: 7 1fr 7;
        grid-gutter: 0;
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }

    #speed-modal-bar {
        width: 100%;
        height: 3;
        color: #4ea8de;
        margin: 0;
    }

    #speed-modal-presets {
        grid-size: 6;
        grid-columns: 1fr 1fr 1fr 1fr 1fr 1fr;
        grid-gutter: 1;
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    #speed-modal-actions {
        height: auto;
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

    #mal-rating-actions {
        height: auto;
        margin-bottom: 1;
    }

    #title, #detail-title {
        text-style: bold;
        color: #f6bd60;
        margin-bottom: 1;
    }

    #library-status, #detail-summary, #detail-directory, #detail-mal,
    #detail-rating-title,
    #detail-preferences,
    #playback-status, #add-series-status, #confirm-remove-message,
    #confirm-remove-status, #mal-settings-status, #app-settings-status,
    #settings-section-title, #settings-section-help {
        margin-bottom: 1;
    }

    #series-table-header {
        padding: 0 1;
        height: 1;
        margin-bottom: 0;
    }

    #episode-table-header {
        padding: 0 3 0 1;
        height: 1;
        margin-bottom: 0;
    }

    .series-header-cell, .series-header-button {
        color: #9fb3c8;
        text-style: bold;
        background: transparent;
        border: none;
        padding: 0;
        min-height: 1;
        height: 1;
        content-align: left middle;
    }

    .episode-header-cell {
        color: #9fb3c8;
        text-style: bold;
        background: transparent;
        border: none;
        padding: 0;
        min-height: 1;
        height: 1;
        content-align: left middle;
    }

    .series-header-button {
        margin-right: 0;
        color: #f6bd60;
        text-style: bold;
    }

    .series-header-button:focus {
        background: #264653;
        color: #f1faee;
        text-style: bold;
        border: none;
    }

    #sort-title {
        width: 3fr;
        padding-right: 1;
    }

    .progress-column {
        width: 9;
        content-align: right middle;
        padding-right: 1;
    }

    .current-column {
        width: 4fr;
        padding-right: 1;
    }

    .resume-column {
        width: 8;
        content-align: right middle;
        padding-right: 1;
    }

    .added-column {
        width: 11;
    }

    .episode-index-column {
        width: 5;
        padding-right: 1;
    }

    .episode-name-column {
        width: 1fr;
        padding-right: 1;
    }

    .episode-canon-column {
        width: 5;
        content-align: center middle;
    }

    #settings-section-title {
        text-style: bold;
        color: #f6bd60;
    }

    #settings-section-help {
        color: #9fb3c8;
    }

    #detail-directory, #detail-mal, #detail-preferences, #detail-rating-title {
        color: #9fb3c8;
    }

    #mal-rating {
        height: auto;
        margin-bottom: 1;
    }

    #detail-actions {
        height: auto;
        margin-bottom: 1;
    }

    ListView {
        border: round #355070;
        background: #16212b;
    }

    #episode-list {
        border: round #355070;
        background: #16212b;
        height: 1fr;
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

    .mal-rate-button {
        height: 1;
        min-width: 3;
        width: 3;
        margin-right: 0;
        padding: 0;
        border: none;
    }

    #rate-10 {
        min-width: 4;
        width: 4;
    }

    .mal-rate-button:focus {
        border: none;
        background: #355070;
        color: #f1faee;
        text-style: bold;
    }

    .compact-secondary-button {
        width: auto;
        min-width: 5;
        height: 3;
        margin-right: 1;
        padding: 0;
        content-align: center middle;
    }

    .speed-modal-step-button {
        min-width: 7;
        width: 7;
        height: 3;
        margin-right: 0;
        padding: 0;
        content-align: center middle;
    }

    .speed-modal-preset-button {
        width: 100%;
        min-width: 0;
        height: 3;
        margin-right: 0;
        padding: 0;
        content-align: center middle;
    }

    .compact-secondary-button:focus {
        background: #355070;
        color: #f1faee;
        text-style: bold;
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


def _series_row_renderable(progress: SeriesProgress) -> Table:
    progress_text = f"{progress.watched_count}/{progress.total_count}"
    current_episode = progress.current_episode or "-"
    resume_text = "-"
    if progress.current_episode is not None:
        resume_text = _format_seconds(progress.current_position_seconds)
    added_text = _format_added_at(progress.entry.added_at)
    return _series_table(
        Text(progress.entry.title),
        Text(progress_text),
        Text(current_episode),
        Text(resume_text),
        Text(added_text),
    )


def _series_table_header_renderable() -> Table:
    return _series_table(
        Text("Title", style="bold"),
        Text("Progress", style="bold"),
        Text("Current Episode", style="bold"),
        Text("Resume", style="bold"),
        Text("Added", style="bold"),
    )


def _series_table(
    title: Text,
    progress: Text,
    current_episode: Text,
    resume: Text,
    added: Text,
) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(ratio=3, overflow="ellipsis")
    table.add_column(width=9, justify="right")
    table.add_column(ratio=4, overflow="ellipsis")
    table.add_column(width=8, justify="right")
    table.add_column(width=11)
    table.add_row(title, progress, current_episode, resume, added)
    return table


def _episode_row_renderable(
    episode_progress: EpisodeProgress,
    *,
    filler_episode_numbers: set[int],
) -> Table:
    canon = _episode_canon_marker(
        episode_progress,
        filler_episode_numbers=filler_episode_numbers,
    )
    return _episode_table(
        Text(str(episode_progress.episode.index), style="bold"),
        Text(_format_episode_label(episode_progress)),
        canon,
    )


def _episode_table(
    index: Text,
    name: Text,
    canon: Text,
) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=5)
    table.add_column(ratio=1, overflow="ellipsis")
    table.add_column(width=5, justify="center")
    table.add_row(index, name, canon)
    return table


def _episode_canon_marker(
    episode_progress: EpisodeProgress,
    *,
    filler_episode_numbers: set[int],
) -> Text:
    if episode_progress.episode.index in filler_episode_numbers:
        return Text("✗", style="bold red")
    return Text("✓", style="bold green")


def _episode_watched_marker(episode_progress: EpisodeProgress) -> Text:
    if episode_progress.watched:
        return Text("✓", style="bold green")
    return Text("✗", style="bold red")


def _episode_seen_time(episode_progress: EpisodeProgress) -> Text:
    if episode_progress.watched:
        if episode_progress.duration_seconds is not None:
            return Text(_format_seconds(episode_progress.duration_seconds))
        if episode_progress.position_seconds > 0:
            return Text(_format_seconds(episode_progress.position_seconds))
        return Text("-")
    if episode_progress.is_current or episode_progress.position_seconds > 0:
        return Text(_format_seconds(episode_progress.position_seconds))
    return Text("-")


def _filter_series_progress(
    progress_items: list[SeriesProgress],
    query: str,
) -> list[SeriesProgress]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return progress_items
    return [
        item
        for item in progress_items
        if normalized_query in item.entry.title.casefold()
        or normalized_query in item.entry.slug.casefold()
        or (
            item.current_episode is not None
            and normalized_query in item.current_episode.casefold()
        )
    ]


def _sort_series_progress(
    progress_items: list[SeriesProgress],
    *,
    sort_field: str,
    descending: bool,
) -> list[SeriesProgress]:
    if sort_field == "title":
        return sorted(
            progress_items,
            key=lambda item: (
                item.entry.title.casefold(),
                item.entry.added_at,
            ),
            reverse=descending,
        )
    return sorted(
        progress_items,
        key=lambda item: (
            item.entry.added_at,
            item.entry.title.casefold(),
        ),
        reverse=descending,
    )


def _library_sort_status(*, sort_field: str, descending: bool) -> str:
    field_label = "addition date" if sort_field == "added" else "name"
    direction_label = "descending" if descending else "ascending"
    return (
        f"Sort: {field_label}, {direction_label}. "
        "Press `t` for addition date, `n` for name, `v` to reverse."
    )


def _format_recent_activity_row(entry: "RecentActivityEntry") -> str:
    result = "completed" if entry.completed else _format_seconds(entry.position_seconds)
    watched_at = _format_activity_timestamp(entry.watched_at)
    return (
        f"{_truncate_text(entry.series_title, 26):<26}  "
        f"{_truncate_text(entry.episode_name, 40):<40}  "
        f"{result:>9}  "
        f"{watched_at:>17}"
    )


def _format_activity_timestamp(value: int) -> str:
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value, UTC).strftime("%d %b %Y %H:%M")


def _speed_progress_value(speed: float) -> int:
    return round((speed - 0.5) * 100)


def _format_episode_number_ranges(numbers: list[int]) -> str:
    if not numbers:
        return "none"
    ranges: list[str] = []
    start = numbers[0]
    end = numbers[0]
    for number in numbers[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(_range_label(start, end))
        start = number
        end = number
    ranges.append(_range_label(start, end))
    return ", ".join(ranges)


def _range_label(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _sortable_header_label(label: str, *, is_active: bool, descending: bool) -> str:
    if not is_active:
        return label
    arrow = "▼" if descending else "▲"
    return f"{label} {arrow}"


def _truncate_text(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return f"{value[: width - 1]}…"


def _format_added_at(value: int) -> str:
    if value <= 0:
        return "-"
    return datetime.fromtimestamp(value, UTC).strftime("%d %b %Y")


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
    return (
        f"{episode_progress.episode.index:>2}. "
        f"{_format_episode_label(episode_progress)}"
    )


def _format_episode_label(episode_progress: EpisodeProgress) -> str:
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
    return f"{episode_progress.episode.label} [{details}]"


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
            lines.append(f"Profile: {_terminal_hyperlink(profile, profile)}")
    else:
        lines.append("User: not loaded yet. Press Refresh to fetch account details.")
    return "\n".join(lines)


def _format_mal_anime_info(info: MALAnimeInfo) -> str:
    score = "n/a" if info.score is None else f"{info.score:.2f}"
    rank = "n/a" if info.rank is None else str(info.rank)
    popularity = "n/a" if info.popularity is None else str(info.popularity)
    return f"Score: {score} | Ranked: {rank} | Popularity: {popularity}"


def _format_detail_mal_text(detail: SeriesDetail) -> str:
    if detail.entry.mal_anime_id is None:
        return "MAL: not linked"
    mal_url = anime_url(detail.entry.mal_anime_id)
    link = ""
    if mal_url is not None:
        link = f" ({_terminal_hyperlink(mal_url, mal_url)})"
    mal_text = f"MAL: {detail.entry.mal_anime_id}{link}"
    if detail.mal_anime_info is not None:
        mal_text = f"{mal_text}\n{_format_mal_anime_info(detail.mal_anime_info)}"
    return mal_text


def _format_mal_info_screen(info: MALAnimeInfo) -> str:
    lines = [
        _format_mal_anime_info(info),
        "",
        f"Alternative Titles: {_join_or_na(info.alternative_titles)}",
        f"Media Type: {_value_or_na(info.media_type)}",
        f"Status: {_value_or_na(info.status)}",
        f"Episodes: {_int_or_na(info.num_episodes)}",
        f"Aired: {_format_aired(info.start_date, info.end_date)}",
        f"Source: {_value_or_na(info.source)}",
        (
            "Average Episode Duration: "
            f"{_format_duration(info.average_episode_duration_seconds)}"
        ),
        f"Rating: {_value_or_na(info.rating)}",
        f"Studios: {_join_or_na(info.studios)}",
        f"Genres: {_join_or_na(info.genres)}",
        "",
        "Synopsis:",
        info.synopsis or "n/a",
        "",
        "Background:",
        info.background or "n/a",
    ]
    return "\n".join(lines)


def _join_or_na(values: list[str] | None) -> str:
    if not values:
        return "n/a"
    return ", ".join(values)


def _value_or_na(value: str) -> str:
    return value or "n/a"


def _int_or_na(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _format_aired(start_date: str, end_date: str) -> str:
    if start_date and end_date:
        return f"{start_date} to {end_date}"
    if start_date:
        return f"from {start_date}"
    if end_date:
        return f"until {end_date}"
    return "n/a"


def _format_duration(value: int | None) -> str:
    if value is None or value <= 0:
        return "n/a"
    minutes, seconds = divmod(value, 60)
    if seconds == 0:
        return f"{minutes} min"
    return f"{minutes} min {seconds} sec"


def _terminal_hyperlink(url: str, label: str) -> str:
    return f'[link="{escape(url)}"]{escape(label)}[/link]'


def _help_text() -> str:
    sections = [
        "Library",
        "  Enter: Open selected series",
        "  a: Add series",
        "  e: Edit selected series",
        "  d: Remove selected series",
        "  m: MAL account",
        "  s: Settings",
        "  h or ?: Help",
        "  q: Quit",
        "",
        "Series Detail",
        "  p: Play selected episode",
        "  i: Open MAL info screen",
        "  m: Open MAL page in browser",
        "  o: Open series preferences",
        "  e: Edit series",
        "  r: Refresh detail",
        "  Left / Right: Move across action buttons",
        "  Enter on episode list: Play highlighted episode",
        "",
        "MAL",
        "  Authenticate from the library screen with m before using MAL sync features",
        "  Ratings, profile actions, and watched-count sync require MAL login",
        "  Linked series show MAL ID, score, rank, and popularity",
        "  Rating buttons set the MAL score directly",
        "  Info screen shows cached synopsis, titles, studios, genres, and more",
        "  Refresh inside the info screen forces a metadata cache refresh",
        "",
        "Playback",
        "  Episodes resume from saved progress automatically",
        "  Linked series sync watched count to MAL after playback",
        "  Series preferences can start fresh episodes from a configured chapter",
        "",
        "Add / Edit Series",
        "  Directory matches appear as you type",
        "  Down focuses the directory suggestions",
        "  Right descends into the highlighted directory",
        "  Ctrl+S saves the form",
        "",
        "General",
        "  Escape returns to the previous screen",
        "  Ctrl+S saves in forms and settings screens",
    ]
    return "\n".join(sections)


def _avatar_renderable(path: Path) -> object:
    try:
        textual_image = import_module("textual_image.renderable")
        return textual_image.Image(str(path), width=36)
    except ImportError as error:
        try:
            rich_pixels = import_module("rich_pixels")
        except ImportError:
            raise error from error

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
