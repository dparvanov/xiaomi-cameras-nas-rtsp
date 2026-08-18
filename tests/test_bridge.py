import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from settings_store import (
    SettingsError,
    SettingsStore,
    sanitize_camera_id,
    unique_camera_id,
    validate_relative_directory,
)
from webapp import BridgeRuntime, create_app
from xiaomi_rtsp_bridge import BridgeConfig, CameraConfig, Clip, ClipState, WorkerSupervisor


class CompletedProcess:
    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.previous_password = os.environ.get("RTSP_PUBLISH_PASSWORD")
        os.environ["RTSP_PUBLISH_PASSWORD"] = "publisher-test-password"

    def tearDown(self):
        if self.previous_password is None:
            os.environ.pop("RTSP_PUBLISH_PASSWORD", None)
        else:
            os.environ["RTSP_PUBLISH_PASSWORD"] = self.previous_password
        self.temporary_directory.cleanup()

    def config(self):
        payload = {
            "publisher": {
                "host": "mediamtx",
                "rtsp_port": 8554,
                "api_port": 9997,
                "username": "bridge",
                "password_env": "RTSP_PUBLISH_PASSWORD",
                "ffmpeg_path": "/usr/bin/ffmpeg",
            },
            "recordings_root": str(self.base / "recordings"),
            "state_db": str(self.base / "data/state.sqlite3"),
            "health_file": str(self.base / "data/health.json"),
            "log_file": str(self.base / "logs/bridge.log"),
            "status_interval_seconds": 5,
            "defaults": {"file_settle_seconds": 1, "poll_interval_seconds": 1, "retry_limit": 3},
            "cameras": [],
        }
        path = self.base / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path, BridgeConfig.from_json(path)

    @staticmethod
    def clip(name, mtime=1, size=10):
        return Clip(Path("/recordings/front") / name, size, mtime, 0)


class ConfigurationTests(BaseCase):
    def test_checked_in_example_is_valid_and_is_ui_managed(self):
        config = BridgeConfig.from_json(Path(__file__).parents[1] / "config.example.json")
        self.assertEqual(config.recordings_root, Path("/recordings"))
        self.assertEqual(config.cameras, ())

    def test_safe_subdirectory_and_ids(self):
        root = self.base / "recordings"
        root.mkdir()
        self.assertEqual(validate_relative_directory(root, "camera-1")[0], "camera-1")
        with self.assertRaises(SettingsError):
            validate_relative_directory(root, "../escape")
        with self.assertRaises(SettingsError):
            validate_relative_directory(root, "/absolute")
        self.assertEqual(sanitize_camera_id("Front Door (Xiaomi)"), "front-door-xiaomi")
        self.assertEqual(sanitize_camera_id("123"), "camera-123")
        self.assertEqual(unique_camera_id("Front Door", {"front-door"}), "front-door-2")

    def test_registry_images_embed_default_config_and_market_source_is_complete(self):
        project = Path(__file__).parents[1]
        dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY config.example.json /app/config.default.json", dockerfile)
        self.assertIn("healthcheck.sh", dockerfile)
        self.assertTrue((project / "Dockerfile.mediamtx").is_file())
        self.assertTrue((project / "healthcheck.sh").is_file())
        self.assertEqual(BridgeConfig.from_json(project / "config.example.json").state_db, Path("/data/state.sqlite3"))

        market = project / "market"
        store = json.loads((market / "store-config.json").read_text(encoding="utf-8"))
        languages = json.loads((market / "supported-languages.json").read_text(encoding="utf-8"))
        compose = (market / "Apps/XiaomiCamerasNasRtsp/docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(store["version"], 2)
        self.assertIn("en_US", languages)
        self.assertTrue((market / "Apps/XiaomiCamerasNasRtsp/icon.svg").is_file())
        self.assertIn("main: bridge", compose)
        self.assertIn("/DATA/AppData/xiaomi-cameras-nas-rtsp/data:/data", compose)
        self.assertIn("/DATA/Cameras/xiaomi_camera_videos:/recordings:ro", compose)

    def test_all_zimaos_variants_use_the_migrated_read_only_recordings_path(self):
        project = Path(__file__).parents[1]
        migrated_root = "/DATA/Cameras/xiaomi_camera_videos"
        old_mount_root = "/media/" + "Safe-Storage/Cameras/xiaomi_camera_videos"
        deployment_files = (
            "docker-compose.yml",
            "zimaos-import.compose.yml",
            "zimaos-one-click.compose.yml",
            "market/Apps/XiaomiCamerasNasRtsp/docker-compose.yml",
        )
        for relative_path in deployment_files:
            contents = (project / relative_path).read_text(encoding="utf-8")
            self.assertIn(migrated_root, contents, relative_path)
            self.assertIn(":/recordings:ro", contents, relative_path)
            self.assertNotIn(old_mount_root, contents, relative_path)

        for relative_path in (".env.example", "README.md", "SPEC.md", "webapp.py"):
            contents = (project / relative_path).read_text(encoding="utf-8")
            self.assertIn(migrated_root, contents, relative_path)
            self.assertNotIn(old_mount_root, contents, relative_path)


class NearLiveQueueTests(BaseCase):
    def test_near_live_skips_initial_archive_and_persists_highwater(self):
        _, config = self.config()
        state = ClipState(config.state_db, "front")
        initial = [self.clip("001.mp4", 10), self.clip("002.mp4", 20), self.clip("003.mp4", 30)]
        self.assertEqual(state.discover(initial, "path", "near_live"), 1)
        self.assertEqual(state.status_of(initial[0].path), "skipped")
        self.assertEqual(state.status_of(initial[1].path), "skipped")
        self.assertEqual(state.status_of(initial[2].path), "pending")
        self.assertEqual(state.summary()["highwater_path"], str(initial[2].path))
        state.close()

        restarted = ClipState(config.state_db, "front")
        # Xiaomi is still uploading old files. Their newer NAS mtimes do not
        # matter when path ordering is configured: they remain behind highwater.
        late_old = self.clip("001-late-upload.mp4", 99)
        new_live = self.clip("004.mp4", 40)
        self.assertEqual(restarted.discover([*initial, late_old, new_live], "path", "backfill"), 1)
        self.assertEqual(restarted.status_of(late_old.path), "skipped")
        self.assertEqual(restarted.status_of(new_live.path), "pending")
        self.assertEqual(restarted.summary()["policy"], "near_live")
        self.assertEqual(restarted.summary()["highwater_path"], str(new_live.path))
        restarted.close()

    def test_backfill_is_oldest_first_and_reset_restarts_near_live(self):
        _, config = self.config()
        state = ClipState(config.state_db, "front")
        clips = [self.clip("001.mp4", 10), self.clip("002.mp4", 20), self.clip("003.mp4", 30)]
        self.assertEqual(state.discover(clips, "path", "backfill"), 3)
        self.assertEqual(state.next_pending("path").path, clips[0].path)
        self.assertEqual(state.summary()["policy"], "backfill")
        state.reset_near_live()
        self.assertFalse(state.summary()["initialized"])
        self.assertIsNone(state.summary()["highwater_path"])
        self.assertEqual(state.discover(clips, "path", "backfill"), 1)
        self.assertEqual(state.summary()["policy"], "near_live")
        self.assertEqual(state.next_pending("path").path, clips[-1].path)
        state.close()

    def test_interrupted_playback_requeues_per_camera(self):
        _, config = self.config()
        front = ClipState(config.state_db, "front")
        garden = ClipState(config.state_db, "garden")
        clip = self.clip("004.mp4", 40)
        front.discover([clip], policy="backfill")
        garden.discover([clip], policy="backfill")
        front.mark_playing(front.next_pending("path"))
        garden.mark_completed(garden.next_pending("path"))
        front.close()
        garden.close()
        front = ClipState(config.state_db, "front")
        garden = ClipState(config.state_db, "garden")
        self.assertIsNotNone(front.next_pending("path"))
        self.assertIsNone(garden.next_pending("path"))
        front.close()
        garden.close()

    def test_runtime_refuses_policy_switch_after_initialization(self):
        _, config = self.config()
        root = config.recordings_root
        (root / "front").mkdir(parents=True)
        store = SettingsStore(self.base / "data/settings.json", root, "setup-owner", "a-long-unique-password")
        settings = store.update(".", "blueiris", "a-reader-password-which-is-long", {"front": {"enabled": True, "camera_id": "front", "name": "Front", "start_policy": "near_live"}})
        state = ClipState(config.state_db, "front")
        state.discover([self.clip("003.mp4", 30)], policy="near_live")
        state.close()
        switched = json.loads(json.dumps(settings))
        switched["cameras"]["front"]["start_policy"] = "backfill"
        runtime = BridgeRuntime(config, store)
        try:
            with self.assertRaisesRegex(SettingsError, "already been initialized"):
                runtime.apply(switched, persist=False)
        finally:
            runtime.stop()


class SettingsTests(BaseCase):
    def make_store(self):
        root = self.base / "recordings"
        (root / "front").mkdir(parents=True)
        (root / "front" / "nested").mkdir()
        store = SettingsStore(self.base / "data/settings.json", root, "setup-owner", "a-long-unique-password")
        return root, store

    def test_scan_is_immediate_children_and_preserves_missing_selection(self):
        root, store = self.make_store()
        self.assertEqual([row["key"] for row in store.scan_candidates()[0]], ["front"])
        settings = store.update(".", "blueiris", "a-reader-password-which-is-long", {"front": {"enabled": True, "camera_id": "front", "name": "Front", "start_policy": "near_live"}})
        self.assertTrue(settings["cameras"]["front"]["enabled"])
        root.rename(self.base / "recordings-offline")
        self.assertEqual(store.scan_candidates()[0], [])
        self.assertIn("front", store.public()["cameras"])

    def test_enabled_camera_requires_reader_password(self):
        _, store = self.make_store()
        with self.assertRaisesRegex(SettingsError, "reader password"):
            store.update(".", "blueiris", "", {"front": {"enabled": True, "camera_id": "front", "name": "Front", "start_policy": "near_live"}})

    def test_password_is_hashed_and_never_public(self):
        _, store = self.make_store()
        store.update(".", "blueiris", "a-reader-password-which-is-long", {})
        self.assertTrue(store.public()["reader"]["password_set"])
        encoded = json.dumps(store.internal())
        self.assertNotIn("a-reader-password-which-is-long", encoded)
        self.assertNotIn("password_hash", json.dumps(store.public()))


class FakeRuntime:
    def __init__(self):
        self.applied = []
        self.resets = []
        self.stopped = False

    def status(self):
        return {"media_error": "", "cameras": {}}

    def apply(self, settings):
        self.applied.append(settings)

    def reset_near_live(self, key):
        self.resets.append(key)

    def stop(self):
        self.stopped = True


class WebUiTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.old_environment = {name: os.environ.get(name) for name in ("SETUP_ADMIN_USERNAME", "SETUP_ADMIN_PASSWORD", "SETUP_SESSION_SECRET")}
        os.environ.update({"SETUP_ADMIN_USERNAME": "setup-owner", "SETUP_ADMIN_PASSWORD": "a-long-unique-password", "SETUP_SESSION_SECRET": "this-is-a-test-session-secret-with-more-than-32-bytes"})
        self.config_path, _ = self.config()
        (self.base / "recordings" / "front").mkdir(parents=True)
        self.runtime = FakeRuntime()
        self.app = create_app(self.config_path, runtime=self.runtime)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        for name, value in self.old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        super().tearDown()

    def csrf(self, path="/login"):
        page = self.client.get(path)
        return re.search(r'name=csrf_token value="([^"]+)"', page.get_data(as_text=True)).group(1)

    def login(self):
        response = self.client.post("/login", data={"csrf_token": self.csrf(), "username": "setup-owner", "password": "a-long-unique-password"})
        self.assertEqual(response.status_code, 302)

    def test_authentication_csrf_and_settings_apply(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.post("/login", data={}).status_code, 400)
        self.login()
        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn("front", dashboard)
        self.assertEqual(self.client.post("/settings", data={}).status_code, 400)
        self.assertEqual(
            self.client.post("/logout", data={"csrf_token": self.csrf("/")}, headers={"Origin": "http://attacker.invalid"}).status_code,
            403,
        )
        token = self.csrf("/")
        response = self.client.post("/settings", data={
            "csrf_token": token,
            "recordings_subdirectory": ".",
            "reader_username": "blueiris",
            "reader_password": "a-reader-password-which-is-long",
            "reader_password_confirm": "a-reader-password-which-is-long",
            "camera_key": "front",
            "enabled:front": "on",
            "camera_id:front": "front-door",
            "name:front": "Front Door",
            "start_policy:front": "near_live",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.runtime.applied[-1]["cameras"]["front"]["camera_id"], "front-door")
        api = self.client.get("/api/status").get_data(as_text=True)
        self.assertNotIn("a-reader-password-which-is-long", api)
        self.assertNotIn("password_hash", api)
        self.client.post("/reset-near-live", data={"csrf_token": self.csrf("/"), "camera_key": "front", "confirm": "on"})
        self.assertEqual(self.runtime.resets, ["front"])


class WorkerReconciliationTests(BaseCase):
    def test_reconcile_keeps_unchanged_worker_and_replaces_changed_worker(self):
        _, config = self.config()
        source = self.base / "recordings" / "front"
        source.mkdir(parents=True)
        camera = CameraConfig("front", "Front", (source,), "xiaomi/front", config.defaults, "near_live")
        supervisor = WorkerSupervisor(config, publisher_factory=lambda command, log: CompletedProcess())
        try:
            supervisor.reconcile([camera])
            first_worker = supervisor._workers["front"][1]
            supervisor.reconcile([camera])
            self.assertIs(supervisor._workers["front"][1], first_worker)
            changed = CameraConfig("front", "Front", (source,), "xiaomi/front", config.defaults, "backfill")
            supervisor.reconcile([changed])
            self.assertIsNot(supervisor._workers["front"][1], first_worker)
            supervisor.reconcile([])
            self.assertEqual(supervisor.snapshots(), {})
        finally:
            supervisor.stop()


if __name__ == "__main__":
    unittest.main()
