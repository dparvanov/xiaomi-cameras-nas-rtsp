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

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SETTINGS_VERSION = 3
MIN_ADMIN_PASSWORD_LENGTH = 12
MIN_RTSP_PASSWORD_LENGTH = 12
LEGACY_PLACEHOLDER_USERNAME = "REPLACE_WITH_A_NONDEFAULT_SETUP_USERNAME"


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


def _credential_cipher(secret: str) -> Fernet:
    if len(secret) < 32:
        raise SettingsError("The credential-encryption secret must contain at least 32 characters.")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"xiaomi-cameras-nas-rtsp/reader-password/v1",
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


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
        raise SettingsError("Username must contain only letters, numbers, periods, hyphens, or underscores.")
    return value


def _is_placeholder(value: str) -> bool:
    return value.upper().startswith("REPLACE_WITH_") or value.lower() in {"change-me", "replace-me"}


def load_or_create_session_secret(path: Path, configured: str = "") -> str:
    """Return a stable session secret without requiring first-install environment edits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    configured = configured.strip()
    if configured and not _is_placeholder(configured):
        if len(configured) < 32:
            raise SettingsError("SETUP_SESSION_SECRET must contain at least 32 characters when supplied.")
        return configured

    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        generated = secrets.token_urlsafe(48)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = path.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(generated + "\n")
            return generated
    except OSError as exc:
        raise SettingsError(f"Cannot read persistent session secret: {exc}") from exc

    if len(existing) < 32:
        raise SettingsError("The persistent session secret is invalid; it must contain at least 32 characters.")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return existing


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
    """JSON state with atomic writes and encrypted recoverable RTSP credentials."""

    def __init__(
        self,
        path: Path,
        recordings_root: Path,
        credential_secret: str,
        admin_username: str = "",
        admin_password: str = "",
    ) -> None:
        self.path = path
        self.recordings_root = recordings_root.resolve(strict=False)
        self._cipher = _credential_cipher(credential_secret)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self.path.exists():
                self._settings = self._load()
                if self._settings["version"] in {1, 2}:
                    previous_version = self._settings["version"]
                    self._settings["version"] = SETTINGS_VERSION
                    admin = self._settings.get("admin")
                    if previous_version == 1 and isinstance(admin, dict) and admin.get("username") == LEGACY_PLACEHOLDER_USERNAME:
                        self._settings["admin"] = None
                    reader = self._settings.get("reader")
                    if isinstance(reader, dict) and not reader.get("password_hash"):
                        reader["username"] = "viewer"
                    if isinstance(reader, dict):
                        reader["password_encrypted"] = ""
                    self._save()
            else:
                admin = self._bootstrap_admin(admin_username, admin_password)
                self._settings = {
                    "version": SETTINGS_VERSION,
                    "admin": admin,
                    "recordings_subdirectory": ".",
                    "reader": {"username": "viewer", "password_hash": "", "password_encrypted": ""},
                    "cameras": {},
                }
                self._save()

    @staticmethod
    def _validate_admin(username: str, password: str) -> tuple[str, str]:
        username = validate_username(username)
        if len(password) < MIN_ADMIN_PASSWORD_LENGTH or _is_placeholder(password):
            raise SettingsError(f"Admin password must contain at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")
        return username, password

    @classmethod
    def _bootstrap_admin(cls, username: str, password: str) -> dict[str, str] | None:
        username = username.strip()
        if not username and not password:
            return None
        if _is_placeholder(username) or _is_placeholder(password):
            return None
        if not username or not password:
            raise SettingsError("Supply both SETUP_ADMIN_USERNAME and SETUP_ADMIN_PASSWORD, or leave both unset for first-run setup.")
        username, password = cls._validate_admin(username, password)
        return {"username": username, "password_hash": _hash_admin_password(password)}

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"Cannot read persistent settings: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") not in {1, 2, SETTINGS_VERSION}:
            raise SettingsError("Persistent settings have an unsupported format.")
        for key in ("admin", "reader", "cameras", "recordings_subdirectory"):
            if key not in value:
                raise SettingsError("Persistent settings are incomplete.")
        if value["admin"] is not None and not isinstance(value["admin"], dict):
            raise SettingsError("Persistent admin settings are invalid.")
        if not isinstance(value["reader"], dict) or not isinstance(value["cameras"], dict):
            raise SettingsError("Persistent stream settings are invalid.")
        return value

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._settings, indent=2, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "recordings_subdirectory": self._settings["recordings_subdirectory"],
                "reader": {
                    "username": self._settings["reader"]["username"],
                    "password_set": bool(self._settings["reader"]["password_hash"]),
                    "password_available": bool(self._settings["reader"].get("password_encrypted")),
                },
                "cameras": {key: {"enabled": bool(value.get("enabled")), "camera_id": value.get("camera_id", ""), "name": value.get("name", ""), "start_policy": value.get("start_policy", "near_live")} for key, value in self._settings["cameras"].items()},
            }

    def internal(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._settings))

    def is_admin_configured(self) -> bool:
        with self._lock:
            return isinstance(self._settings["admin"], dict)

    def admin_username(self) -> str | None:
        with self._lock:
            admin = self._settings["admin"]
            return admin["username"] if isinstance(admin, dict) else None

    def create_admin(self, username: str, password: str) -> str:
        with self._lock:
            if self._settings["admin"] is not None:
                raise SettingsError("The administrator account has already been created.")
            username, password = self._validate_admin(username, password)
            self._settings["admin"] = {"username": username, "password_hash": _hash_admin_password(password)}
            self._save()
            return username

    def verify_admin(self, username: str, password: str) -> bool:
        with self._lock:
            admin = self._settings["admin"]
            if not isinstance(admin, dict):
                return False
            return hmac.compare_digest(username, admin["username"]) and verify_admin_password(password, admin["password_hash"])

    def reader_credentials(self) -> tuple[str, str]:
        with self._lock:
            reader = self._settings["reader"]
            encrypted = str(reader.get("password_encrypted", ""))
            if not encrypted:
                return reader["username"], ""
            try:
                password = self._cipher.decrypt(encrypted.encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError, ValueError):
                return reader["username"], ""
            return reader["username"], password

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
                if len(reader_password) < MIN_RTSP_PASSWORD_LENGTH:
                    raise SettingsError(f"RTSP reader password must be at least {MIN_RTSP_PASSWORD_LENGTH} characters.")
                updated["reader"]["password_hash"] = mediamtx_sha256(reader_password)
                updated["reader"]["password_encrypted"] = self._cipher.encrypt(reader_password.encode("utf-8")).decode("ascii")

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
                raise SettingsError("Set an RTSP client password before enabling a camera.")
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
