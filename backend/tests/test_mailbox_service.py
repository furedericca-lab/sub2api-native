import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.mailbox import service


class MailboxServiceResolutionTests(unittest.TestCase):
    def test_empty_and_legacy_bases_use_loopback(self):
        self.assertEqual(service.resolve_api_base({}), service.DEFAULT_API_BASE)
        self.assertEqual(
            service.resolve_api_base({"outlookemail_api_base": "http://outlook-email:5000/"}),
            service.DEFAULT_API_BASE,
        )

    def test_explicit_external_base_is_preserved(self):
        with mock.patch.dict(service.os.environ, {"SUB2API_OUTLOOKEMAIL_API_BASE": ""}, clear=False):
            self.assertEqual(
                service.resolve_api_base({"outlookemail_api_base": "https://mail.example.test/api"}),
                "https://mail.example.test/api",
            )

    def test_embedded_environment_overrides_legacy_config_base(self):
        with mock.patch.dict(
            service.os.environ,
            {"SUB2API_OUTLOOKEMAIL_API_BASE": service.DEFAULT_API_BASE},
            clear=False,
        ):
            self.assertEqual(
                service.resolve_api_base({"outlookemail_api_base": "https://mail.example.test/api"}),
                service.DEFAULT_API_BASE,
            )

    def test_runtime_env_parser_does_not_execute_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.env"
            path.write_text(
                "LOGIN_PASSWORD=secret value\n"
                "SECRET_KEY='key-value'\n"
                "BROKEN LINE\n"
                "$(touch %s)=bad\n" % (Path(tmp) / "marker"),
                encoding="utf-8",
            )
            parsed = service.read_runtime_env(path)
        self.assertEqual(parsed["LOGIN_PASSWORD"], "secret value")
        self.assertEqual(parsed["SECRET_KEY"], "key-value")
        self.assertFalse((Path(tmp) / "marker").exists())

    def test_embedded_runtime_file_wins_over_bootstrap_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.env"
            path.write_text(
                "LOGIN_PASSWORD=current-runtime-password\nSECRET_KEY=key-value\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                service.os.environ,
                {
                    "OUTLOOKEMAIL_RUNTIME_ENV": str(path),
                    "LOGIN_PASSWORD": "bootstrap-password",
                },
                clear=False,
            ):
                self.assertEqual(service.resolve_login_password({}), "current-runtime-password")

    def test_management_url_validates_host_and_ipv6(self):
        with mock.patch.dict(
            service.os.environ,
            {"OUTLOOKEMAIL_PUBLIC_PORT": "15001", "OUTLOOKEMAIL_PUBLIC_HOST": "192.0.2.10"},
        ):
            self.assertEqual(
                service.management_url("untrusted.example", "/extension-login/tok"),
                "http://192.0.2.10:15001/extension-login/tok",
            )
        with mock.patch.dict(
            service.os.environ,
            {"OUTLOOKEMAIL_PUBLIC_PORT": "15001", "OUTLOOKEMAIL_PUBLIC_HOST": "2001:db8::1"},
        ):
            self.assertEqual(
                service.management_url("untrusted.example", "/extension-login/tok", scheme="https"),
                "https://[2001:db8::1]:15001/extension-login/tok",
            )
        for host in ("evil/path", "evil..example", "user@example.com", "", "mail.example.test"):
            with self.subTest(host=host):
                with mock.patch.dict(service.os.environ, {"OUTLOOKEMAIL_PUBLIC_HOST": ""}, clear=False):
                    with self.assertRaises(service.MailboxServiceError):
                        service.management_url(host, "/extension-login/tok")

    def test_management_url_rejects_wildcard_public_host(self):
        with mock.patch.dict(service.os.environ, {"OUTLOOKEMAIL_PUBLIC_HOST": "0.0.0.0"}):
            with self.assertRaises(service.MailboxServiceError):
                service.management_url("127.0.0.1", "/extension-login/tok")

    def test_launch_path_accepts_only_one_time_endpoint(self):
        self.assertEqual(
            service.validate_launch_path("/extension-login/abc?next=%2F"),
            "/extension-login/abc?next=%2F",
        )
        for value in ("https://evil.test/x", "//evil.test/x", "/login", "/extension-login/"):
            with self.subTest(value=value):
                with self.assertRaises(service.MailboxPayloadError):
                    service.validate_launch_path(value)


class MailboxServiceHttpContractTests(unittest.TestCase):
    def test_status_raises_unavailable_when_upstream_cannot_be_reached(self):
        with mock.patch.object(
            service,
            "_request_json",
            side_effect=service.MailboxUnavailableError("synthetic transport failure"),
        ):
            with self.assertRaises(service.MailboxUnavailableError):
                service.get_status({"outlookemail_api_key": "test-api-key"})

    def test_status_raises_unavailable_for_non_success_root(self):
        with mock.patch.object(
            service,
            "_request_json",
            return_value=(503, {}, {"error": "synthetic upstream failure"}),
        ):
            with self.assertRaises(service.MailboxUnavailableError):
                service.get_status({})

    def test_status_is_non_sensitive_and_counts_accounts(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/"):
                return 200, {"X-App-Version": "v3.0.6"}, None
            return 200, {}, {"success": True, "total": 12, "accounts": []}

        with mock.patch.object(service, "_request_json", side_effect=fake_request):
            status = service.get_status({"outlookemail_api_key": "test-api-key"})
        self.assertTrue(status.healthy)
        self.assertEqual(status.version, "v3.0.6")
        self.assertEqual(status.account_count, 12)
        self.assertTrue(status.integration_key_configured)
        payload = status.as_dict()
        self.assertNotIn("test-api-key", json.dumps(payload))
        self.assertEqual(len(calls), 2)

    def test_launch_rejects_upstream_absolute_url(self):
        with mock.patch.object(
            service,
            "_request_json",
            return_value=(200, {}, {"success": True, "launch_url": "https://evil.test/x"}),
        ):
            with mock.patch.dict(service.os.environ, {"LOGIN_PASSWORD": "test-password"}, clear=False):
                with self.assertRaises(service.MailboxPayloadError):
                    service.launch_url({})

    def test_launch_rejects_upstream_password_failure_without_exposing_body(self):
        with mock.patch.object(
            service,
            "_request_json",
            return_value=(401, {}, {"success": False, "error": "synthetic password failure"}),
        ):
            with mock.patch.dict(service.os.environ, {"LOGIN_PASSWORD": "test-password"}, clear=False):
                with self.assertRaises(service.MailboxPayloadError) as caught:
                    service.launch_url({})
        self.assertNotIn("synthetic password failure", str(caught.exception))


class MailboxRouteTests(unittest.TestCase):
    def _client(self, tmp: Path, *, authenticated: bool):
        from fastapi.testclient import TestClient

        from backend.web import application
        from backend.registration.store import RegistrationRepository

        gr = mock.Mock()
        gr.config = {"outlookemail_api_key": "synthetic-key"}
        gr.DEFAULT_CONFIG = {}
        gr.CONFIG_FILE = str(tmp / "config.json")
        gr.get_registration_repository.return_value = RegistrationRepository(tmp / "results.db")
        patches = [
            mock.patch.object(application, "_gr", return_value=gr),
            mock.patch.object(application, "_web_auth_enabled", return_value=True),
            mock.patch.object(application, "_valid_session", return_value=authenticated),
            mock.patch.object(application, "DATA_DIR", tmp),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return application, TestClient(application.create_app())

    def test_mailbox_routes_require_parent_session(self):
        with tempfile.TemporaryDirectory() as raw:
            application, client = self._client(Path(raw), authenticated=False)
            response = client.get("/api/mailbox/status")
            self.assertEqual(response.status_code, 401)
            self.assertTrue(response.json()["auth_required"])

    def test_launch_route_returns_same_host_management_url(self):
        with tempfile.TemporaryDirectory() as raw:
            application, client = self._client(Path(raw), authenticated=True)
            with mock.patch.object(
                application.mailbox_service, "launch_url", return_value="/extension-login/synthetic"
            ), mock.patch.dict(
                application.mailbox_service.os.environ,
                {"OUTLOOKEMAIL_PUBLIC_HOST": "192.0.2.10"},
            ):
                response = client.post(
                    "/api/mailbox/launch",
                    json={"next": "/"},
                    headers={"Host": "untrusted.example"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["url"],
                "http://192.0.2.10:15000/extension-login/synthetic",
            )
            self.assertNotIn("synthetic-key", response.text)

    def test_status_route_masks_integration_key(self):
        with tempfile.TemporaryDirectory() as raw:
            application, client = self._client(Path(raw), authenticated=True)
            with mock.patch.object(
                application.mailbox_service,
                "get_status",
                return_value=service.MailboxStatus(True, "v3.0.6", 3, True),
            ):
                response = client.get("/api/mailbox/status")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["integration_key_configured"])
            self.assertNotIn("synthetic-key", response.text)

    def test_status_route_returns_safe_503_when_mailbox_is_unavailable(self):
        with tempfile.TemporaryDirectory() as raw:
            application, client = self._client(Path(raw), authenticated=True)
            with mock.patch.object(
                application.mailbox_service,
                "get_status",
                side_effect=service.MailboxUnavailableError("synthetic transport failure"),
            ):
                response = client.get("/api/mailbox/status")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"ok": False, "error": "邮箱服务暂不可用"})
            self.assertNotIn("synthetic transport failure", response.text)


if __name__ == "__main__":
    unittest.main()
