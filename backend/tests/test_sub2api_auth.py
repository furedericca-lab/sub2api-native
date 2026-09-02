import unittest

from backend.integrations.sub2api_auth import Sub2ApiAuthService, resolve_login_captcha


class FakeClient:
    base_url = "https://example.test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, *, payload=None, token=""):
        self.calls.append((method, path, payload, token))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSolver:
    def __init__(self, token="captcha-value"):
        self.token = token
        self.calls = []

    def solve(self, provider, settings, page_url):
        self.calls.append((provider, settings, page_url))
        return None if provider == "none" else self.token


class Sub2ApiAuthTests(unittest.TestCase):
    def test_turnstile_settings_use_exact_login_field(self):
        client = FakeClient([{"access_token": "access"}])
        solver = FakeSolver()
        token = Sub2ApiAuthService(client, solver).login(
            "account@example.test",
            "secret",
            {"turnstile_enabled": True, "turnstile_site_key": "site-key"},
        )
        self.assertEqual(token, "access")
        payload = client.calls[0][2]
        self.assertEqual(payload["turnstile_token"], "captcha-value")
        self.assertNotIn("captcha_token", payload)
        self.assertEqual(solver.calls[0][0], "turnstile")

    def test_cap_settings_use_generic_captcha_field(self):
        client = FakeClient([{"access_token": "access"}])
        token = Sub2ApiAuthService(client, FakeSolver()).login(
            "account@example.test",
            "secret",
            {"captcha_provider": "cap", "cap_endpoint": "https://cap.test/key/"},
        )
        self.assertEqual(token, "access")
        payload = client.calls[0][2]
        self.assertEqual(payload["captcha_token"], "captcha-value")
        self.assertNotIn("turnstile_token", payload)

    def test_no_captcha_does_not_add_token_field(self):
        client = FakeClient([{"access_token": "access"}])
        Sub2ApiAuthService(client, FakeSolver()).login(
            "account@example.test", "secret", {}
        )
        self.assertEqual(
            client.calls[0][2],
            {"email": "account@example.test", "password": "secret"},
        )

    def test_totp_second_step_returns_access_token(self):
        client = FakeClient([{"temp_token": "temporary"}, {"access_token": "access"}])
        token = Sub2ApiAuthService(client, FakeSolver()).login(
            "account@example.test",
            "secret",
            {},
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        self.assertEqual(token, "access")
        self.assertEqual(client.calls[1][1], "/api/v1/auth/login/2fa")
        self.assertEqual(len(client.calls[1][2]["totp_code"]), 6)

    def test_unknown_provider_fails_before_login(self):
        client = FakeClient([])
        with self.assertRaisesRegex(ValueError, "不支持"):
            Sub2ApiAuthService(client, FakeSolver()).login(
                "account@example.test",
                "secret",
                {"captcha_provider": "unknown"},
            )
        self.assertEqual(client.calls, [])

    def test_explicit_cap_takes_precedence_over_turnstile_flag(self):
        contract = resolve_login_captcha(
            {"captcha_provider": "cap", "turnstile_enabled": True}
        )
        self.assertEqual((contract.provider, contract.payload_field), ("cap", "captcha_token"))


if __name__ == "__main__":
    unittest.main()
