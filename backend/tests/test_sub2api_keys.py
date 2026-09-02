import unittest
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.integrations.sub2api_keys import (
    ApiKeyCreateUncertainError,
    ApiKeyMutationUncertainError,
    ApiKeyProtocolError,
    ApiKeyValidationError,
    Sub2ApiKeyService,
)
from backend.integrations.sub2api_transport import Sub2ApiNetworkError
from backend.registration.store import RegistrationRepository
from backend.web import application


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, *, payload=None, token=""):
        self.calls.append((method, path, payload, token))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


GROUPS = {
    "code": 0,
    "data": [
        {
            "id": 7,
            "name": "Primary",
            "platform": "anthropic",
            "description": "Default group",
            "rate_multiplier": 1.2,
            "private_field": "ignored",
        }
    ],
    "message": "ok",
}


def key_page(items, *, pages=1):
    return {"items": items, "page": 1, "page_size": 100, "pages": pages, "total": len(items)}


class Sub2ApiKeyServiceTests(unittest.TestCase):
    def test_group_response_is_minimized(self):
        service = Sub2ApiKeyService(FakeClient([GROUPS]))
        self.assertEqual(
            service.list_groups("access"),
            [
                {
                    "id": 7,
                    "name": "Primary",
                    "platform": "anthropic",
                    "description": "Default group",
                    "rate_multiplier": 1.2,
                }
            ],
        )

    def test_list_masks_complete_upstream_key_without_leaking_characters(self):
        raw_secret = "secret-value-that-must-not-escape"
        service = Sub2ApiKeyService(
            FakeClient(
                [
                    key_page(
                        [
                            {
                                "id": 1,
                                "name": "default",
                                "group_id": 7,
                                "status": "active",
                                "key": raw_secret,
                            }
                        ]
                    )
                ]
            )
        )
        result = service.list_keys("access")
        self.assertEqual(result[0]["masked_key"], "********")
        self.assertNotIn("key", result[0])
        self.assertNotIn(raw_secret, repr(result))

    def test_create_sends_minimal_payload_once(self):
        client = FakeClient(
            [
                GROUPS,
                key_page([]),
                {"id": 9, "name": "native", "group_id": 7, "status": "active", "key": "new-secret-value"},
            ]
        )
        result = Sub2ApiKeyService(client).create_key("access", " native ", 7)
        posts = [call for call in client.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"name": "native", "group_id": 7})
        self.assertEqual(result.secret, "new-secret-value")
        self.assertFalse(result.reconciled)

    def test_network_failure_reconciles_new_id_without_replaying_post(self):
        client = FakeClient(
            [
                GROUPS,
                key_page([{"id": 2, "name": "old", "group_id": 7, "key": "old-secret-value"}]),
                Sub2ApiNetworkError("timeout"),
                key_page(
                    [
                        {"id": 2, "name": "old", "group_id": 7, "key": "old-secret-value"},
                        {"id": 3, "name": "native", "group_id": 7, "status": "active", "key": "new-secret-value"},
                    ]
                ),
            ]
        )
        result = Sub2ApiKeyService(client).create_key("access", "native", 7)
        self.assertTrue(result.reconciled)
        self.assertEqual(result.id, 3)
        self.assertEqual(len([call for call in client.calls if call[0] == "POST"]), 1)

    def test_ambiguous_reconciliation_fails_closed(self):
        client = FakeClient(
            [
                GROUPS,
                key_page([]),
                Sub2ApiNetworkError("timeout"),
                key_page(
                    [
                        {"id": 3, "name": "native", "group_id": 7, "key": "first-secret-value"},
                        {"id": 4, "name": "native", "group_id": 7, "key": "second-secret-value"},
                    ]
                ),
            ]
        )
        with self.assertRaisesRegex(ApiKeyCreateUncertainError, "无法唯一确认"):
            Sub2ApiKeyService(client).create_key("access", "native", 7)
        self.assertEqual(len([call for call in client.calls if call[0] == "POST"]), 1)

    def test_stale_group_rejected_before_any_key_read_or_post(self):
        client = FakeClient([GROUPS])
        with self.assertRaisesRegex(ApiKeyValidationError, "分组已不可用"):
            Sub2ApiKeyService(client).create_key("access", "native", 99)
        self.assertEqual(len(client.calls), 1)

    def test_reveal_requires_owned_key_and_reads_full_detail(self):
        client = FakeClient(
            [
                key_page([{"id": 12, "name": "native", "group_id": 7, "key": "masked-or-full"}]),
                {"id": 12, "name": "native", "group_id": 7, "status": "active", "key": "full-secret-value"},
            ]
        )
        result = Sub2ApiKeyService(client).reveal_key("access", 12)
        self.assertEqual(result.secret, "full-secret-value")
        self.assertEqual(client.calls[1][1], "/api/v1/keys/12")

    def test_reveal_rejects_key_from_another_account_before_detail(self):
        client = FakeClient([key_page([])])
        with self.assertRaisesRegex(ApiKeyValidationError, "不属于当前账号"):
            Sub2ApiKeyService(client).reveal_key("access", 12)
        self.assertEqual(len(client.calls), 1)

    def test_reveal_fails_closed_when_detail_is_masked(self):
        client = FakeClient(
            [
                key_page([{"id": 12, "name": "native", "group_id": 7, "key": "full-secret-value"}]),
                {"id": 12, "name": "native", "group_id": 7, "status": "active", "key": "********"},
            ]
        )
        with self.assertRaisesRegex(ApiKeyProtocolError, "完整值"):
            Sub2ApiKeyService(client).reveal_key("access", 12)

    def test_reveal_fails_closed_when_detail_id_changes(self):
        client = FakeClient(
            [
                key_page([{"id": 12, "name": "native", "group_id": 7, "key": "full-secret-value"}]),
                {"id": 13, "name": "other", "group_id": 7, "status": "active", "key": "other-secret-value"},
            ]
        )
        with self.assertRaisesRegex(ApiKeyProtocolError, "完整值"):
            Sub2ApiKeyService(client).reveal_key("access", 12)

    def test_update_group_sends_one_put_and_reconciles_remote_state(self):
        client = FakeClient(
            [
                GROUPS,
                key_page([{"id": 12, "name": "native", "group_id": 3}]),
                {"message": "updated"},
                key_page([{"id": 12, "name": "native", "group_id": 7, "status": "active"}]),
            ]
        )
        result = Sub2ApiKeyService(client).update_group("access", 12, 7)
        puts = [call for call in client.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], "/api/v1/keys/12")
        self.assertEqual(puts[0][2], {"group_id": 7})
        self.assertEqual(result["group_id"], 7)

    def test_update_group_network_uncertainty_never_replays_put(self):
        client = FakeClient(
            [
                GROUPS,
                key_page([{"id": 12, "name": "native", "group_id": 3}]),
                Sub2ApiNetworkError("timeout"),
                key_page([{"id": 12, "name": "native", "group_id": 3}]),
            ]
        )
        with self.assertRaisesRegex(ApiKeyMutationUncertainError, "尚未确认"):
            Sub2ApiKeyService(client).update_group("access", 12, 7)
        self.assertEqual(len([call for call in client.calls if call[0] == "PUT"]), 1)

    def test_delete_network_uncertainty_reconciles_without_replaying_delete(self):
        client = FakeClient(
            [
                key_page([{"id": 12, "name": "native", "group_id": 7}]),
                Sub2ApiNetworkError("timeout"),
                key_page([]),
            ]
        )
        self.assertTrue(Sub2ApiKeyService(client).delete_key("access", 12))
        self.assertEqual(len([call for call in client.calls if call[0] == "DELETE"]), 1)


class Sub2ApiKeyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name) / "data"
        self.store = RegistrationRepository(
            self.data_root / "accounts" / "registration_results.sqlite3"
        )
        self.profile = self.store.create_profile({"name": "test", "site_key": "true-sota"})
        self.gr = mock.Mock()
        self.gr.get_registration_repository.return_value = self.store
        self.gr.get_proxies.return_value = {"https": "http://proxy.test:8080"}
        self.gr.load_config.return_value = None
        self.gr._wire_runtime_modules.return_value = None
        self.patcher = mock.patch.object(application, "_gr", return_value=self.gr)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.data_patcher = mock.patch.object(application, "DATA_DIR", self.data_root)
        self.data_patcher.start()
        self.addCleanup(self.data_patcher.stop)
        self.auth = mock.patch.object(application, "_valid_session", return_value=True)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        application._account_remote_guard = threading.Lock()
        self.client = TestClient(application.create_app())

    def _result(self, *, success=True):
        return self.store.add_result(
            {
                "profile_id": self.profile["id"],
                "started_at": "2026-08-30T00:00:00Z",
                "finished_at": "2026-08-30T00:00:01Z",
                "email": "account@example.test",
                "password": "secret-password",
                "registration_status": "success" if success else "failure",
                "success": success,
            }
        )

    def test_context_uses_catalog_origin_and_returns_masked_metadata(self):
        row_id = self._result()
        auth_service = mock.Mock()
        auth_service.public_settings.return_value = {"captcha_provider": "none"}
        auth_service.login.return_value = "access-token"
        key_service = mock.Mock()
        key_service.list_groups.return_value = [{"id": 7, "name": "Primary"}]
        key_service.list_keys.return_value = [
            {"id": 2, "name": "existing", "group_id": 7, "status": "active", "masked_key": "********"}
        ]
        with mock.patch("backend.integrations.sub2api_auth.Sub2ApiAuthService", return_value=auth_service), \
             mock.patch("backend.integrations.sub2api_keys.Sub2ApiKeyService", return_value=key_service), \
             mock.patch("backend.integrations.sub2api_transport.Sub2ApiClient") as client_cls, \
             mock.patch("backend.integrations.sub2api_captcha.CamoufoxCaptchaSolver") as solver_cls:
            response = self.client.get(f"/api/accounts/{row_id}/api-key-context")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("access-token", response.text)
        self.assertNotIn("secret-password", response.text)
        self.assertEqual(response.json()["existing_key_count"], 1)
        client_cls.assert_called_once_with(
            "https://true-sota.com",
            timeout=30,
            proxies={"https": "http://proxy.test:8080"},
        )
        auth_service.login.assert_called_once_with(
            "account@example.test",
            "secret-password",
            {"captcha_provider": "none"},
        )
        solver_cls.return_value.close.assert_called_once()

    def test_create_returns_one_time_secret_with_no_store(self):
        row_id = self._result()
        auth_service = mock.Mock()
        auth_service.public_settings.return_value = {}
        auth_service.login.return_value = "access-token"
        key_service = mock.Mock()
        key_service.create_key.return_value = SimpleNamespace(
            as_dict=lambda: {
                "id": 8,
                "name": "native",
                "group_id": 7,
                "status": "active",
                "secret": "one-time-secret",
                "reconciled": False,
            }
        )
        with mock.patch("backend.integrations.sub2api_auth.Sub2ApiAuthService", return_value=auth_service), \
             mock.patch("backend.integrations.sub2api_keys.Sub2ApiKeyService", return_value=key_service), \
             mock.patch("backend.integrations.sub2api_transport.Sub2ApiClient"), \
             mock.patch("backend.integrations.sub2api_captcha.CamoufoxCaptchaSolver"):
            response = self.client.post(
                f"/api/accounts/{row_id}/api-keys",
                json={"name": "native", "group_id": 7},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.json()["key"]["secret"], "one-time-secret")
        key_service.create_key.assert_called_once_with("access-token", "native", 7)

    def test_uncertain_create_returns_504_with_no_store(self):
        row_id = self._result()
        auth_service = mock.Mock()
        auth_service.public_settings.return_value = {}
        auth_service.login.return_value = "access-token"
        key_service = mock.Mock()
        key_service.create_key.side_effect = ApiKeyCreateUncertainError("创建结果未知")
        with mock.patch("backend.integrations.sub2api_auth.Sub2ApiAuthService", return_value=auth_service), \
             mock.patch("backend.integrations.sub2api_keys.Sub2ApiKeyService", return_value=key_service), \
             mock.patch("backend.integrations.sub2api_transport.Sub2ApiClient"), \
             mock.patch("backend.integrations.sub2api_captcha.CamoufoxCaptchaSolver"):
            response = self.client.post(
                f"/api/accounts/{row_id}/api-keys",
                json={"name": "native", "group_id": 7},
            )
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_reveal_route_returns_secret_once_without_persisting(self):
        row_id = self._result()
        auth_service = mock.Mock()
        auth_service.public_settings.return_value = {}
        auth_service.login.return_value = "access-token"
        key_service = mock.Mock()
        key_service.reveal_key.return_value = SimpleNamespace(
            as_dict=lambda: {
                "id": 8,
                "name": "native",
                "group_id": 7,
                "status": "active",
                "secret": "existing-secret-value",
            }
        )
        with mock.patch("backend.integrations.sub2api_auth.Sub2ApiAuthService", return_value=auth_service), \
             mock.patch("backend.integrations.sub2api_keys.Sub2ApiKeyService", return_value=key_service), \
             mock.patch("backend.integrations.sub2api_transport.Sub2ApiClient"), \
             mock.patch("backend.integrations.sub2api_captcha.CamoufoxCaptchaSolver"):
            response = self.client.get(f"/api/accounts/{row_id}/api-keys/8/reveal")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.json()["key"]["secret"], "existing-secret-value")
        key_service.reveal_key.assert_called_once_with("access-token", 8)

    def test_failed_account_is_rejected_before_remote_operation(self):
        row_id = self._result(success=False)
        with mock.patch("backend.integrations.sub2api_transport.Sub2ApiClient") as client_cls:
            response = self.client.get(f"/api/accounts/{row_id}/api-key-context")
        self.assertEqual(response.status_code, 409)
        client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
