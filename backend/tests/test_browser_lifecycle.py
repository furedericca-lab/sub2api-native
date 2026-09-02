import unittest
from types import SimpleNamespace
from unittest import mock

from backend.automation import session as browser_session


class CamoufoxProcessMatchTests(unittest.TestCase):
    def tearDown(self):
        browser_session.set_browser_session(None, None)
        browser_session.allow_browser_launches()

    @staticmethod
    def _identity_state(
        *,
        profile_dir="/tmp/sub2api-native-camoufox/test-a",
        cookies=None,
        origins=None,
    ):
        context = mock.Mock()
        context.storage_state.return_value = {
            "cookies": list(cookies or []),
            "origins": list(origins or []),
        }
        browser_session.set_browser_session(
            SimpleNamespace(user_data_path=profile_dir),
            SimpleNamespace(raw_context=context),
        )

    def test_fresh_browser_identity_requires_new_empty_managed_profile(self):
        self._identity_state()
        snapshot = browser_session.assert_fresh_browser_identity(
            previous_profile_dir="/tmp/sub2api-native-camoufox/test-old"
        )
        self.assertEqual(snapshot["cookie_count"], 0)
        self.assertEqual(snapshot["site_origin_count"], 0)

    def test_fresh_browser_identity_rejects_reused_profile(self):
        path = "/tmp/sub2api-native-camoufox/test-a"
        self._identity_state(profile_dir=path)
        with self.assertRaisesRegex(
            browser_session.BrowserIdentityIsolationError, "资料目录未轮换"
        ):
            browser_session.assert_fresh_browser_identity(previous_profile_dir=path)

    def test_fresh_browser_identity_rejects_cookie_or_site_storage(self):
        self._identity_state(cookies=[{"name": "session"}])
        with self.assertRaisesRegex(
            browser_session.BrowserIdentityIsolationError, "存在 Cookie"
        ):
            browser_session.assert_fresh_browser_identity()

        self._identity_state(
            profile_dir="/tmp/sub2api-native-camoufox/test-b",
            origins=[{"origin": "https://site.example", "localStorage": []}],
        )
        with self.assertRaisesRegex(
            browser_session.BrowserIdentityIsolationError, "存在站点存储"
        ):
            browser_session.assert_fresh_browser_identity()

    def test_matches_camoufox_executables_and_managed_profiles(self):
        self.assertTrue(browser_session._is_camoufox_process("/cache/camoufox/camoufox-bin", ""))
        self.assertTrue(
            browser_session._is_camoufox_process(
                "/usr/lib/firefox/firefox",
                "firefox -profile /tmp/sub2api-native-camoufox/123-profile",
            )
        )

    def test_does_not_match_regular_firefox(self):
        self.assertFalse(
            browser_session._is_camoufox_process(
                "/usr/lib/firefox/firefox",
                "firefox https://example.com",
            )
        )

    def test_emergency_block_prevents_browser_restart(self):
        browser_session.block_browser_launches()
        with self.assertRaisesRegex(RuntimeError, "紧急终止"):
            browser_session.start_browser()

    def test_kill_all_targets_camoufox_tree_only(self):
        processes = {
            101: (1, "/cache/camoufox/camoufox", "camoufox"),
            102: (101, "/usr/lib/helper", "content process"),
            201: (1, "/usr/lib/firefox/firefox", "firefox https://example.com"),
        }
        killed = []
        with (
            mock.patch.object(browser_session, "_linux_processes", return_value=processes),
            mock.patch.object(browser_session, "_cleanup_all_managed_profiles", return_value=2),
            mock.patch.object(browser_session.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            result = browser_session.kill_all_camoufox_processes()

        self.assertEqual(result, {"killed": 2, "profiles_cleaned": 2})
        self.assertEqual({pid for pid, _ in killed}, {101, 102})
        self.assertNotIn(201, {pid for pid, _ in killed})

    def test_kill_all_browser_processes_keeps_regular_browsers(self):
        processes = {
            101: (1, "/cache/camoufox/camoufox", "camoufox"),
            102: (101, "/usr/lib/helper", "content process"),
            401: (1, "/usr/bin/google-chrome", "google-chrome https://example.com"),
        }
        killed = []
        with (
            mock.patch.object(browser_session, "_linux_processes", return_value=processes),
            mock.patch.object(browser_session, "_cleanup_all_managed_profiles", return_value=2),
            mock.patch.object(browser_session.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            result = browser_session.kill_all_browser_processes()

        self.assertEqual(result, {"killed": 2, "profiles_cleaned": 2})
        self.assertEqual({pid for pid, _ in killed}, {101, 102})
        self.assertNotIn(401, {pid for pid, _ in killed})


if __name__ == "__main__":
    unittest.main()
