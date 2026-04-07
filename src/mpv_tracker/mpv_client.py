"""MPV process launching and IPC event handling."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from errno import EPIPE
from pathlib import Path
from typing import TYPE_CHECKING

from mpv_tracker.config import WATCHED_THRESHOLD
from mpv_tracker.models import MediaTrackOption

if TYPE_CHECKING:
    from collections.abc import Callable

_SOCKET_TIMEOUT_SECONDS = 0.25
_DRAIN_IDLE_READS = 2
_OBSERVED_PROPERTIES = ("path", "time-pos", "duration", "eof-reached")


@dataclass(slots=True)
class PlaybackSnapshot:
    """Playback values collected from MPV IPC."""

    episode_name: str
    position_seconds: float
    duration_seconds: float | None
    watched: bool


@dataclass(slots=True)
class _ObservedPlaybackState:
    """Mutable playback state assembled from MPV property-change events."""

    episode_name: str
    position_seconds: float
    duration_seconds: float | None
    eof_reached: bool


class MPVWatcher:
    """Run MPV and stream playback updates through its IPC socket."""

    def __init__(  # noqa: PLR0913
        self,
        media_directory: Path,
        *,
        episode_name: str,
        playlist_start: int,
        start_position_seconds: float = 0.0,
        preferred_start_chapter_index: int | None = None,
        preferred_audio_track_id: int | None = None,
        preferred_subtitle_track_id: int | None = None,
        preferred_playback_speed: float = 1.0,
        filler_episode_names: set[str] | None = None,
    ) -> None:
        self._media_directory = media_directory
        self._episode_name = episode_name
        self._playlist_start = playlist_start
        self._start_position_seconds = start_position_seconds
        self._preferred_start_chapter_index = preferred_start_chapter_index
        self._preferred_audio_track_id = preferred_audio_track_id
        self._preferred_subtitle_track_id = preferred_subtitle_track_id
        self._preferred_playback_speed = preferred_playback_speed
        self._filler_episode_names = filler_episode_names or set()
        self._initial_seek_applied = start_position_seconds <= 0
        self._chapter_seek_episode_name: str | None = None
        self._track_selection_episode_name: str | None = None
        self._playback_speed_episode_name: str | None = None
        self._skipped_filler_episode_names: set[str] = set()

    def watch(
        self,
        *,
        on_update: Callable[[PlaybackSnapshot], None] | None = None,
    ) -> PlaybackSnapshot:
        """Run MPV and return the latest playback state."""
        with tempfile.TemporaryDirectory(prefix="mpv-tracker-") as temp_dir:
            socket_path = Path(temp_dir) / "mpv.sock"
            try:
                process = subprocess.Popen(  # noqa: S603
                    [  # noqa: S607
                        "mpv",
                        f"--input-ipc-server={socket_path}",
                        "--force-window=no",
                        f"--playlist-start={self._playlist_start}",
                        str(self._media_directory),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError as error:
                msg = "The `mpv` executable was not found in PATH."
                raise RuntimeError(msg) from error

            try:
                client = self._wait_for_socket(socket_path)
            except TimeoutError:
                process.wait()
                snapshot = PlaybackSnapshot(
                    episode_name=self._episode_name,
                    position_seconds=self._start_position_seconds,
                    duration_seconds=None,
                    watched=False,
                )
                if on_update is not None:
                    on_update(snapshot)
                return snapshot

            observed = _ObservedPlaybackState(
                episode_name=self._episode_name,
                position_seconds=self._start_position_seconds,
                duration_seconds=None,
                eof_reached=False,
            )

            with client:
                client.settimeout(_SOCKET_TIMEOUT_SECONDS)
                self._observe_properties(client)
                latest = _snapshot_from_observed_state(observed)
                while process.poll() is None:
                    message = self._read_message(client)
                    if message is None:
                        continue
                    latest = self._apply_runtime_updates(
                        client,
                        observed,
                        message,
                        latest,
                        on_update,
                    )

                return self._drain_messages(client, observed, latest, on_update)

    def _wait_for_socket(self, socket_path: Path) -> socket.socket:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if socket_path.exists():
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(os.fspath(socket_path))
                return client
            time.sleep(0.1)
        msg = "Timed out waiting for MPV IPC socket."
        raise TimeoutError(msg)

    def _observe_properties(self, client: socket.socket) -> None:
        for request_id, property_name in enumerate(_OBSERVED_PROPERTIES, start=1):
            self._send_command(
                client,
                ["observe_property", request_id, property_name],
            )

    def _handle_message(
        self,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        event_name = message.get("event")
        if event_name == "property-change":
            return _apply_property_change(observed, message)
        if event_name == "end-file":
            return _apply_end_file(observed, message)
        return None

    def _drain_messages(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        latest: PlaybackSnapshot,
        on_update: Callable[[PlaybackSnapshot], None] | None,
    ) -> PlaybackSnapshot:
        idle_reads = 0
        while idle_reads < _DRAIN_IDLE_READS:
            message = self._read_message(client)
            if message is None:
                idle_reads += 1
                continue
            idle_reads = 0
            updated = self._handle_message(observed, message)
            if updated is None:
                continue
            latest = updated
            if on_update is not None:
                on_update(latest)
        return latest

    def _maybe_apply_initial_seek(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        if self._initial_seek_applied:
            return None

        event_name = message.get("event")
        property_name = message.get("name")
        should_seek = event_name == "property-change" and property_name in {
            "time-pos",
            "duration",
        }
        if not should_seek or observed.episode_name != self._episode_name:
            return None

        self._send_command(
            client,
            ["seek", self._start_position_seconds, "absolute", "exact"],
        )
        self._initial_seek_applied = True
        observed.position_seconds = self._start_position_seconds
        return _snapshot_from_observed_state(observed)

    def _maybe_apply_preferred_start_chapter(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        preferred_start_chapter_index = self._preferred_start_chapter_index
        if preferred_start_chapter_index is None:
            return None
        if self._chapter_seek_episode_name == observed.episode_name:
            return None
        if (
            observed.episode_name == self._episode_name
            and self._start_position_seconds > 0
        ):
            return None

        event_name = message.get("event")
        property_name = message.get("name")
        should_seek = event_name == "property-change" and property_name in {
            "time-pos",
            "duration",
        }
        if not should_seek:
            return None

        self._send_command(
            client,
            ["set_property", "chapter", preferred_start_chapter_index],
        )
        self._chapter_seek_episode_name = observed.episode_name
        observed.position_seconds = 0.0
        return _snapshot_from_observed_state(observed)

    def _apply_runtime_updates(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
        latest: PlaybackSnapshot,
        on_update: Callable[[PlaybackSnapshot], None] | None,
    ) -> PlaybackSnapshot:
        latest = self._apply_optional_snapshot(
            latest,
            self._handle_message(observed, message),
            on_update,
        )
        latest = self._apply_optional_snapshot(
            latest,
            self._maybe_apply_initial_seek(client, observed, message),
            on_update,
        )
        latest = self._apply_optional_snapshot(
            latest,
            self._maybe_apply_preferred_start_chapter(client, observed, message),
            on_update,
        )
        latest = self._apply_optional_snapshot(
            latest,
            self._maybe_apply_preferred_tracks(client, observed, message),
            on_update,
        )
        latest = self._apply_optional_snapshot(
            latest,
            self._maybe_apply_preferred_speed(client, observed, message),
            on_update,
        )
        return self._apply_optional_snapshot(
            latest,
            self._maybe_skip_filler_episode(client, observed, message),
            on_update,
        )

    def _apply_optional_snapshot(
        self,
        latest: PlaybackSnapshot,
        candidate: PlaybackSnapshot | None,
        on_update: Callable[[PlaybackSnapshot], None] | None,
    ) -> PlaybackSnapshot:
        if candidate is None:
            return latest
        if on_update is not None:
            on_update(candidate)
        return candidate

    def _maybe_skip_filler_episode(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        if not self._filler_episode_names:
            return None
        if message.get("event") != "property-change" or message.get("name") != "path":
            return None
        if observed.episode_name not in self._filler_episode_names:
            return None
        if observed.episode_name in self._skipped_filler_episode_names:
            return None
        self._send_command(client, ["playlist-next", "force"])
        self._skipped_filler_episode_names.add(observed.episode_name)
        return _snapshot_from_observed_state(observed)

    def _maybe_apply_preferred_speed(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        if self._preferred_playback_speed == 1.0:
            return None
        if self._playback_speed_episode_name == observed.episode_name:
            return None
        event_name = message.get("event")
        property_name = message.get("name")
        should_apply = event_name == "property-change" and property_name in {
            "time-pos",
            "duration",
        }
        if not should_apply:
            return None
        self._send_command(
            client,
            ["set_property", "speed", self._preferred_playback_speed],
        )
        self._playback_speed_episode_name = observed.episode_name
        return _snapshot_from_observed_state(observed)

    def _maybe_apply_preferred_tracks(
        self,
        client: socket.socket,
        observed: _ObservedPlaybackState,
        message: dict[str, object],
    ) -> PlaybackSnapshot | None:
        if (
            self._preferred_audio_track_id is None
            and self._preferred_subtitle_track_id is None
        ):
            return None
        if self._track_selection_episode_name == observed.episode_name:
            return None
        event_name = message.get("event")
        property_name = message.get("name")
        should_apply = event_name == "property-change" and property_name in {
            "time-pos",
            "duration",
        }
        if not should_apply:
            return None

        if self._preferred_audio_track_id is not None:
            audio_value: str | int = (
                "no"
                if self._preferred_audio_track_id == 0
                else self._preferred_audio_track_id
            )
            self._send_command(
                client,
                ["set_property", "aid", audio_value],
            )
        if self._preferred_subtitle_track_id is not None:
            subtitle_value: str | int = (
                "no"
                if self._preferred_subtitle_track_id == 0
                else self._preferred_subtitle_track_id
            )
            self._send_command(
                client,
                ["set_property", "sid", subtitle_value],
            )
        self._track_selection_episode_name = observed.episode_name
        return _snapshot_from_observed_state(observed)

    def _send_command(self, client: socket.socket, command: list[object]) -> None:
        payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
        client.sendall(payload)

    def _read_message(self, client: socket.socket) -> dict[str, object] | None:
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            try:
                packet = client.recv(4096)
            except TimeoutError:
                return None
            except OSError as error:
                if _is_ipc_disconnect(error):
                    return None
                raise
            if not packet:
                return None
            chunks.extend(packet)
        try:
            message = json.loads(chunks.decode("utf-8").strip())
        except json.JSONDecodeError:
            return {}
        if isinstance(message, dict):
            return {
                str(key): value
                for key, value in message.items()
                if isinstance(key, str)
            }
        return {}


def probe_media_tracks(
    video_path: Path,
) -> tuple[list[MediaTrackOption], list[MediaTrackOption]]:
    """Inspect a media file through MPV and return audio and subtitle tracks."""
    with tempfile.TemporaryDirectory(prefix="mpv-tracker-probe-") as temp_dir:
        socket_path = Path(temp_dir) / "mpv.sock"
        try:
            process = subprocess.Popen(  # noqa: S603
                [  # noqa: S607
                    "mpv",
                    f"--input-ipc-server={socket_path}",
                    "--vo=null",
                    "--ao=null",
                    "--force-window=no",
                    "--no-config",
                    "--input-terminal=no",
                    "--really-quiet",
                    "--idle=no",
                    "--pause=yes",
                    "--mute=yes",
                    str(video_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as error:
            msg = "The `mpv` executable was not found in PATH."
            raise RuntimeError(msg) from error

        try:
            client = _wait_for_probe_socket(socket_path)
            with client:
                client.settimeout(0.5)
                for _ in range(40):
                    _send_probe_command(client, ["get_property", "track-list"])
                    message = _read_probe_message(client)
                    if message is None:
                        time.sleep(0.1)
                        continue
                    data = message.get("data")
                    tracks = _parse_media_tracks(data)
                    if tracks is not None:
                        _send_probe_command(client, ["quit"])
                        process.wait(timeout=5)
                        return tracks
                    time.sleep(0.1)
        finally:
            process.terminate()
            process.wait(timeout=5)
    return ([], [])


def _wait_for_probe_socket(socket_path: Path) -> socket.socket:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if socket_path.exists():
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(os.fspath(socket_path))
            return client
        time.sleep(0.1)
    msg = "Timed out waiting for MPV IPC socket."
    raise TimeoutError(msg)


def _send_probe_command(client: socket.socket, command: list[object]) -> None:
    payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
    client.sendall(payload)


def _read_probe_message(client: socket.socket) -> dict[str, object] | None:
    chunks = bytearray()
    while not chunks.endswith(b"\n"):
        try:
            packet = client.recv(4096)
        except TimeoutError:
            return None
        if not packet:
            return None
        chunks.extend(packet)
    try:
        message = json.loads(chunks.decode("utf-8").strip())
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def _parse_media_tracks(
    value: object,
) -> tuple[list[MediaTrackOption], list[MediaTrackOption]] | None:
    if not isinstance(value, list):
        return None
    audio_tracks: list[MediaTrackOption] = []
    subtitle_tracks: list[MediaTrackOption] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        track_id = item.get("id")
        track_type = item.get("type")
        if not isinstance(track_id, int) or track_type not in {"audio", "sub"}:
            continue
        label = _format_track_label(track_id, item)
        option = MediaTrackOption(
            track_id=track_id,
            track_type=track_type,
            label=label,
        )
        if track_type == "audio":
            audio_tracks.append(option)
        else:
            subtitle_tracks.append(option)
    return (audio_tracks, subtitle_tracks)


def _format_track_label(track_id: int, item: dict[str, object]) -> str:
    title = item.get("title")
    lang = item.get("lang")
    pieces = [f"{track_id}"]
    if isinstance(lang, str) and lang:
        pieces.append(lang)
    if isinstance(title, str) and title:
        pieces.append(title)
    return " - ".join(pieces)


def _apply_property_change(
    observed: _ObservedPlaybackState,
    message: dict[str, object],
) -> PlaybackSnapshot | None:
    property_name = message.get("name")
    data = message.get("data")
    snapshot: PlaybackSnapshot | None = None
    if property_name == "path":
        episode_name = _episode_name_from_path(data)
        if episode_name is None:
            return None
        observed.episode_name = episode_name
        observed.position_seconds = 0.0
        observed.duration_seconds = None
        observed.eof_reached = False
        snapshot = _snapshot_from_observed_state(observed)
    elif property_name == "time-pos":
        position_seconds = _as_float(data)
        if position_seconds is None:
            return None
        observed.position_seconds = position_seconds
        snapshot = _snapshot_from_observed_state(observed)
    elif property_name == "duration":
        observed.duration_seconds = _as_float(data)
        snapshot = _snapshot_from_observed_state(observed)
    elif property_name == "eof-reached":
        observed.eof_reached = bool(data)
        snapshot = _snapshot_from_observed_state(observed)
    return snapshot


def _apply_end_file(
    observed: _ObservedPlaybackState,
    message: dict[str, object],
) -> PlaybackSnapshot | None:
    reason = message.get("reason")
    if reason != "eof":
        return None
    observed.eof_reached = True
    return _snapshot_from_observed_state(observed)


def _snapshot_from_observed_state(
    observed: _ObservedPlaybackState,
) -> PlaybackSnapshot:
    watched = observed.eof_reached
    if (
        observed.duration_seconds is not None
        and observed.duration_seconds > 0
        and observed.position_seconds / observed.duration_seconds >= WATCHED_THRESHOLD
    ):
        watched = True
    return PlaybackSnapshot(
        episode_name=observed.episode_name,
        position_seconds=observed.position_seconds,
        duration_seconds=observed.duration_seconds,
        watched=watched,
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _episode_name_from_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value).name


def _is_ipc_disconnect(error: OSError) -> bool:
    return error.errno in {None, EPIPE}
