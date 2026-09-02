import unittest

from backend.registration.capabilities import discover_capabilities


class RegistrationCapabilitiesTests(unittest.TestCase):
    def test_turnstile_settings_envelope(self):
        caps = discover_capabilities({
            "code": 0,
            "data": {
                "registration_enabled": True,
                "email_verify_enabled": True,
                "turnstile_enabled": True,
                "turnstile_site_key": "site-key",
                "affiliate_enabled": True,
                "registration_email_suffix_whitelist": ["@qq.com", "@foxmail.com"],
            },
        })
        self.assertEqual(caps.captcha_provider, "turnstile")
        self.assertEqual(caps.captcha_site_key, "site-key")
        self.assertEqual(caps.email_suffixes, ("@qq.com", "@foxmail.com"))
        self.assertTrue(caps.email_verification)

    def test_cap_provider_is_discovered_without_turnstile(self):
        caps = discover_capabilities({
            "data": {
                "registration_enabled": True,
                "cap_endpoint": "https://cap.example/challenge",
                "captcha_site_key": "cap-key",
            }
        })
        self.assertEqual(caps.captcha_provider, "cap")
        self.assertEqual(caps.captcha_site_key, "cap-key")

    def test_raw_settings_and_unknown_values_are_safe(self):
        caps = discover_capabilities({
            "registration_enabled": 1,
            "email_verify_enabled": 0,
            "registration_email_suffix_whitelist": "@qq.com",
        })
        self.assertTrue(caps.registration_enabled)
        self.assertEqual(caps.email_suffixes, ("@qq.com",))
        self.assertEqual(caps.captcha_provider, "none")


if __name__ == "__main__":
    unittest.main()
