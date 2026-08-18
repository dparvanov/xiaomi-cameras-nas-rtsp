"""Persistent, secret-safe settings for the ZimaOS setup interface."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import unicodedata
from pathlib import Path
from typing import Any


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SettingsError(ValueError):
    pass


def _hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"))


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


def mediamtx_sha256(password: str) -> str:
    """Return MediaMTX's documented `sha256:<base64>` internal-auth format."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return "sha256:" + base64.b64encode(digest).decode("ascii")


def sanitize_camera_id(folder_name: str) -> str:
    normalized = unicodedata.normalize("NFKD", folder_name).encode("ascii", "ignore").decode("ascii").lower()
    candidate = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not candidate:
        candidate = "camera"
    if not candidate[0].isalpha():
        candidate = "camera-" + candidate
    return candidate[:64].rstrip("-") or "camera"


def unique_camera_id(folder_name: str, used: set[str]) -> str:
    base = sanitize_camera_id(folder_name)
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"-{suffix}"
        candidate = base[: 64 - len(tail)].rstrip("-") + tail
        suffix += 1
    return candidate


def validate_camera_id(camera_id: str) -> str:
    value = camera_id.strip().lower()
    if not CAMERA_ID_RE.fullmatch(value):
        raise SettingsError("Camera ID must use lowercase letters, numbers, hyphens, or underscores, and start with a letter or number.")
    return value


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_RE.fullmatch(value):
        raise SettingsError("RTSP username must contain only letters, numbers, periods, hyphens, or underscores.")
    return value


def validate_relative_directory(root: Path, relative: str) -> tuple[str, Path]:
    value = relative.strip() or "."
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SettingsError("The recordings directory must be the mounted root or a subdirectory under it.")
    root_resolved = root.resolve(strict=False)
    resolved = (root_resolved / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SettingsError("The recordings directory escapes the mounted recordings root.") from exc
    normalized = "." if resolved == root_resolved else resolved.relative_to(root_resolved).as_posix()
    return normalized, resolved


class SettingsStore:
    """JSON state with atomic writes. Plaintext passwords never enter this file."""

    def __init__(self, path: Path, recordings_root: Path, admin_username: str, admin_password: str) -> None:
        self.path = path
        self.recordings_root = recordings_root.resolve(strict=False)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self.path.exists():
                self._settings = self._load()
            else:
                self._validate_initial_admin(admin_username, admin_password)
                self._settings = {
                    "version": 1,
                    "admin": {"username": admin_username, "password_hash": _hash_admin_password(admin_password)},
                    "recordings_subdirectory": ".",
                    "reader": {"username": "blueiris", "password_hash": ""},
                    "cameras": {},
                }
                self._save()

    @staticmethod
    def _validate_initial_admin(username: str, password: str) -> None:
        validate_username(username)
        if username.lower() in {"admin", "administrator", "change-me"}:
            raise SettingsError("SETUP_ADMIN_USERNAME must not use a default administrative name.")
        if len(password) < 16 or password.lower() in {"change-me", "password", "admin"}:
            raise SettingsError("SETUP_ADMIN_PASSWORD must be a unique password of at least 16 characters.")

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"Cannot read persistent settings: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise SettingsError("Persistent settings have an unsupported format.")
        for key in ("admin", "reader", "cameras", "recordings_subdirectory"):
            if key not in value:
                raise SettingsError("Persistent settings are incomplete.")
        return value

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._settings, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "recordings_subdirectory": self._settings["recordings_subdirectory"],
                "reader": {"username": self._settings["reader"]["username"], "password_set": bool(self._settings["reader"]["password_hash"])},
                "cameras": {key: {"enabled": bool(value.get("enabled")), "camera_id": value.get("camera_id", ""), "name": value.get("name", ""), "start_policy": value.get("start_policy", "near_live")} for key, value in self._settings["cameras"].items()},
            }

    def internal(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def verify_admin(self, username: str, password: str) -> bool:
        with self._lock:
            admin = self._settings["admin"]
            return hmac.compare_digest(username, admin["username"]) and verify_admin_password(password, admin["password_hash"])

    def selected_directory(self) -> tuple[str, Path]:
        with self._lock:
            return validate_relative_directory(self.recordings_root, self._settings["recordings_subdirectory"])

    def scan_candidates(self) -> tuple[list[dict[str, str]], str | None]:
        try:
            subdirectory, selected = self.selected_directory()
            if not selected.is_dir():
                return [], f"Selected recordings directory is unavailable: {selected}"
            candidates = []
            for item in sorted(selected.iterdir(), key=lambda path: path.name.casefold()):
                # Camera candidates are actual immediate directories, never
                # symlinks that could lead the worker outside /recordings.
                if item.is_dir() and not item.is_symlink():
                    relative = item.resolve(strict=False).relative_to(self.recordings_root).as_posix()
                    candidates.append({"key": relative, "folder": item.name, "selected_root": subdirectory})
            return candidates, None
        except (OSError, SettingsError) as exc:
            return [], str(exc)

    def preview_update(
        self,
        recordings_subdirectory: str,
        reader_username: str,
        reader_password: str,
        camera_updates: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            normalized_root, _ = validate_relative_directory(self.recordings_root, recordings_subdirectory)
            reader_username = validate_username(reader_username)
            updated = self.internal()
            updated["recordings_subdirectory"] = normalized_root
            updated["reader"]["username"] = reader_username
            if reader_password:
                if len(reader_password) < 16:
                    raise SettingsError("RTSP reader password must be at least 16 characters.")
                updated["reader"]["password_hash"] = mediamtx_sha256(reader_password)

            seen_ids: set[str] = set()
            for key, value in camera_updates.items():
                normalized_key, _ = validate_relative_directory(self.recordings_root, key)
                camera_id = validate_camera_id(str(value["camera_id"]))
                if camera_id in seen_ids:
                    raise SettingsError("Each configured camera must have a unique camera ID.")
                seen_ids.add(camera_id)
                name = str(value.get("name", "")).strip() or camera_id
                policy = str(value.get("start_policy", updated["cameras"].get(normalized_key, {}).get("start_policy", "near_live")))
                if policy not in {"near_live", "backfill"}:
                    raise SettingsError("Start policy must be near_live or backfill.")
                updated["cameras"][normalized_key] = {"enabled": bool(value.get("enabled")), "camera_id": camera_id, "name": name, "start_policy": policy}

            all_ids = [entry.get("camera_id", "") for entry in updated["cameras"].values()]
            if len(all_ids) != len(set(all_ids)):
                raise SettingsError("Each saved camera, including temporarily unavailable folders, must have a unique camera ID.")
            if any(entry.get("enabled") for entry in updated["cameras"].values()) and not updated["reader"]["password_hash"]:
                raise SettingsError("Set an RTSP reader password before enabling a camera for Blue Iris.")
            return json.loads(json.dumps(updated))

    def commit(self, settings: dict[str, Any]) -> None:
        with self._lock:
            self._settings = settings
            self._save()

    def update(
        self,
        recordings_subdirectory: str,
        reader_username: str,
        reader_password: str,
        camera_updates: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        proposed = self.preview_update(recordings_subdirectory, reader_username, reader_password, camera_updates)
        self.commit(proposed)
        return proposed
