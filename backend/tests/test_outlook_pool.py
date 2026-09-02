import unittest
from datetime import datetime, timezone

from backend.mailbox import outlook_pool
from backend.mailbox.utilities import extract_verification_code


class FakeResponse:
    def __init__(self, data, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, server):
        self.server = server
        self.cookies = {}
        self.proxies = None

    def post(self, url, **kwargs):
        self.server["login_calls"] += 1
        self.server["login_payloads"].append(kwargs.get("json"))
        return FakeResponse({"success": True, "launch_url": "/extension-login/once"})

    def get(self, url, **kwargs):
        if "/extension-login/" in url:
            self.cookies["session"] = f"session-{self.server['login_calls']}"
            return FakeResponse({}, headers={"set-cookie": "session=ignored; Path=/"})
        if url.endswith("/api/csrf-token"):
            self.server["csrf_headers"].append(dict(kwargs.get("headers") or {}))
            status_code = (
                self.server["csrf_statuses"].pop(0)
                if self.server["csrf_statuses"]
                else 200
            )
            if status_code != 200:
                return FakeResponse({"success": False}, status_code=status_code)
            return FakeResponse(
                {"csrf_token": "csrf-value", "csrf_disabled": False},
                headers={"set-cookie": "csrf_session=bound; Path=/"},
            )
        raise AssertionError(url)

    def put(self, url, **kwargs):
        self.server["put_calls"].append(
            {
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "json": kwargs.get("json"),
            }
        )
        return FakeResponse({"success": True, "message": "状态更新成功"})


class OutlookEmailCodeTimeTests(unittest.TestCase):
    def test_login_cookie_rejects_absolute_launch_url(self):
        server = {"login_calls": 0, "login_payloads": [], "csrf_headers": [], "csrf_statuses": [], "put_calls": []}

        class AbsoluteLaunchSession(FakeSession):
            def post(self, url, **kwargs):
                self.server["login_calls"] += 1
                return FakeResponse(
                    {"success": True, "launch_url": "https://evil.example/extension-login/token"}
                )

        with self.assertRaises(Exception):
            outlook_pool.login_cookie(
                lambda: AbsoluteLaunchSession(server),
                "http://mail",
                "synthetic-password",
            )

    def test_numeric_code_accepts_true_sota_context_with_is(self):
        self.assertEqual(
            extract_verification_code(
                "Hello operator,\n\nYour verification code is:\n\n482731\n"
            ),
            "482731",
        )
        self.assertEqual(extract_verification_code("验证码为：928415"), "928415")
        self.assertIsNone(extract_verification_code("Order 482731 was created"))

    def test_domain_match_supports_only_leading_suffix_wildcards(self):
        allowed = {"qq.com", "*.edu.cn"}
        self.assertTrue(outlook_pool.domain_matches_allowed("qq.com", allowed))
        self.assertTrue(outlook_pool.domain_matches_allowed("mail.example.edu.cn", allowed))
        self.assertFalse(outlook_pool.domain_matches_allowed("edu.cn", allowed))
        self.assertFalse(outlook_pool.domain_matches_allowed("example.cn", allowed))
        self.assertTrue(outlook_pool.domain_matches_allowed("anything.invalid", {"*"}))

    def test_get_messages_preserves_all_by_merging_inbox_and_junk(self):
        calls = []

        def http_get(url, **kwargs):
            calls.append(kwargs.get("params"))
            folder = kwargs["params"]["folder"]
            return FakeResponse(
                {"success": True, "emails": [{"id": folder, "subject": folder}]}
            )

        messages = (
            outlook_pool.get_messages(
                http_get, "http://mail", "key", "user@example.com", folder="all"
            )
        )
        self.assertEqual([call["folder"] for call in calls], ["inbox", "junkemail"])
        self.assertEqual([item["id"] for item in messages], ["inbox", "junkemail"])

    def test_acquire_skips_unreadable_account_before_returning(self):
        accounts = [
            {"email": "broken@qq.com", "status": "active"},
            {"email": "ready@qq.com", "status": "active"},
        ]
        calls = []

        def http_get(url, **kwargs):
            email = kwargs["params"]["email"]
            calls.append(email)
            if email == "broken@qq.com":
                return FakeResponse({"success": False, "error": "invalid grant"})
            return FakeResponse({"success": True, "emails": []})

        with unittest.mock.patch.object(
            outlook_pool, "get_accounts", return_value=accounts
        ):
            email, _ = outlook_pool.acquire_email(
                http_get,
                lambda: None,
                "http://mail",
                api_key="key",
                pick_mode="sequential",
            )

        self.assertEqual(email, "ready@qq.com")
        self.assertEqual(calls, ["broken@qq.com", "ready@qq.com"])
        outlook_pool.release_email(email)
        with unittest.mock.patch.object(
            outlook_pool, "get_accounts", return_value=accounts
        ):
            retry, _ = outlook_pool.acquire_email(
                http_get,
                lambda: None,
                "http://mail",
                api_key="key",
                pick_mode="sequential",
                preflight_messages=False,
            )
        self.assertEqual(retry, "broken@qq.com")

    def test_message_received_at_supports_api_timestamp_formats(self):
        self.assertEqual(
            outlook_pool.message_received_at({"timestamp": 1_700_000_000_000}),
            1_700_000_000,
        )
        self.assertEqual(
            outlook_pool.message_received_at({"date": "2026-08-04T12:00:00Z"}),
            datetime(2026, 8, 4, 12, tzinfo=timezone.utc).timestamp(),
        )
        self.assertIsNone(outlook_pool.message_received_at({"date": "unknown"}))

    def test_wait_for_code_ignores_messages_before_submission(self):
        submitted_at = 1_700_000_000.5
        requested = []

        def http_get(url, **kwargs):
            requested.append((url, kwargs))
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "old",
                            "subject": "OLD-111 xAI",
                            "date": submitted_at - 1,
                            "body_preview": "OLD-111",
                        },
                        {
                            "id": "missing-time",
                            "subject": "MIS-444 xAI",
                            "body_preview": "MIS-444",
                        },
                        {
                            "id": "new",
                            "subject": "NEW-222 xAI",
                            "date": submitted_at + 1,
                            "body_preview": "NEW-222",
                        },
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

        self.assertEqual(code, "NEW-222")
        self.assertEqual(requested[0][1]["params"]["email"], "fixture@outlook.com")

    def test_wait_for_code_accepts_same_second_timestamp(self):
        submitted_at = 1_700_000_000.75

        def http_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "same-second",
                            "subject": "Your verification code",
                            "body_preview": "Your verification code is: 381204",
                            "date": 1_700_000_000,
                        }
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

        self.assertEqual(code, "381204")

    def test_wait_for_code_reads_numeric_hyphenated_subject(self):
        submitted_at = 1_786_770_721.584

        def http_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "new-code",
                            "subject": "SpaceXAI confirmation code: 180-699",
                            "date": submitted_at + 1,
                            "from": "noreply@x.ai",
                        }
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

        self.assertEqual(code, "180-699")

    def test_recipient_parser_supports_string_list_and_graph_shapes(self):
        self.assertEqual(
            outlook_pool.message_recipient_addresses(
                {
                    "to": "Alias A <alias-a@qq.com>, alias-b@foxmail.com",
                    "toRecipients": [
                        {"emailAddress": {"address": "alias-c@qq.com"}}
                    ],
                }
            ),
            {"alias-a@qq.com", "alias-b@foxmail.com", "alias-c@qq.com"},
        )

    def test_wait_for_code_skips_same_inbox_message_for_other_alias(self):
        submitted_at = 1_700_000_000.0

        def http_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "shared-message",
                            "to": "alias-b@foxmail.com",
                            "subject": "Your verification code",
                            "body_preview": "Your verification code is: 482731",
                            "date": submitted_at + 1,
                        }
                    ],
                }
            )

        with unittest.mock.patch.object(
            outlook_pool.time, "time", side_effect=[0, 0, 2]
        ):
            with self.assertRaisesRegex(Exception, "未收到验证码"):
                outlook_pool.wait_for_code(
                    http_get,
                    lambda: None,
                    "http://mail-pool.test",
                    "alias-a@qq.com",
                    api_key="api-key",
                    source="accounts",
                    timeout=1,
                    poll_interval=0,
                    min_received_at=submitted_at,
                    raise_if_cancelled=lambda _callback: None,
                    sleep_with_cancel=lambda _seconds, _callback: None,
                )

        with unittest.mock.patch.object(
            outlook_pool.time, "time", side_effect=[0, 0]
        ):
            code = outlook_pool.wait_for_code(
                http_get,
                lambda: None,
                "http://mail-pool.test",
                "alias-b@foxmail.com",
                api_key="api-key",
                source="accounts",
                timeout=1,
                poll_interval=0,
                min_received_at=submitted_at,
                raise_if_cancelled=lambda _callback: None,
                sleep_with_cancel=lambda _seconds, _callback: None,
            )
        self.assertEqual(code, "482731")

    def test_wait_for_code_accepts_legacy_message_without_recipient_metadata(self):
        submitted_at = 1_700_000_000.0

        def http_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "legacy-message",
                            "subject": "Your verification code",
                            "body_preview": "Your verification code is: 928415",
                            "date": submitted_at + 1,
                        }
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "alias-a@qq.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )
        self.assertEqual(code, "928415")


if __name__ == "__main__":
    unittest.main()
