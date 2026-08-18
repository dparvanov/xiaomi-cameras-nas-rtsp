"""Authenticated ZimaOS setup UI and live worker reconciler."""

from __future__ import annotations

import atexit
import base64
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from functools import wraps
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

from settings_store import SettingsError, SettingsStore, load_or_create_session_secret, unique_camera_id, validate_relative_directory
from xiaomi_rtsp_bridge import BridgeConfig, CameraConfig, ClipState, WorkerSupervisor


LOG = logging.getLogger("xiaomi_setup_ui")


class MediaMTXClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.url = f"http://{config.publisher.host}:{config.publisher.api_port}/v3/config/global/patch"

    def apply_reader(self, settings: dict[str, Any]) -> None:
        users = [
            {
                "user": self.config.publisher.username,
                "pass": self.config.publisher.password,
                "permissions": [
                    {"action": "publish", "path": "~^xiaomi/.+$"},
                    {"action": "api"},
                ],
            }
        ]
        reader = settings["reader"]
        if reader["password_hash"]:
            users.append(
                {
                    "user": reader["username"],
                    "pass": reader["password_hash"],
                    "permissions": [{"action": "read", "path": "~^xiaomi/.+$"}],
                }
            )
        body = json.dumps({"authInternalUsers": users}).encode("utf-8")
        credentials = base64.b64encode(f"{self.config.publisher.username}:{self.config.publisher.password}".encode("utf-8")).decode("ascii")
        request_object = urllib.request.Request(
            self.url,
            data=body,
            method="PATCH",
            headers={"Content-Type": "application/json", "Authorization": f"Basic {credentials}"},
        )
        try:
            with urllib.request.urlopen(request_object, timeout=5) as response:
                if response.status not in {200, 204}:
                    raise RuntimeError(f"MediaMTX returned HTTP {response.status}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            # Exception string does not contain the Authorization header or body.
            raise RuntimeError(f"Could not apply RTSP authentication to MediaMTX: {exc}") from exc


class BridgeRuntime:
    def __init__(self, config: BridgeConfig, store: SettingsStore) -> None:
        self.config = config
        self.store = store
        self.supervisor = WorkerSupervisor(config)
        self.mediamtx = MediaMTXClient(config)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._last_error = ""
        self._thread = threading.Thread(target=self._heartbeat, name="bridge-heartbeat", daemon=True)

    def start(self) -> None:
        try:
            self.apply(self.store.internal(), persist=False)
        except RuntimeError as exc:
            self._last_error = str(exc)
            LOG.warning("Initial MediaMTX configuration is pending: %s", exc)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.supervisor.stop()

    def apply(self, settings: dict[str, Any], persist: bool = True) -> None:
        with self._lock:
            if settings["reader"]["username"] == self.config.publisher.username:
                raise SettingsError("The RTSP client username must differ from the bridge publisher username.")
            cameras = self._camera_configs(settings)
            # Check every saved camera, including one currently disabled, so a
            # rescan or toggle cannot silently rewrite an established policy.
            for saved in settings["cameras"].values():
                state = ClipState(self.config.state_db, saved["camera_id"])
                try:
                    summary = state.summary()
                finally:
                    state.close()
                if summary["initialized"] and summary["policy"] and summary["policy"] != saved.get("start_policy", "near_live"):
                    raise SettingsError("This camera has already been initialized. Use ‘Start from newest now’ to intentionally reset it to near-live.")
            self.mediamtx.apply_reader(settings)
            if persist:
                self.store.commit(settings)
            self.supervisor.reconcile(cameras)
            self._last_error = ""
            self._write_health()

    def status(self) -> dict[str, Any]:
        snapshots = self.supervisor.snapshots()
        configured = self.store.public()["cameras"]
        cameras = {}
        for key, camera in configured.items():
            snapshot = snapshots.get(camera["camera_id"])
            state = ClipState(self.config.state_db, camera["camera_id"])
            try:
                queue = state.summary()
            finally:
                state.close()
            newest_mtime = queue.get("newest_mtime_ns")
            lag = f"{int((time.time_ns() - newest_mtime) / 1_000_000_000)}s (filesystem mtime)" if newest_mtime else "—"
            cameras[key] = {
                "enabled": camera["enabled"],
                "state": snapshot.state if snapshot else ("disabled" if not camera["enabled"] else "pending start"),
                "detail": snapshot.detail if snapshot else "",
                "rtsp_path": f"xiaomi/{camera['camera_id']}",
                "queued": queue["queued"],
                "playing_file": Path(queue["playing_path"]).name if queue.get("playing_path") else None,
                "newest_file": Path(queue["newest_path"]).name if queue.get("newest_path") else None,
                "highwater_file": Path(queue["highwater_path"]).name if queue.get("highwater_path") else None,
                "source_lag": lag,
                "effective_policy": queue.get("policy"),
            }
        return {"media_error": self._last_error, "cameras": cameras}

    def _camera_configs(self, settings: dict[str, Any]) -> list[CameraConfig]:
        cameras = []
        for relative_folder, value in settings["cameras"].items():
            if not value.get("enabled"):
                continue
            _, source_path = validate_relative_directory(self.config.recordings_root, relative_folder)
            camera_id = value["camera_id"]
            cameras.append(
                CameraConfig(
                    camera_id=camera_id,
                    name=value.get("name") or camera_id,
                    watch_roots=(source_path,),
                    rtsp_path=f"xiaomi/{camera_id}",
                    settings=self.config.defaults,
                    start_policy=value.get("start_policy", "near_live"),
                )
            )
        return cameras

    def reset_near_live(self, camera_key: str) -> None:
        settings = self.store.internal()
        camera = settings["cameras"].get(camera_key)
        if not camera:
            raise SettingsError("Unknown saved camera folder.")
        camera["start_policy"] = "near_live"
        state = ClipState(self.config.state_db, camera["camera_id"])
        try:
            state.reset_near_live()
        finally:
            state.close()
        self.store.commit(settings)
        self.supervisor.reconcile(self._camera_configs(settings))
        self._write_health()

    def _heartbeat(self) -> None:
        while not self._stop_event.wait(15):
            try:
                # MediaMTX keeps its API patch in memory. Reapply it after a
                # MediaMTX restart; reconciliation is intentionally separate
                # so unchanged camera workers keep running.
                self.mediamtx.apply_reader(self.store.internal())
                self._last_error = ""
                self._write_health()
            except (OSError, RuntimeError) as exc:
                self._last_error = str(exc)
                LOG.warning("Could not refresh bridge health/authentication: %s", exc)

    def _write_health(self) -> None:
        self.config.health_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": time.time(), "stopping": self._stop_event.is_set(), **self.status()}
        temporary = self.config.health_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.config.health_file)


def create_app(config_path: str | Path | None = None, runtime: BridgeRuntime | None = None) -> Flask:
    path = Path(config_path or os.environ.get("BRIDGE_CONFIG", "/config/config.json"))
    config = BridgeConfig.from_json(path)
    admin_username = os.environ.get("SETUP_ADMIN_USERNAME", "")
    admin_password = os.environ.get("SETUP_ADMIN_PASSWORD", "")
    data_directory = config.state_db.parent
    session_secret = load_or_create_session_secret(data_directory / "session.secret", os.environ.get("SETUP_SESSION_SECRET", ""))
    store = SettingsStore(data_directory / "settings.json", config.recordings_root, admin_username, admin_password)
    if runtime is None:
        runtime = BridgeRuntime(config, store)
        runtime.start()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=session_secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SETUP_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=3600,
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    app.extensions["settings_store"] = store
    app.extensions["bridge_runtime"] = runtime
    login_attempts: dict[str, list[float]] = {}
    login_attempts_lock = threading.Lock()

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def require_csrf() -> None:
        token = request.form.get("csrf_token", "")
        expected = session.get("csrf_token")
        if not expected or not token or not hmac.compare_digest(token, expected):
            abort(400, "Invalid CSRF token.")
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            abort(403, "Cross-site form submission denied.")

    def signed_in() -> bool:
        username = store.admin_username()
        return bool(username) and hmac.compare_digest(session.get("setup_user", ""), username)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not store.is_admin_configured():
                return redirect(url_for("setup"))
            if not signed_in():
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    @app.after_request
    def secure_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if store.is_admin_configured():
            return redirect(url_for("index" if signed_in() else "login"))
        username = request.form.get("username", "") if request.method == "POST" else ""
        if request.method == "POST":
            require_csrf()
            password = request.form.get("password", "")
            if password != request.form.get("password_confirm", ""):
                flash("Password confirmation does not match.", "error")
            else:
                try:
                    username = store.create_admin(username, password)
                except SettingsError as exc:
                    flash(str(exc), "error")
                else:
                    session.clear()
                    session["setup_user"] = username
                    session.permanent = True
                    csrf_token()
                    flash("Administrator created. Your bridge is ready to configure.", "success")
                    return redirect(url_for("index"))
        return render_template("setup.html", csrf_token=csrf_token(), username=username)

    @app.get("/login")
    def login():
        if not store.is_admin_configured():
            return redirect(url_for("setup"))
        if signed_in():
            return redirect(url_for("index"))
        return render_template("login.html", csrf_token=csrf_token())

    @app.post("/login")
    def login_submit():
        require_csrf()
        source_ip = request.remote_addr or "unknown"
        now = time.monotonic()
        with login_attempts_lock:
            attempts = [timestamp for timestamp in login_attempts.get(source_ip, []) if now - timestamp < 300]
            if len(attempts) >= 5:
                abort(429, "Too many login attempts. Try again later.")
        if not store.verify_admin(request.form.get("username", ""), request.form.get("password", "")):
            with login_attempts_lock:
                attempts.append(now)
                login_attempts[source_ip] = attempts
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))
        with login_attempts_lock:
            login_attempts.pop(source_ip, None)
        session.clear()
        session["setup_user"] = store.admin_username()
        session.permanent = True
        csrf_token()
        return redirect(url_for("index"))

    @app.get("/")
    @login_required
    def index():
        settings = store.public()
        candidates, scan_error = store.scan_candidates()
        configured = settings["cameras"]
        runtime_status = runtime.status()
        used_ids = {value["camera_id"] for value in configured.values() if value["camera_id"]}
        camera_rows = []
        candidate_keys = set()
        for candidate in candidates:
            key = candidate["key"]
            candidate_keys.add(key)
            saved = configured.get(key, {})
            camera_id = saved.get("camera_id") or unique_camera_id(candidate["folder"], used_ids)
            used_ids.add(camera_id)
            status = runtime_status["cameras"].get(key, {"state": "disabled" if not saved.get("enabled") else "pending start", "detail": "", "queued": 0, "playing_file": None, "newest_file": None, "highwater_file": None, "source_lag": "—"})
            camera_rows.append({"key": key, "camera_id": camera_id, "name": saved.get("name") or candidate["folder"], "enabled": bool(saved.get("enabled")), "start_policy": saved.get("start_policy", "near_live"), "missing": False, "status": SimpleNamespace(**status)})
        for key, saved in configured.items():
            if key not in candidate_keys:
                status = runtime_status["cameras"].get(key, {"state": "disabled" if not saved.get("enabled") else "NAS unavailable", "detail": "", "queued": 0, "playing_file": None, "newest_file": None, "highwater_file": None, "source_lag": "—"})
                camera_rows.append({"key": key, "camera_id": saved["camera_id"], "name": saved.get("name") or Path(key).name, "enabled": bool(saved.get("enabled")), "start_policy": saved.get("start_policy", "near_live"), "missing": True, "status": SimpleNamespace(**status)})
        return render_template(
            "dashboard.html",
            settings=settings,
            cameras=camera_rows,
            enabled_count=sum(1 for camera in camera_rows if camera["enabled"]),
            scan_error=scan_error,
            runtime=runtime_status,
            admin_username=store.admin_username(),
            nas_host=request.host.split(":", 1)[0],
            csrf_token=csrf_token(),
        )

    @app.post("/settings")
    @login_required
    def save_settings():
        require_csrf()
        password = request.form.get("reader_password", "")
        if password != request.form.get("reader_password_confirm", ""):
            flash("RTSP password confirmation does not match.", "error")
            return redirect(url_for("index"))
        allowed = set(store.public()["cameras"])
        candidates, _ = store.scan_candidates()
        allowed.update(candidate["key"] for candidate in candidates)
        updates: dict[str, dict[str, Any]] = {}
        for key in request.form.getlist("camera_key"):
            if key not in allowed:
                abort(400, "Unknown camera folder.")
            updates[key] = {
                "enabled": request.form.get(f"enabled:{key}") == "on",
                "camera_id": request.form.get(f"camera_id:{key}", ""),
                "name": request.form.get(f"name:{key}", ""),
                "start_policy": request.form.get(f"start_policy:{key}", "near_live"),
            }
        try:
            proposed = store.preview_update(
                request.form.get("recordings_subdirectory", "."),
                request.form.get("reader_username", ""),
                password,
                updates,
            )
            runtime.apply(proposed)
        except (SettingsError, RuntimeError, ValueError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        flash("Settings applied. Enabled streams are now reconciled.", "success")
        return redirect(url_for("index"))

    @app.post("/reset-near-live")
    @login_required
    def reset_near_live():
        require_csrf()
        if request.form.get("confirm") != "on":
            abort(400, "Confirmation is required.")
        try:
            runtime.reset_near_live(request.form.get("camera_key", ""))
        except (SettingsError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Camera queue reset. The next stable newest source clip becomes the near-live starting point.", "success")
        return redirect(url_for("index"))

    @app.post("/logout")
    @login_required
    def logout():
        require_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/api/status")
    @login_required
    def api_status():
        return jsonify({"settings": store.public(), "runtime": runtime.status()})

    atexit.register(runtime.stop)
    return app
