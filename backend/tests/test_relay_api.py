import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.registration.account_key_crypto import AccountKeyCrypto


class RelayApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        from backend.web import application

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name)
        self.gr = mock.Mock()
        self.gr.config = {
            "relay_enabled": False,
            "relay_strategy": "fill_first",
        }
        self.gr.DEFAULT_CONFIG = self.gr.config.copy()
        self.gr.load_config.return_value = None
        self.gr.get_registration_repository.return_value = mock.Mock()
        self.data_patch = mock.patch.object(application, "DATA_DIR", self.data_root)
        self.gr_patch = mock.patch.object(application, "_gr", return_value=self.gr)
        self.session_patch = mock.patch.object(application, "_valid_session", return_value=True)
        self.data_patch.start()
        self.gr_patch.start()
        self.session_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.gr_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.client = TestClient(application.create_app())

    def test_machine_surface_is_disabled_and_non_responses_is_rejected(self):
        disabled = self.client.get("/v1/models")
        self.assertEqual(disabled.status_code, 404)
        unsupported = self.client.post("/v1/chat/completions", json={})
        self.assertEqual(unsupported.status_code, 404)
        self.assertIn("/v1/responses", unsupported.json()["error"]["message"])

    def test_console_overview_and_credential_rotation_boundary(self):
        overview = self.client.get("/api/relay/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["in_flight"], 0)
        self.assertEqual(overview.json()["cooling_down"], 0)

        rotated = self.client.post("/api/relay/keys/rotate")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertTrue(rotated.headers.get("cache-control", "").lower().find("no-store") >= 0)
        credential = rotated.json()["relay_api_key"]
        self.assertTrue(credential.startswith("sk-relay-"))
        self.assertNotIn(credential, (self.data_root / "relay" / "relay_state.json").read_text())

    def test_startup_fails_closed_when_repository_initialization_fails(self):
        from backend.web import application

        self.gr.get_registration_repository.side_effect = RuntimeError("schema rejected")
        with self.assertRaisesRegex(RuntimeError, "schema rejected"):
            with TestClient(application.create_app()):
                pass

    def test_probe_reports_running_job_as_conflict(self):
        from backend.web import application

        with mock.patch.object(
            application.job_coordinator,
            "idle_guard",
            side_effect=RuntimeError("job is running"),
        ), mock.patch.object(
            application.job_coordinator,
            "status",
            return_value={"running": True},
        ):
            response = self.client.post("/api/relay/pool/1/probe")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("注册任务运行中", response.text)


class RelayLegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        from backend.web import application

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name)
        relay_root = self.data_root / "relay"
        relay_root.mkdir(parents=True, exist_ok=True)
        crypto = AccountKeyCrypto(self.data_root)
        self.relay_db = relay_root / "relay_state.sqlite3"
        with sqlite3.connect(self.relay_db) as db:
            db.executescript("""
            PRAGMA user_version=2;
            CREATE TABLE relay_pool (
              account_id INTEGER PRIMARY KEY, profile_name TEXT NOT NULL,
              site_key TEXT NOT NULL, origin TEXT NOT NULL, key_ciphertext TEXT NOT NULL,
              key_id INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0,
              enabled INTEGER NOT NULL DEFAULT 1, in_flight INTEGER NOT NULL DEFAULT 0,
              cooldown_until REAL NOT NULL DEFAULT 0, last_used_at REAL NOT NULL DEFAULT 0,
              last_status TEXT NOT NULL DEFAULT '', last_http_status INTEGER NOT NULL DEFAULT 0,
              models_json TEXT NOT NULL DEFAULT '[]', models_updated_at REAL NOT NULL DEFAULT 0
            );
            """)
            db.execute(
                "INSERT INTO relay_pool(account_id,profile_name,site_key,origin,key_ciphertext,key_id,group_id) VALUES(?,?,?,?,?,?,?)",
                (12, "legacy", "site", "https://legacy.test", crypto.encrypt("legacy-key"), 88, 4),
            )
        self.store = mock.Mock()
        self.gr = mock.Mock()
        self.gr.config = {"relay_enabled": False, "relay_strategy": "fill_first"}
        self.gr.DEFAULT_CONFIG = self.gr.config.copy()
        self.gr.get_registration_repository.return_value = self.store
        self.data_patch = mock.patch.object(application, "DATA_DIR", self.data_root)
        self.gr_patch = mock.patch.object(application, "_gr", return_value=self.gr)
        self.data_patch.start()
        self.gr_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.gr_patch.stop)

    def test_startup_migrates_legacy_asset_then_drops_table(self):
        from backend.web import application

        self.store.account_for_result.return_value = {"id": 31}
        self.store.upsert_account_key.return_value = 72
        with TestClient(application.create_app()):
            pass
        self.store.upsert_account_key.assert_called_once()
        self.store.set_relay_key.assert_called_once_with(31, 72)
        with sqlite3.connect(self.relay_db) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_pool'"
                ).fetchone()
            )

    def test_orphaned_legacy_asset_fails_closed_without_dropping_table(self):
        from backend.web import application

        self.store.account_for_result.return_value = None
        with self.assertRaisesRegex(RuntimeError, "has no canonical Account"):
            with TestClient(application.create_app()):
                pass
        with sqlite3.connect(self.relay_db) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_pool'"
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()
