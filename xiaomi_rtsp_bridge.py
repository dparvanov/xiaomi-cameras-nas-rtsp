#!/usr/bin/env python3
"""Bridge Xiaomi NAS recordings into independent RTSP streams.

Each configured camera owns its own SQLite-backed replay queue and FFmpeg
publisher. A shared MediaMTX service exposes those publishers at stable RTSP
URLs for compatible viewers and recorders.

Downstream clients assign footage timestamps at *re-ingest time*. This bridge
does not alter source files or system time to imitate historical timestamps.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import quote


LOG = logging.getLogger("xiaomi_rtsp_bridge")
CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RTSP_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _parse_start_policy(value: Any, name: str) -> str:
    if value not in {"near_live", "backfill"}:
        raise ConfigurationError(f'"{name}" must be "near_live" or "backfill".')
    return value


class ConfigurationError(ValueError):
    """Raised when config.json cannot be used safely."""


class ManagedProcess(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


PublisherFactory = Callable[[list[str], Path], ManagedProcess]


@dataclass(frozen=True)
class ReplaySettings:
    extensions: frozenset[str]
    ordering: str
    file_settle_seconds: int
    poll_interval_seconds: int
    retry_limit: int
    transcode: bool
    video_preset: str


@dataclass(frozen=True)
class PublisherConfig:
    host: str
    rtsp_port: int
    api_port: int
    ffmpeg_path: Path
    username: str
    password: str


@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    name: str
    watch_roots: tuple[Path, ...]
    rtsp_path: str
    settings: ReplaySettings
    start_policy: str = "near_live"

    def publisher_url(self, publisher: PublisherConfig) -> str:
        user = quote(publisher.username, safe="")
        password = quote(publisher.password, safe="")
        return f"rtsp://{user}:{password}@{publisher.host}:{publisher.rtsp_port}/{self.rtsp_path}"

    def display_url(self, publisher: PublisherConfig) -> str:
        return f"rtsp://{publisher.host}:{publisher.rtsp_port}/{self.rtsp_path}"


@dataclass(frozen=True)
class BridgeConfig:
    config_path: Path
    publisher: PublisherConfig
    recordings_root: Path
    defaults: ReplaySettings
    cameras: tuple[CameraConfig, ...]
    state_db: Path
    log_file: Path
    health_file: Path
    status_interval_seconds: int

    @classmethod
    def from_json(cls, config_path: Path) -> "BridgeConfig":
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"Configuration file does not exist: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid JSON in {config_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigurationError("The configuration root must be a JSON object.")

        base = config_path.parent

        def nonempty_string(value: Any, name: str) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f'"{name}" must be a non-empty string.')
            return value.strip()

        def integer(value: Any, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
            if value is None:
                value = default
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
                suffix = f" through {maximum}" if maximum is not None else " or more"
                raise ConfigurationError(f'"{name}" must be an integer from {minimum}{suffix}.')
            return value

        def config_path_value(value: Any, name: str) -> Path:
            text = nonempty_string(value, name)
            path = Path(text).expanduser()
            return (path if path.is_absolute() else base / path).resolve(strict=False)

        def normalize_extensions(value: Any, name: str) -> frozenset[str]:
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                raise ConfigurationError(f'"{name}" must be a non-empty list such as [".mp4"].')
            return frozenset(item.lower() if item.startswith(".") else f".{item.lower()}" for item in value)

        def parse_settings(value: Any, label: str, base_settings: ReplaySettings | None = None) -> ReplaySettings:
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise ConfigurationError(f'"{label}" must be an object.')
            inherited = base_settings
            extensions = normalize_extensions(value.get("extensions", list(inherited.extensions) if inherited else [".mp4"]), f"{label}.extensions")
            ordering = value.get("ordering", inherited.ordering if inherited else "path")
            if ordering not in {"path", "mtime"}:
                raise ConfigurationError(f'"{label}.ordering" must be "path" or "mtime".')
            transcode = value.get("transcode", inherited.transcode if inherited else True)
            if not isinstance(transcode, bool):
                raise ConfigurationError(f'"{label}.transcode" must be true or false.')
            video_preset = value.get("video_preset", inherited.video_preset if inherited else "veryfast")
            if not isinstance(video_preset, str) or not video_preset.strip():
                raise ConfigurationError(f'"{label}.video_preset" must be a non-empty string.')
            return ReplaySettings(
                extensions=extensions,
                ordering=ordering,
                file_settle_seconds=integer(value.get("file_settle_seconds"), f"{label}.file_settle_seconds", inherited.file_settle_seconds if inherited else 90, 1),
                poll_interval_seconds=integer(value.get("poll_interval_seconds"), f"{label}.poll_interval_seconds", inherited.poll_interval_seconds if inherited else 10, 1),
                retry_limit=integer(value.get("retry_limit"), f"{label}.retry_limit", inherited.retry_limit if inherited else 3, 1),
                transcode=transcode,
                video_preset=video_preset.strip(),
            )

        publisher_raw = raw.get("publisher")
        if not isinstance(publisher_raw, dict):
            raise ConfigurationError('"publisher" must be an object.')
        password_env = nonempty_string(publisher_raw.get("password_env"), "publisher.password_env")
        password = os.environ.get(password_env)
        if not password:
            raise ConfigurationError(f'Environment variable "{password_env}" is required for the RTSP publisher password.')
        publisher = PublisherConfig(
            host=nonempty_string(publisher_raw.get("host", "mediamtx"), "publisher.host"),
            rtsp_port=integer(publisher_raw.get("rtsp_port"), "publisher.rtsp_port", 8554, 1, 65535),
            api_port=integer(publisher_raw.get("api_port"), "publisher.api_port", 9997, 1, 65535),
            ffmpeg_path=config_path_value(publisher_raw.get("ffmpeg_path", "/usr/bin/ffmpeg"), "publisher.ffmpeg_path"),
            username=nonempty_string(publisher_raw.get("username", "bridge"), "publisher.username"),
            password=password,
        )

        defaults = parse_settings(raw.get("defaults"), "defaults")
        cameras_raw = raw.get("cameras", [])
        if not isinstance(cameras_raw, list):
            raise ConfigurationError('"cameras" must be a list when present.')

        cameras: list[CameraConfig] = []
        ids: set[str] = set()
        paths: set[str] = set()
        for index, camera_raw in enumerate(cameras_raw):
            label = f"cameras[{index}]"
            if not isinstance(camera_raw, dict):
                raise ConfigurationError(f'"{label}" must be an object.')
            camera_id = nonempty_string(camera_raw.get("id"), f"{label}.id").lower()
            if not CAMERA_ID_RE.fullmatch(camera_id):
                raise ConfigurationError(f'"{label}.id" must contain only lowercase letters, digits, hyphens, and underscores.')
            if camera_id in ids:
                raise ConfigurationError(f'Duplicate camera id: "{camera_id}".')
            ids.add(camera_id)

            rtsp_path = nonempty_string(camera_raw.get("rtsp_path"), f"{label}.rtsp_path").strip("/")
            if not RTSP_PATH_RE.fullmatch(rtsp_path) or ".." in rtsp_path.split("/"):
                raise ConfigurationError(f'"{label}.rtsp_path" is not a safe RTSP path.')
            if rtsp_path in paths:
                raise ConfigurationError(f'Duplicate RTSP path: "{rtsp_path}".')
            paths.add(rtsp_path)

            roots_raw = camera_raw.get("watch_roots")
            if not isinstance(roots_raw, list) or not roots_raw or not all(isinstance(item, str) and item.strip() for item in roots_raw):
                raise ConfigurationError(f'"{label}.watch_roots" must be a non-empty list of NAS folders.')
            roots = tuple(config_path_value(item, f"{label}.watch_roots") for item in roots_raw)
            if len(set(roots)) != len(roots):
                raise ConfigurationError(f'"{label}.watch_roots" contains a duplicate folder.')

            cameras.append(
                CameraConfig(
                    camera_id=camera_id,
                    name=nonempty_string(camera_raw.get("name", camera_id), f"{label}.name"),
                    watch_roots=roots,
                    rtsp_path=rtsp_path,
                    settings=parse_settings(camera_raw.get("settings"), f"{label}.settings", defaults),
                    start_policy=_parse_start_policy(camera_raw.get("start_policy", "near_live"), f"{label}.start_policy"),
                )
            )

        return cls(
            config_path=config_path,
            publisher=publisher,
            recordings_root=config_path_value(raw.get("recordings_root", "/recordings"), "recordings_root"),
            defaults=defaults,
            cameras=tuple(cameras),
            state_db=config_path_value(raw.get("state_db", "data/state.sqlite3"), "state_db"),
            log_file=config_path_value(raw.get("log_file", "logs/bridge.log"), "log_file"),
            health_file=config_path_value(raw.get("health_file", "data/health.json"), "health_file"),
            status_interval_seconds=integer(raw.get("status_interval_seconds"), "status_interval_seconds", 60, 5),
        )


@dataclass(frozen=True)
class Clip:
    path: Path
    size: int
    mtime_ns: int
    attempts: int


@dataclass(frozen=True)
class WorkerSnapshot:
    state: str
    detail: str = ""


class ClipState:
    """Persistent queue plus a durable near-live high-water mark per camera."""

    def __init__(self, db_path: Path, camera_id: str) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.camera_id = camera_id
        self._connection = sqlite3.connect(db_path, timeout=15)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=15000")
        self._ensure_schema()
        self._connection.execute("UPDATE clips SET status = 'pending' WHERE camera_id = ? AND status = 'playing'", (self.camera_id,))
        self._connection.commit()

    def _ensure_schema(self) -> None:
        table_sql = self._connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'clips'").fetchone()
        if table_sql and ("camera_id" not in table_sql[0] or "skipped" not in table_sql[0]):
            self._connection.execute("ALTER TABLE clips RENAME TO clips_legacy_backup")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS clips (
                camera_id TEXT NOT NULL, source_path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'playing', 'completed', 'failed', 'skipped')),
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at INTEGER NOT NULL,
                PRIMARY KEY(camera_id, source_path)
            )
            """
        )
        self._connection.execute("CREATE INDEX IF NOT EXISTS clips_pending_idx ON clips(camera_id, status, mtime_ns, source_path)")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS camera_state (
                camera_id TEXT PRIMARY KEY, policy TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0,
                highwater_path TEXT, highwater_mtime_ns INTEGER, updated_at INTEGER NOT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _key(clip: Clip, ordering: str) -> tuple[Any, ...]:
        return (str(clip.path).casefold(),) if ordering == "path" else (clip.mtime_ns, str(clip.path).casefold())

    def discover(self, clips: Iterable[Clip], ordering: str = "path", policy: str = "backfill") -> int:
        """Queue clips according to an immutable first-enable policy."""
        clips = list(clips)
        now = int(time.time())
        state = self._connection.execute("SELECT policy, initialized, highwater_path, highwater_mtime_ns FROM camera_state WHERE camera_id = ?", (self.camera_id,)).fetchone()
        if state is None:
            effective_policy, initialized, high_path, high_mtime = policy, False, None, None
            self._connection.execute("INSERT INTO camera_state(camera_id, policy, initialized, updated_at) VALUES (?, ?, 0, ?)", (self.camera_id, policy, now))
        else:
            effective_policy, initialized, high_path, high_mtime = state
        if not clips:
            self._connection.commit()
            return 0

        if effective_policy == "near_live" and not initialized:
            newest = max(clips, key=lambda clip: self._key(clip, ordering))
            for clip in clips:
                status = "pending" if clip == newest else "skipped"
                self._upsert(clip, status, now)
            self._connection.execute(
                "UPDATE camera_state SET initialized = 1, highwater_path = ?, highwater_mtime_ns = ?, updated_at = ? WHERE camera_id = ?",
                (str(newest.path), newest.mtime_ns, now, self.camera_id),
            )
            self._connection.commit()
            return 1

        discovered = 0
        high_clip = Clip(Path(high_path), 0, high_mtime or 0, 0) if high_path else None
        for clip in clips:
            if effective_policy == "near_live" and high_clip and self._key(clip, ordering) < self._key(high_clip, ordering):
                self._upsert(clip, "skipped", now)
                continue
            previous = self._connection.execute("SELECT size, mtime_ns FROM clips WHERE camera_id = ? AND source_path = ?", (self.camera_id, str(clip.path))).fetchone()
            if previous is None or previous != (clip.size, clip.mtime_ns):
                self._upsert(clip, "pending", now)
                discovered += 1
            if effective_policy == "near_live" and (high_clip is None or self._key(clip, ordering) > self._key(high_clip, ordering)):
                high_clip = clip
        if effective_policy == "near_live" and high_clip:
            self._connection.execute(
                "UPDATE camera_state SET highwater_path = ?, highwater_mtime_ns = ?, updated_at = ? WHERE camera_id = ?",
                (str(high_clip.path), high_clip.mtime_ns, now, self.camera_id),
            )
        elif effective_policy == "backfill" and not initialized:
            self._connection.execute("UPDATE camera_state SET initialized = 1, updated_at = ? WHERE camera_id = ?", (now, self.camera_id))
        self._connection.commit()
        return discovered

    def _upsert(self, clip: Clip, status: str, now: int) -> None:
        self._connection.execute(
            "INSERT INTO clips(camera_id, source_path, size, mtime_ns, status, attempts, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(camera_id, source_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, status=excluded.status, attempts=0, last_error=NULL, updated_at=excluded.updated_at",
            (self.camera_id, str(clip.path), clip.size, clip.mtime_ns, status, now),
        )

    def reset_near_live(self) -> None:
        now = int(time.time())
        self._connection.execute("DELETE FROM clips WHERE camera_id = ? AND status IN ('pending', 'playing')", (self.camera_id,))
        self._connection.execute(
            "INSERT INTO camera_state(camera_id, policy, initialized, highwater_path, highwater_mtime_ns, updated_at) VALUES (?, 'near_live', 0, NULL, NULL, ?) "
            "ON CONFLICT(camera_id) DO UPDATE SET policy='near_live', initialized=0, highwater_path=NULL, highwater_mtime_ns=NULL, updated_at=excluded.updated_at",
            (self.camera_id, now),
        )
        self._connection.commit()

    def summary(self) -> dict[str, Any]:
        state = self._connection.execute("SELECT policy, initialized, highwater_path, highwater_mtime_ns FROM camera_state WHERE camera_id = ?", (self.camera_id,)).fetchone()
        queued = self._connection.execute("SELECT COUNT(*) FROM clips WHERE camera_id = ? AND status = 'pending'", (self.camera_id,)).fetchone()[0]
        newest = self._connection.execute("SELECT source_path, mtime_ns FROM clips WHERE camera_id = ? ORDER BY mtime_ns DESC, source_path DESC LIMIT 1", (self.camera_id,)).fetchone()
        playing = self._connection.execute("SELECT source_path FROM clips WHERE camera_id = ? AND status = 'playing' LIMIT 1", (self.camera_id,)).fetchone()
        return {"policy": state[0] if state else None, "initialized": bool(state[1]) if state else False, "highwater_path": state[2] if state else None, "highwater_mtime_ns": state[3] if state else None, "queued": queued, "newest_path": newest[0] if newest else None, "newest_mtime_ns": newest[1] if newest else None, "playing_path": playing[0] if playing else None}

    def next_pending(self, ordering: str) -> Clip | None:
        order = "source_path COLLATE NOCASE" if ordering == "path" else "mtime_ns, source_path COLLATE NOCASE"
        row = self._connection.execute(
            f"SELECT source_path, size, mtime_ns, attempts FROM clips WHERE camera_id = ? AND status = 'pending' ORDER BY {order} LIMIT 1",
            (self.camera_id,),
        ).fetchone()
        return Clip(Path(row[0]), row[1], row[2], row[3]) if row else None

    def mark_playing(self, clip: Clip) -> int:
        attempt = clip.attempts + 1
        self._connection.execute(
            "UPDATE clips SET status = 'playing', attempts = ?, updated_at = ? WHERE camera_id = ? AND source_path = ?",
            (attempt, int(time.time()), self.camera_id, str(clip.path)),
        )
        self._connection.commit()
        return attempt

    def mark_completed(self, clip: Clip) -> None:
        self._connection.execute(
            "UPDATE clips SET status = 'completed', last_error = NULL, updated_at = ? WHERE camera_id = ? AND source_path = ?",
            (int(time.time()), self.camera_id, str(clip.path)),
        )
        self._connection.commit()

    def mark_failed_or_retry(self, clip: Clip, attempt: int, error: str, retry_limit: int) -> bool:
        final_failure = attempt >= retry_limit
        self._connection.execute(
            "UPDATE clips SET status = ?, last_error = ?, updated_at = ? WHERE camera_id = ? AND source_path = ?",
            ("failed" if final_failure else "pending", error[-2000:], int(time.time()), self.camera_id, str(clip.path)),
        )
        self._connection.commit()
        return final_failure

    def status_of(self, source_path: Path) -> str | None:
        row = self._connection.execute(
            "SELECT status FROM clips WHERE camera_id = ? AND source_path = ?", (self.camera_id, str(source_path))
        ).fetchone()
        return row[0] if row else None


class CameraWorker:
    """One isolated scanner and FFmpeg publisher for one Xiaomi camera."""

    def __init__(
        self,
        config: CameraConfig,
        publisher: PublisherConfig,
        state_db: Path,
        log_file: Path,
        stop_event: threading.Event,
        publisher_factory: PublisherFactory | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.state_db = state_db
        self.log_file = log_file
        self.stop_event = stop_event
        self._publisher_factory = publisher_factory or self._spawn_ffmpeg
        self._state: ClipState | None = None
        self._publisher: ManagedProcess | None = None
        self._snapshot = WorkerSnapshot("starting")
        self._snapshot_lock = threading.Lock()
        self._known_unavailable_roots: set[Path] = set()

    def snapshot(self) -> WorkerSnapshot:
        with self._snapshot_lock:
            return self._snapshot

    def _set_status(self, state: str, detail: str = "") -> None:
        with self._snapshot_lock:
            self._snapshot = WorkerSnapshot(state, detail)

    def run(self) -> None:
        self._log(logging.INFO, "Worker started. Publishing to %s", self.config.display_url(self.publisher))
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.run_once()
                except Exception as exc:
                    # Keep this worker isolated and retry on the next polling
                    # interval. A transient SMB or FFmpeg problem must not
                    # permanently stop this camera or affect another camera.
                    self._set_status("error", str(exc))
                    self._log(logging.exception, "Worker iteration failed: %s", exc)
                    self.stop_event.wait(self.config.settings.poll_interval_seconds)
                    continue
                if not worked:
                    self.stop_event.wait(self.config.settings.poll_interval_seconds)
        finally:
            self._stop_process(self._publisher, "FFmpeg publisher")
            self._publisher = None
            if self._state:
                self._state.close()
                self._state = None
            if self.snapshot().state != "error":
                self._set_status("stopped")
            self._log(logging.INFO, "Worker stopped.")

    def run_once(self) -> bool:
        """Scan once and process at most one clip; public for deterministic tests."""
        if self._state is None:
            self._state = ClipState(self.state_db, self.config.camera_id)
        discovered, usable_root_count = self._scan_nas()
        if discovered:
            self._log(logging.INFO, "Queued %d completed clip(s).", discovered)
        if not usable_root_count:
            self._set_status("NAS unavailable")
            return False

        clip = self._state.next_pending(self.config.settings.ordering)
        if clip is None:
            self._set_status("waiting")
            return False
        self._publish_clip(clip)
        return True

    def close_for_test(self) -> None:
        self._stop_process(self._publisher, "FFmpeg publisher")
        if self._state:
            self._state.close()
            self._state = None

    def _scan_nas(self) -> tuple[int, int]:
        cutoff_ns = time.time_ns() - self.config.settings.file_settle_seconds * 1_000_000_000
        clips: list[Clip] = []
        usable_roots = 0
        for root in self.config.watch_roots:
            if not root.is_dir():
                if root not in self._known_unavailable_roots:
                    self._log(logging.WARNING, "NAS folder is unavailable: %s", root)
                    self._known_unavailable_roots.add(root)
                continue
            if root in self._known_unavailable_roots:
                self._known_unavailable_roots.remove(root)
                self._log(logging.INFO, "NAS folder is available again: %s", root)
            usable_roots += 1
            try:
                for path in root.rglob("*"):
                    try:
                        if path.suffix.lower() not in self.config.settings.extensions or not path.is_file():
                            continue
                        stat = path.stat()
                    except OSError as exc:
                        self._log(logging.WARNING, "Skipping unreadable source file %s: %s", path, exc)
                        continue
                    if stat.st_size > 0 and stat.st_mtime_ns <= cutoff_ns:
                        clips.append(Clip(path.resolve(strict=False), stat.st_size, stat.st_mtime_ns, 0))
            except OSError as exc:
                self._log(logging.WARNING, "Could not scan NAS folder %s: %s", root, exc)
        assert self._state is not None
        return self._state.discover(clips, self.config.settings.ordering, self.config.start_policy), usable_roots

    def _publish_clip(self, clip: Clip) -> None:
        assert self._state is not None
        try:
            current = clip.path.stat()
        except OSError as exc:
            attempt = self._state.mark_playing(clip)
            self._state.mark_failed_or_retry(clip, attempt, f"Source disappeared: {exc}", self.config.settings.retry_limit)
            self._set_status("retrying", clip.path.name)
            self._log(logging.WARNING, "Source disappeared before playback: %s", clip.path)
            return
        if current.st_size != clip.size or current.st_mtime_ns != clip.mtime_ns:
            self._log(logging.INFO, "Deferring source file that changed after queueing: %s", clip.path)
            self._state.discover([Clip(clip.path, current.st_size, current.st_mtime_ns, 0)], self.config.settings.ordering, self.config.start_policy)
            self._set_status("waiting")
            return

        attempt = self._state.mark_playing(clip)
        self._set_status("streaming", clip.path.name)
        self._log(logging.INFO, "Streaming %s (attempt %d).", clip.path, attempt)
        try:
            self._publisher = self._publisher_factory(self._ffmpeg_command(clip.path), self._ffmpeg_log_path())
            while self._publisher.poll() is None and not self.stop_event.wait(0.25):
                pass
            if self.stop_event.is_set() and self._publisher.poll() is None:
                self._stop_process(self._publisher, "FFmpeg publisher")
                self._set_status("stopped")
                return
            exit_code = self._publisher.wait()
        except Exception as exc:
            exit_code = -1
            detail = f"Unable to start FFmpeg: {exc}"
        else:
            detail = f"FFmpeg exited with code {exit_code}; see {self._ffmpeg_log_path().name}."
        finally:
            self._publisher = None

        if exit_code == 0:
            self._state.mark_completed(clip)
            self._set_status("waiting")
            self._log(logging.INFO, "Completed %s.", clip.path.name)
            return

        final_failure = self._state.mark_failed_or_retry(clip, attempt, detail, self.config.settings.retry_limit)
        if final_failure:
            self._set_status("failed file skipped", clip.path.name)
            self._log(logging.ERROR, "Skipping %s after %d failed attempt(s): %s", clip.path, attempt, detail)
        else:
            self._set_status("retrying", clip.path.name)
            self._log(logging.WARNING, "%s (%s)", detail, clip.path)

    def _ffmpeg_command(self, clip_path: Path) -> list[str]:
        command = [
            str(self.publisher.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-re",  # preserve real duration while a client consumes the virtual stream
            "-i",
            str(clip_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
        ]
        if self.config.settings.transcode:
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    self.config.settings.video_preset,
                    "-tune",
                    "zerolatency",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "30",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                ]
            )
        else:
            command.extend(["-c", "copy"])
        command.extend(["-f", "rtsp", "-rtsp_transport", "tcp", self.config.publisher_url(self.publisher)])
        return command

    def _ffmpeg_log_path(self) -> Path:
        return self.log_file.with_name(f"{self.config.camera_id}.ffmpeg.log")

    def _spawn_ffmpeg(self, command: list[str], log_path: Path) -> ManagedProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            return subprocess.Popen(
                command,
                cwd=str(self.publisher.ffmpeg_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )

    def _log(self, level: int | Callable[..., Any], message: str, *args: Any) -> None:
        prefix = f"[{self.config.camera_id}] "
        if callable(level):
            level(prefix + message, *args)
        else:
            LOG.log(level, prefix + message, *args)

    @staticmethod
    def _stop_process(process: ManagedProcess | None, description: str) -> None:
        if process is None or process.poll() is not None:
            return
        LOG.info("Stopping %s.", description)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class WorkerSupervisor:
    """Reconciles enabled camera definitions without disturbing unchanged workers."""

    def __init__(self, config: BridgeConfig, publisher_factory: PublisherFactory | None = None) -> None:
        self.config = config
        self.publisher_factory = publisher_factory
        # Create or migrate shared tables before independent worker threads
        # open SQLite connections, avoiding a first-start schema race.
        bootstrap = ClipState(config.state_db, "__bootstrap__")
        bootstrap.close()
        self._workers: dict[str, tuple[CameraConfig, CameraWorker, threading.Event, threading.Thread]] = {}
        self._lock = threading.RLock()
        self._stopping = False

    def reconcile(self, desired_cameras: Iterable[CameraConfig]) -> None:
        desired = {camera.camera_id: camera for camera in desired_cameras}
        with self._lock:
            if self._stopping:
                return
            remove_ids = [camera_id for camera_id, (camera, _, _, _) in self._workers.items() if desired.get(camera_id) != camera]
            stopped = [self._workers.pop(camera_id) for camera_id in remove_ids]
            for _, _, event, _ in stopped:
                event.set()
            for _, _, _, thread in stopped:
                thread.join(timeout=2)

            for camera_id, camera in desired.items():
                if camera_id in self._workers:
                    continue
                event = threading.Event()
                worker = CameraWorker(
                    camera,
                    self.config.publisher,
                    self.config.state_db,
                    self.config.log_file,
                    event,
                    publisher_factory=self.publisher_factory,
                )
                thread = threading.Thread(target=worker.run, name=f"xiaomi-{camera.camera_id}", daemon=True)
                thread.start()
                self._workers[camera_id] = (camera, worker, event, thread)

    def snapshots(self) -> dict[str, WorkerSnapshot]:
        with self._lock:
            return {camera_id: worker.snapshot() for camera_id, (_, worker, _, _) in self._workers.items()}

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            workers = list(self._workers.values())
            self._workers.clear()
        for _, _, event, _ in workers:
            event.set()
        for _, _, _, thread in workers:
            thread.join(timeout=5)


class BridgeController:
    """Waits for MediaMTX and supervises a thread per independent camera worker."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.workers = [
            CameraWorker(camera, config.publisher, config.state_db, config.log_file, self.stop_event) for camera in config.cameras
        ]
        self._threads: list[threading.Thread] = []

    def run(self) -> None:
        self._validate_dependencies()
        # Create or migrate the schema before worker threads open their own
        # SQLite connections. This avoids a first-start schema race.
        bootstrap = ClipState(self.config.state_db, "__bootstrap__")
        bootstrap.close()
        self._wait_for_rtsp_server()
        try:
            for worker in self.workers:
                thread = threading.Thread(target=worker.run, name=f"xiaomi-{worker.config.camera_id}", daemon=True)
                thread.start()
                self._threads.append(thread)
            self._log_status()
            while not self.stop_event.wait(self.config.status_interval_seconds):
                self._log_status()
        finally:
            self.stop()
            for thread in self._threads:
                thread.join(timeout=10)
            self._write_health()

    def stop(self) -> None:
        self.stop_event.set()

    def _validate_dependencies(self) -> None:
        if not self.config.publisher.ffmpeg_path.is_file():
            raise ConfigurationError(f"FFmpeg does not exist: {self.config.publisher.ffmpeg_path}")
        for camera in self.config.cameras:
            if not camera.watch_roots:
                raise ConfigurationError(f"Camera {camera.camera_id} has no NAS folders.")

    def _wait_for_rtsp_server(self) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.config.publisher.host, self.config.publisher.rtsp_port), timeout=0.5):
                    LOG.info("MediaMTX is reachable at %s:%d.", self.config.publisher.host, self.config.publisher.rtsp_port)
                    return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError(
            f"No RTSP server answered at {self.config.publisher.host}:{self.config.publisher.rtsp_port}. Check MediaMTX."
        )

    def _log_status(self) -> None:
        entries = []
        for worker in self.workers:
            snapshot = worker.snapshot()
            entries.append(f"{worker.config.camera_id}={snapshot.state}" + (f" ({snapshot.detail})" if snapshot.detail else ""))
        status = "; ".join(entries)
        LOG.info("Status: %s", status)
        self._write_health()

    def _write_health(self) -> None:
        self.config.health_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "stopping": self.stop_event.is_set(),
            "workers": {
                worker.config.camera_id: {"state": snapshot.state, "detail": snapshot.detail}
                for worker in self.workers
                for snapshot in [worker.snapshot()]
            },
        }
        temporary_path = self.config.health_file.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary_path.replace(self.config.health_file)


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def healthcheck(config: BridgeConfig) -> bool:
    """Return true only while the controller is updating its persisted status."""
    try:
        payload = json.loads(config.health_file.read_text(encoding="utf-8"))
        updated_at = float(payload["updated_at"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return not payload.get("stopping", False) and time.time() - updated_at <= config.status_interval_seconds * 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Xiaomi NAS recordings as independent virtual RTSP cameras.")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"), help="Path to config.json")
    parser.add_argument("--healthcheck", action="store_true", help="Exit successfully when the running bridge has fresh health state.")
    args = parser.parse_args(argv)

    try:
        config = BridgeConfig.from_json(args.config.resolve(strict=False))
        if args.healthcheck:
            return 0 if healthcheck(config) else 1
        configure_logging(config.log_file)
        controller = BridgeController(config)
    except (ConfigurationError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    def handle_stop(_: int, __: Any) -> None:
        LOG.info("Stop requested.")
        controller.stop()

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)
    try:
        controller.run()
        return 0
    except (ConfigurationError, RuntimeError, OSError) as exc:
        LOG.error("Bridge stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
