import tempfile
import threading
import unittest
import shutil
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.registration.store import RegistrationRepository
from backend.registration.account_key_crypto import AccountKeyCrypto
from backend.web import application


class AccountPoolApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_root = self.root / "data"
        self.store = RegistrationRepository(
            self.data_root / "accounts" / "registration_results.sqlite3"
        )
        self.profile = self.store.create_profile({"name": "BMAPI", "site_key": "bmapi"})
        self.account = self.store.create_account(
            self.profile["id"], "manual@example.com", "secret-password", "manual"
        )

        self.gr = mock.Mock()
        self.gr.get_registration_repository.return_value = self.store
        self.gr.get_proxies.return_value = {}
        self.gr.load_config.return_value = None
        self.gr._wire_runtime_modules.return_value = None
        self.data_patch = mock.patch.object(application, "DATA_DIR", self.data_root)
        self.gr_patch = mock.patch.object(application, "_gr", return_value=self.gr)
        self.auth_patch = mock.patch.object(application, "_valid_session", return_value=True)
        self.data_patch.start()
        self.gr_patch.start()
        self.auth_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.gr_patch.stop)
        self.addCleanup(self.auth_patch.stop)
        application._account_remote_guard = threading.Lock()
        self.client = TestClient(application.create_app())

    def test_account_list_and_detail_never_return_password(self):
        listed = self.client.get("/api/account-pool")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["accounts"][0]["email"], "manual@example.com")
        self.assertNotIn("password", listed.text)
        self.assertNotIn("secret-password", listed.text)

        detail = self.client.get(f"/api/account-pool/{self.account['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertNotIn("password", detail.json()["account"])
        self.assertNotIn("secret-password", detail.text)

    def test_registration_history_never_returns_attempt_password(self):
        self.store.add_result(
            {
                "profile_id": self.profile["id"],
                "email": "attempt@example.com",
                "password": "attempt-password",
                "registration_status": "success",
                "success": True,
            }
        )

        history = self.client.get("/api/accounts")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["items"][0]["email"], "attempt@example.com")
        self.assertNotIn("password", history.json()["items"][0])
        self.assertNotIn("attempt-password", history.text)

    def test_explicit_credentials_and_key_reveal_are_no_store(self):
        credentials = self.client.get(
            f"/api/account-pool/{self.account['id']}/credentials"
        )
        self.assertEqual(credentials.status_code, 200, credentials.text)
        self.assertEqual(credentials.headers.get("cache-control"), "no-store")
        self.assertEqual(credentials.json()["password"], "secret-password")

        crypto = AccountKeyCrypto(self.data_root)
        key_id = self.store.upsert_account_key(
            self.account["id"], 41, "codex-relay", crypto.encrypt("saved-key-secret"), 7, "active"
        )
        revealed = self.client.get(f"/api/api-keys/{key_id}/reveal")
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.headers.get("cache-control"), "no-store")
        self.assertEqual(revealed.json()["secret"], "saved-key-secret")

    def test_account_key_survives_deleting_all_relay_runtime(self):
        crypto = AccountKeyCrypto(self.data_root)
        key_id = self.store.upsert_account_key(
            self.account["id"], 42, "persistent", crypto.encrypt("persistent-secret"), 7, "active"
        )
        self.store.set_relay_key(self.account["id"], key_id)
        self.store.set_account_relay_enabled(self.account["id"], True)

        before = self.client.get(f"/api/api-keys/{key_id}/reveal")
        self.assertEqual(before.status_code, 200, before.text)
        shutil.rmtree(self.data_root / "relay")

        with TestClient(application.create_app()) as rebuilt:
            revealed = rebuilt.get(f"/api/api-keys/{key_id}/reveal")
            self.assertEqual(revealed.status_code, 200, revealed.text)
            self.assertEqual(revealed.json()["secret"], "persistent-secret")
            derived = rebuilt.get("/api/relay/pool")
            self.assertEqual(derived.status_code, 200, derived.text)
            self.assertEqual(derived.json()["items"][0]["account_id"], self.account["id"])

    def test_startup_fails_closed_when_ciphertext_has_no_canonical_key(self):
        crypto = AccountKeyCrypto(self.data_root)
        self.store.upsert_account_key(
            self.account["id"], 43, "lost-key", crypto.encrypt("cannot-recover"), 7, "active"
        )
        crypto.key_path.unlink()

        with self.assertRaisesRegex(RuntimeError, "api_keys.key is missing"):
            with TestClient(application.create_app()):
                pass
        self.assertFalse(crypto.key_path.exists())

    def test_account_pool_export_uses_current_canonical_credentials(self):
        self.store.add_result(
            {
                "profile_id": self.profile["id"],
                "email": "manual@example.com",
                "password": "historical-password",
                "registration_status": "success",
                "success": True,
            }
        )
        self.store.create_account(
            self.profile["id"], "manual@example.com", "updated-password", "manual"
        )
        exported = self.client.post(
            "/api/account-pool/credentials-txt/download",
            json={"ids": [self.account["id"]]},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.headers.get("cache-control"), "no-store")
        self.assertEqual(exported.text, "manual@example.com----updated-password\n")

    def test_manual_bmapi_checkin_uses_account_id(self):
        service = mock.Mock()
        service.checkin.return_value = {
            "status": "already_checked_in",
            "message": "今日已签到",
            "checkin_date": "2026-08-31",
            "next_reset_at": "",
        }
        with mock.patch(
            "backend.integrations.sub2api_account_operations.AccountOperationsService",
            return_value=service,
        ):
            response = self.client.post(
                f"/api/account-pool/{self.account['id']}/checkin"
            )
        self.assertEqual(response.status_code, 200, response.text)
        service.checkin.assert_called_once_with(self.account["id"])

    def test_batch_checkin_uses_canonical_account_ids_and_reports_each_result(self):
        second = self.store.create_account(
            self.profile["id"], "second@example.com", "second-password", "manual"
        )
        service = mock.Mock()
        service.checkin.side_effect = [
            {"status": "success", "message": "签到成功"},
            RuntimeError("上游暂不可用"),
        ]
        with mock.patch(
            "backend.integrations.sub2api_account_operations.AccountOperationsService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/account-pool/checkin",
                json={"ids": [self.account["id"], second["id"]]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["success"], 1)
        self.assertEqual(response.json()["failure"], 1)
        self.assertEqual(
            service.checkin.call_args_list,
            [mock.call(self.account["id"]), mock.call(second["id"])],
        )

    def test_remote_account_operation_reports_running_job_as_conflict(self):
        with mock.patch.object(
            application.job_coordinator,
            "idle_guard",
            side_effect=RuntimeError("job is running"),
        ), mock.patch.object(
            application.job_coordinator,
            "status",
            return_value={"running": True},
        ):
            response = self.client.post(
                f"/api/account-pool/{self.account['id']}/verify"
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("注册任务运行中", response.text)

    def test_relay_membership_is_owned_by_account_api(self):
        missing_key = self.client.put(
            f"/api/account-pool/{self.account['id']}/relay",
            json={"enabled": True},
        )
        self.assertEqual(missing_key.status_code, 422, missing_key.text)

        crypto = AccountKeyCrypto(self.data_root)
        key_id = self.store.upsert_account_key(
            self.account["id"], 51, "codex-relay", crypto.encrypt("account-key"), 2, "active"
        )
        self.store.set_relay_key(self.account["id"], key_id)
        enabled = self.client.put(
            f"/api/account-pool/{self.account['id']}/relay",
            json={"enabled": True},
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(self.store.get_account_context(self.account["id"])["relay_enabled"])

        disabled = self.client.put(
            f"/api/account-pool/{self.account['id']}/relay",
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(self.store.get_account_context(self.account["id"])["relay_enabled"])

        operations = {
            (route.path, method)
            for route in self.client.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("/api/relay/pool", "POST"), operations)
        self.assertNotIn(("/api/relay/pool/sync", "POST"), operations)
        self.assertNotIn(("/api/relay/pool/{account_id}/toggle", "POST"), operations)
        self.assertNotIn(("/api/relay/pool/{account_id}", "DELETE"), operations)


if __name__ == "__main__":
    unittest.main()
