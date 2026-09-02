import unittest

from backend.registration.site_failures import (
    ALREADY_REGISTERED,
    RegistrationResponseMonitor,
    classify_registration_failure,
)


class FakeResponse:
    def __init__(self, status, url, payload):
        self.status = status
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload


class FakeRawPage:
    def __init__(self):
        self.handler = None

    def on(self, event, handler):
        self.handler = handler

    def remove_listener(self, event, handler):
        if self.handler == handler:
            self.handler = None


class FakePage:
    def __init__(self):
        self.raw_page = FakeRawPage()


class SiteFailureTests(unittest.TestCase):
    def test_classifies_structured_email_exists_variants(self):
        for payload in (
            {"code": "EMAIL_EXISTS", "message": "conflict"},
            {"error": {"error_code": "EMAIL_ALREADY_EXISTS"}},
            {"reason": "email already exists"},
            {"detail": "该邮箱已存在"},
        ):
            with self.subTest(payload=payload):
                signal = classify_registration_failure(payload)
                self.assertIsNotNone(signal)
                self.assertEqual(signal.kind, ALREADY_REGISTERED)

    def test_does_not_classify_unrelated_exists_error(self):
        self.assertIsNone(
            classify_registration_failure(
                {"code": "INVITATION_CODE_EXISTS", "message": "code already exists"}
            )
        )

    def test_monitor_only_accepts_registration_error_responses(self):
        page = FakePage()
        monitor = RegistrationResponseMonitor(page)
        page.raw_page.handler(
            FakeResponse(
                409,
                "https://site.example/api/v1/auth/register",
                {"code": "EMAIL_EXISTS"},
            )
        )
        self.assertEqual(monitor.latest_signal().kind, ALREADY_REGISTERED)
        page.raw_page.handler(
            FakeResponse(
                409,
                "https://site.example/api/v1/admin/users",
                {"code": "EMAIL_EXISTS"},
            )
        )
        self.assertIsNone(monitor.latest_signal())
        monitor.close()
        self.assertIsNone(page.raw_page.handler)


if __name__ == "__main__":
    unittest.main()
