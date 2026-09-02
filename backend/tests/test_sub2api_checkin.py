import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from backend.integrations.sub2api_checkin import (
    CamoufoxCaptchaSolver,
    CheckinApiError,
    CheckinNetworkError,
    STATUS_ALREADY,
    STATUS_AUTH_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNCERTAIN,
    STATUS_UNSUPPORTED,
    Sub2ApiCheckinService,
)
from backend.registration.store import RegistrationRepository
from backend.web import application


class FakeClient:
    base_url = "https://example.test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, *, payload=None, token=""):
        self.calls.append((method, path, payload, bool(token)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSolver:
    def __init__(self, token="captcha-token"):
        self.token = token
        self.calls = []

    def solve(self, provider, settings, page_url):
        self.calls.append((provider, dict(settings), page_url))
        return None if provider in {"", "none"} else self.token


class CheckinProtocolTests(unittest.TestCase):
    def test_verify_credentials_stops_after_login(self):
        client = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
        ])
        result = Sub2ApiCheckinService(client, FakeSolver()).verify_credentials(
            "a@example.test", "secret"
        )
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertIn("凭据验证成功", result.message)
        self.assertEqual(
            [path for _, path, _, _ in client.calls],
            ["/api/v1/settings/public", "/api/v1/auth/login"],
        )

    def test_camoufox_solver_disables_geoip_probe_but_keeps_shared_backend(self):
        solver = CamoufoxCaptchaSolver()
        fake_page = object()
        with mock.patch(
        "backend.integrations.sub2api_captcha.browser_session.start_browser",
            return_value=(object(), fake_page),
        ) as start:
            self.assertIs(solver._ensure_page(), fake_page)
        start.assert_called_once_with(log_callback=None, geoip_override=False)

    def test_cap_solver_uses_registered_widget_element_api(self):
        raw_page = mock.Mock()
        raw_page.evaluate.return_value = {"token": "valid-cap-token-1234"}
        page = mock.Mock(raw_page=raw_page)
        solver = CamoufoxCaptchaSolver(attempts=1)
        solver._page = page

        token = solver._solve_cap({
            "cap_endpoint": "https://cap.example.test/key/",
            "cap_asset_url": "https://cdn.example.test/cap-widget",
        })

        self.assertEqual(token, "valid-cap-token-1234")
        page_html = raw_page.set_content.call_args.args[0]
        self.assertIn("customElements.whenDefined('cap-widget')", page_html)
        self.assertIn("document.createElement('cap-widget')", page_html)
        self.assertIn("widget.addEventListener('solve'", page_html)
        self.assertIn("querySelector('.captcha-trigger')", page_html)
        self.assertNotIn("widget.solve()", page_html)
        self.assertNotIn("new Cap(", page_html)
        self.assertIn("https://cdn.example.test/cap-widget/+esm", page_html)

    def test_turnstile_uses_intercepted_target_origin_without_inherited_csp(self):
        raw_page = mock.Mock()
        page = mock.Mock(raw_page=raw_page)
        solver = CamoufoxCaptchaSolver(attempts=1)
        solver._page = page

        with mock.patch(
        "backend.integrations.sub2api_captcha.get_turnstile_token",
            return_value="t" * 80,
        ):
            token = solver._solve_turnstile(
                {
                    "captcha_site_key": "site-key",
                    "captcha_action": "daily_checkin",
                    "captcha_cdata": "attempt-bound-data",
                },
                "https://site.example.test/dashboard",
            )

        self.assertEqual(token, "t" * 80)
        raw_page.route.assert_called_once()
        raw_page.goto.assert_called_once_with(
            "https://site.example.test/dashboard",
            wait_until="domcontentloaded",
        )
        raw_page.unroute.assert_called_once()
        raw_page.wait_for_selector.assert_called_once()
        fulfill_challenge = raw_page.route.call_args.args[1]
        route = mock.Mock()
        fulfill_challenge(route)
        body = route.fulfill.call_args.kwargs["body"]
        self.assertIn("window.turnstile.render", body)
        self.assertIn('id="cf-challenge"', body)
        self.assertNotIn('id="turnstile"', body)
        self.assertIn('data-site-key="site-key"', body)
        self.assertIn('data-action="daily_checkin"', body)
        self.assertIn('data-cdata="attempt-bound-data"', body)
        self.assertIn("options.action = body.dataset.action", body)
        self.assertIn("options.cData = body.dataset.cdata", body)

    def test_turnstile_setup_failure_is_normalized_as_captcha_error(self):
        raw_page = mock.Mock()
        raw_page.wait_for_selector.side_effect = RuntimeError("frame load failed")
        page = mock.Mock(raw_page=raw_page)
        solver = CamoufoxCaptchaSolver(attempts=1)
        solver._page = page

        with self.assertRaisesRegex(Exception, "Turnstile 验证未完成"):
            solver._solve_turnstile(
                {"captcha_site_key": "site-key"},
                "https://site.example.test/dashboard",
            )

    def test_already_checked_in_stops_before_attempt(self):
        client = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
            {"enabled": True, "checked_in": True, "checkin_date": "2026-08-26"},
        ])
        result = Sub2ApiCheckinService(client, FakeSolver()).run("a@example.test", "secret")
        self.assertEqual(result.status, STATUS_ALREADY)
        self.assertEqual(len(client.calls), 3)

    def test_success_uses_attempt_and_single_final_mutation(self):
        client = FakeClient([
            {"captcha_provider": "cap", "cap_endpoint": "https://cap.test/key/"},
            {"access_token": "access"},
            {"enabled": True, "checked_in": False},
            {
                "attempt_id": "attempt-1",
                "captcha_provider": "turnstile",
                "captcha_site_key": "attempt-site-key",
                "captcha_action": "daily_checkin",
                "captcha_cdata": "attempt-bound-data",
            },
            {"captcha_enabled": True, "captcha_provider": "cap"},
            {"checkin_date": "2026-08-26"},
        ])
        solver = FakeSolver()
        result = Sub2ApiCheckinService(client, solver).run("a@example.test", "secret")
        self.assertEqual(result.status, STATUS_SUCCESS)
        final_calls = [call for call in client.calls if call[1] == "/api/v1/checkin"]
        self.assertEqual(len(final_calls), 1)
        self.assertEqual(final_calls[0][2]["attempt_id"], "attempt-1")
        self.assertIn("captcha_token", final_calls[0][2])
        self.assertEqual(len(solver.calls), 2)
        self.assertEqual(solver.calls[1][0], "turnstile")
        self.assertEqual(solver.calls[1][1]["captcha_site_key"], "attempt-site-key")
        self.assertEqual(solver.calls[1][1]["captcha_action"], "daily_checkin")
        self.assertEqual(solver.calls[1][1]["captcha_cdata"], "attempt-bound-data")

    def test_final_network_failure_is_confirmed_by_status_when_possible(self):
        client = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
            {"enabled": True, "checked_in": False},
            {"attempt_id": 7},
            {"captcha_enabled": False},
            CheckinNetworkError("timeout"),
            {"enabled": True, "checked_in": True},
        ])
        result = Sub2ApiCheckinService(client, FakeSolver()).run("a@example.test", "secret")
        self.assertEqual(result.status, STATUS_SUCCESS)

    def test_final_network_failure_without_confirmation_is_uncertain(self):
        client = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
            {"enabled": True, "checked_in": False},
            {"attempt_id": 7},
            {"captcha_enabled": False},
            CheckinNetworkError("timeout"),
            CheckinNetworkError("timeout"),
        ])
        result = Sub2ApiCheckinService(client, FakeSolver()).run("a@example.test", "secret")
        self.assertEqual(result.status, STATUS_UNCERTAIN)

    def test_disabled_and_auth_failure_are_normalized(self):
        disabled = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
            {"enabled": False},
        ])
        self.assertEqual(
            Sub2ApiCheckinService(disabled, FakeSolver()).run("a@example.test", "secret").status,
            STATUS_UNSUPPORTED,
        )
        denied = FakeClient([
            {"captcha_provider": "none"},
            CheckinApiError(401, "denied", "INVALID_CREDENTIALS"),
        ])
        self.assertEqual(
            Sub2ApiCheckinService(denied, FakeSolver()).run("a@example.test", "secret").status,
            STATUS_AUTH_FAILURE,
        )

    def test_cap_rejection_is_not_mislabeled_as_generic_upstream_failure(self):
        rejected = FakeClient([
            {"captcha_provider": "cap"},
            CheckinApiError(400, "Cap verification failed", "400"),
        ])
        result = Sub2ApiCheckinService(rejected, FakeSolver()).run(
            "a@example.test", "secret"
        )
        self.assertEqual(result.status, "captcha_manual_required")
        self.assertIn("登录验证码", result.message)

    def test_claim_captcha_rejection_identifies_checkin_stage(self):
        rejected = FakeClient([
            {"captcha_provider": "none"},
            {"access_token": "access"},
            {"enabled": True, "checked_in": False},
            {"attempt_id": "attempt-1"},
            {"captcha_enabled": False},
            CheckinApiError(400, "captcha action mismatch", "DAILY_CHECKIN_CAPTCHA_ACTION_MISMATCH"),
        ])
        result = Sub2ApiCheckinService(rejected, FakeSolver()).run(
            "a@example.test", "secret"
        )
        self.assertEqual(result.status, "captcha_manual_required")
        self.assertIn("签到验证码", result.message)


class CheckinApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RegistrationRepository(f"{self.temp.name}/results.sqlite3")
        self.profile = self.store.create_profile(
            {"name": "test", "site_key": "bmapi"}
        )
        self.gr = mock.Mock()
        self.gr.get_registration_repository.return_value = self.store
        self.gr.get_proxies.return_value = {}
        self.gr.config = {}
        self.gr.DEFAULT_CONFIG = {}
        self.gr.load_config.return_value = None
        self.gr._wire_runtime_modules.return_value = None
        self.patcher = mock.patch.object(application, "_gr", return_value=self.gr)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.auth = mock.patch.object(application, "_valid_session", return_value=True)
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.client = TestClient(application.create_app())

    def _result(self, *, success=True, failure_type=""):
        return self.store.add_result(
            {
                "profile_id": self.profile["id"],
                "started_at": "2026-08-26T00:00:00Z",
                "finished_at": "2026-08-26T00:00:01Z",
                "email": "account@example.test",
                "password": "secret",
                "registration_status": "success" if success else "failure",
                "success": success,
                "failure_type": failure_type,
            }
        )

    def test_route_rejects_failed_registration(self):
        row = self._result(success=False)
        response = self.client.post(f"/api/accounts/{row}/checkin")
        self.assertEqual(response.status_code, 409)

    def test_route_loads_credentials_and_profile_origin_server_side(self):
        row_id = self._result(success=True)
        service = mock.Mock()
        service.run.return_value.as_dict.return_value = {
            "status": "already_checked_in",
            "message": "今天已经签到",
            "checkin_date": "2026-08-26",
            "next_reset_at": "",
        }
        with mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiCheckinService",
            return_value=service,
        ), mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiClient"
        ) as client_cls, mock.patch(
            "backend.integrations.sub2api_checkin.CamoufoxCaptchaSolver"
        ) as solver_cls:
            response = self.client.post(f"/api/accounts/{row_id}/checkin")
        self.assertEqual(response.status_code, 200, response.text)
        client_cls.assert_called_once_with("https://bmapi.020212.xyz", timeout=30, proxies={})
        service.run.assert_called_once_with("account@example.test", "secret")
        solver_cls.return_value.close.assert_called_once()

    def test_route_rejects_site_without_verified_checkin_support(self):
        profile = self.store.create_profile(
            {"name": "unsupported", "site_key": "true-sota"}
        )
        row_id = self.store.add_result(
            {
                "profile_id": profile["id"],
                "email": "account@example.test",
                "password": "secret",
                "registration_status": "success",
                "success": True,
            }
        )
        with mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiClient"
        ) as client_cls, mock.patch(
            "backend.integrations.sub2api_checkin.CamoufoxCaptchaSolver"
        ) as solver_cls:
            response = self.client.post(f"/api/accounts/{row_id}/checkin")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "该站点尚未验证签到功能")
        client_cls.assert_not_called()
        solver_cls.assert_not_called()

    def test_route_verifies_already_registered_credentials_before_promoting(self):
        row_id = self._result(success=False, failure_type="already_registered")
        service = mock.Mock()
        verification = SimpleNamespace(
            status="success",
            message="账号凭据验证成功（未执行签到）",
            as_dict=lambda: {
                "status": "success",
                "message": "账号凭据验证成功（未执行签到）",
                "checkin_date": "",
                "next_reset_at": "",
            },
        )
        service.verify_credentials.return_value = verification
        with mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiCheckinService",
            return_value=service,
        ), mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiClient"
        ), mock.patch(
            "backend.integrations.sub2api_checkin.CamoufoxCaptchaSolver"
        ):
            response = self.client.post(
                f"/api/accounts/{row_id}/verify-credentials",
                json={"password": "new-secret"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        service.verify_credentials.assert_called_once_with("account@example.test", "new-secret")
        promoted = self.store.get_results_by_ids([row_id])[0]
        self.assertEqual(promoted["registration_status"], "success")
        self.assertEqual(promoted["success"], 1)
        self.assertEqual(promoted["password"], "new-secret")
        self.assertEqual(__import__("json").loads(promoted["extra_json"])["credential_verification"], "live_login")

    def test_route_rejects_non_already_registered_records(self):
        row_id = self._result(success=False, failure_type="code_timeout")
        response = self.client.post(
            f"/api/accounts/{row_id}/verify-credentials",
            json={"password": "new-secret"},
        )
        self.assertEqual(response.status_code, 409)

    def test_route_keeps_record_unchanged_when_credential_verification_fails(self):
        row_id = self._result(success=False, failure_type="already_registered")
        before = self.store.get_results_by_ids([row_id])[0]
        service = mock.Mock()
        service.verify_credentials.return_value = SimpleNamespace(
            status=STATUS_AUTH_FAILURE,
            message="邮箱或密码错误",
        )
        with mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiCheckinService",
            return_value=service,
        ), mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiClient"
        ), mock.patch(
            "backend.integrations.sub2api_checkin.CamoufoxCaptchaSolver"
        ):
            response = self.client.post(
                f"/api/accounts/{row_id}/verify-credentials",
                json={"password": "wrong-secret"},
            )
        self.assertEqual(response.status_code, 409, response.text)
        after = self.store.get_results_by_ids([row_id])[0]
        self.assertEqual(after["registration_status"], before["registration_status"])
        self.assertEqual(after["failure_type"], before["failure_type"])
        self.assertEqual(after["password"], before["password"])
        self.assertEqual(after["extra_json"], before["extra_json"])

    def test_running_registration_rejects_before_runtime_reconfiguration(self):
        row_id = self._result(success=True)

        @contextmanager
        def running_guard():
            raise RuntimeError("已有注册任务在运行")
            yield

        with mock.patch.object(
            application.job_coordinator,
            "idle_guard",
            running_guard,
        ), mock.patch.object(
            application.job_coordinator,
            "status",
            return_value={"running": True},
        ), mock.patch(
            "backend.integrations.sub2api_checkin.Sub2ApiClient"
        ) as client_cls, mock.patch(
            "backend.integrations.sub2api_checkin.CamoufoxCaptchaSolver"
        ) as solver_cls:
            response = self.client.post(f"/api/accounts/{row_id}/checkin")

        self.assertEqual(response.status_code, 409)
        self.gr.load_config.assert_not_called()
        self.gr._wire_runtime_modules.assert_not_called()
        client_cls.assert_not_called()
        solver_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
