"""Textual application for browsing tracked series and launching playback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, ListItem, ListView, Static

from mpv_tracker.service import TrackerService

if TYPE_CHECKING:
    from mpv_tracker.models import EpisodeProgress, SeriesDetail, SeriesProgress

BINDING = Binding | tuple[str, str] | tuple[str, str, str]


def run_tui() -> None:
    """Launch the Textual interface."""
    MPVTrackerApp().run()


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
        ("d", "remove_series", "Remove"),
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
                    "Enter a title and directory. Slug is optional. "
                    "Directory matches appear below as you type."
                ),
                id="add-series-status",
            )
            yield Input(placeholder="Series title", id="add-title")
            yield Input(placeholder="/path/to/series", id="add-directory")
            yield ListView(id="directory-matches")
            yield Input(placeholder="optional-slug", id="add-slug")
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
        if event.input.id == "add-slug":
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
            yield Static("", id="playback-status")
            with Horizontal(id="detail-actions"):
                yield Button("Play", id="play", variant="primary")
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

    def action_focus_next_action(self) -> None:
        focused = self.focused
        play_button = self.query_one("#play", Button)
        back_button = self.query_one("#back", Button)
        refresh_button = self.query_one("#refresh", Button)
        if focused is play_button:
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
        back_button = self.query_one("#back", Button)
        refresh_button = self.query_one("#refresh", Button)
        if focused is play_button:
            refresh_button.focus()
            return
        if focused is back_button:
            play_button.focus()
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

    #add-series-view, #confirm-remove-view {
        padding: 1 2;
    }

    #title, #detail-title {
        text-style: bold;
        color: #f6bd60;
        margin-bottom: 1;
    }

    #library-status, #detail-summary, #detail-directory, #playback-status,
    #add-series-status, #confirm-remove-message, #confirm-remove-status {
        margin-bottom: 1;
    }

    #detail-directory {
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

    def __init__(self) -> None:
        super().__init__()
        self.service = TrackerService.create_default()
        self.library_message: str | None = None

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
