"""Textual application for browsing tracked series and launching playback."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

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


class LibraryScreen(Screen[None]):
    """First screen showing all tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
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
        service = self._tracker_app().service
        progress_items = service.list_progress()
        list_view = self.query_one("#series-list", ListView)
        list_view.clear()
        if not progress_items:
            self.query_one("#library-status", Static).update(
                "No series tracked yet. Use `mpv-tracker add ...` to register one.",
            )
            return

        self.query_one("#library-status", Static).update(
            "Select a series and press Enter to view details.",
        )
        for item in progress_items:
            list_view.append(SeriesListItem(item))
        list_view.index = 0

    def action_refresh(self) -> None:
        self.refresh_series()

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


class SeriesDetailScreen(Screen[None]):
    """Detail screen for a single tracked series."""

    BINDINGS: ClassVar[list[BINDING]] = [
        ("escape", "pop_screen", "Back"),
        ("p", "play_selected", "Play"),
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
        self._sync_play_button()

    def action_refresh(self) -> None:
        self.load_detail()

    def action_pop_screen(self) -> None:
        self._tracker_app().pop_screen()

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

    #title, #detail-title {
        text-style: bold;
        color: #f6bd60;
        margin-bottom: 1;
    }

    #library-status, #detail-summary, #detail-directory, #playback-status {
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
    """

    BINDINGS: ClassVar[list[BINDING]] = [
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.service = TrackerService.create_default()

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
