import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.integrations.sub2api_account_operations import (
    AccountOperationError,
    AccountOperationsService,
)
from backend.registration.store import RegistrationRepository


class FakeCrypto:
    def encrypt(self, value: str) -> str:
        return "encrypted:" + value


class FakeKeys:
    def __init__(self, remote, reveals):
        self.remote = list(remote)
        self.reveals = dict(reveals)

    def list_keys(self, _token):
        return list(self.remote)

    def reveal_key(self, _token, key_id, *, owned_key_ids=None):
        if owned_key_ids is not None and key_id not in owned_key_ids:
            raise AssertionError("ownership set missing key")
        value = self.reveals[key_id]
        if isinstance(value, Exception):
            raise value
        return value


class FakeSession:
    def __init__(self, keys):
        self.keys = keys
        self.token = "token"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FailingListKeys:
    def list_keys(self, _token):
        raise RuntimeError("key list unavailable")


class AccountOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")
        self.profile = self.store.create_profile({"name": "BMAPI", "site_key": "bmapi"})
        self.account = self.store.create_account(
            self.profile["id"], "account@example.com", "password-1"
        )

    def service(self, session):
        return AccountOperationsService(
            self.store,
            FakeCrypto(),
            session_factory=lambda *_args, **_kwargs: session,
        )

    def test_sync_reconciles_missing_keys_and_preserves_unrevealed_known_key(self):
        missing_id = self.store.upsert_account_key(
            self.account["id"], 10, "removed", "encrypted:removed", 1, "active"
        )
        known_id = self.store.upsert_account_key(
            self.account["id"], 11, "known-old", "encrypted:known", 1, "active"
        )
        self.store.set_relay_key(self.account["id"], missing_id)
        keys = FakeKeys(
            [
                {"id": 11, "name": "known", "group_id": 2, "status": "active"},
                {"id": 12, "name": "fresh", "group_id": 2, "status": "active"},
            ],
            {
                11: RuntimeError("detail unavailable"),
                12: SimpleNamespace(
                    id=12,
                    name="fresh",
                    group_id=2,
                    status="active",
                    secret="fresh-secret",
                ),
            },
        )
        summary = self.service(FakeSession(keys)).sync_keys(self.account["id"])

        self.assertEqual(summary.as_dict(), {
            "discovered": 2,
            "synced": 1,
            "unavailable": 1,
            "missing": 1,
        })
        self.assertEqual(self.store.get_account_key(missing_id)["status"], "missing")
        known = self.store.get_account_key(known_id)
        self.assertEqual((known["name"], known["group_id"], known["status"]), ("known", 2, "active"))
        fresh = self.store.account_key_by_remote_id(self.account["id"], 12)
        self.assertEqual(fresh["key_ciphertext"], "encrypted:fresh-secret")
        self.assertEqual(self.store.relay_assets(), [])

    def test_checkin_rejects_unverified_capability_before_browser_creation(self):
        profile = self.store.create_profile({"name": "True SOTA", "site_key": "true-sota"})
        account = self.store.create_account(
            profile["id"], "other@example.com", "password-2"
        )
        factory = mock.Mock()
        service = AccountOperationsService(
            self.store,
            FakeCrypto(),
            session_factory=factory,
        )

        with self.assertRaisesRegex(AccountOperationError, "尚未验证签到功能"):
            service.checkin(account["id"])
        factory.assert_not_called()

    def test_manual_add_persists_verified_account_when_key_list_is_unavailable(self):
        account, summary = self.service(FakeSession(FailingListKeys())).add_account(
            self.profile["id"], "manual@example.com", "password-3"
        )

        self.assertEqual(account["status"], "active")
        self.assertIn("API Key 同步失败", account["last_error"])
        self.assertEqual(
            summary.as_dict(),
            {"discovered": 0, "synced": 0, "unavailable": 1, "missing": 0},
        )
        self.assertIsNotNone(
            self.store.get_account_context(int(account["id"]))
        )

    def test_manual_add_rejects_disabled_profile_before_login(self):
        self.store.update_profile(self.profile["id"], {"enabled": False})
        factory = mock.Mock()
        service = AccountOperationsService(
            self.store,
            FakeCrypto(),
            session_factory=factory,
        )

        with self.assertRaisesRegex(AccountOperationError, "Profile 已停用"):
            service.add_account(
                self.profile["id"], "manual@example.com", "password-4"
            )
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
