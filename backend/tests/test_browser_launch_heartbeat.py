"""浏览器启动心跳与任务阶段回归。

Scope: 2026-09-03 运行观察 —— Camoufox mmdb 缓存未命中时，__enter__() 会先同步
下载 MaxMind GeoLite2（IPv4+IPv6 约 45 MB），慢速出口下实测 8 分钟静默，任务快照
整段停在"任务启动中"，运维侧无法与死锁区分。这里锁定三层契约：

1. 心跳文案按 GeoIP 缓存状态区分，且都能被协调器识别；
2. 心跳必须在进入 __enter__() 之前发出，否则无法解释静默窗口；
3. 协调器收到心跳后，内存阶段与落库快照都离开"任务启动中"。
"""

import unittest
from unittest import mock

from backend.automation import session as browser_session
from backend.shared.stages import LOG_BROWSER_LAUNCHING_PREFIX, STAGE_BROWSER_LAUNCHING
from backend.web.jobs import RegistrationJobCoordinator


class GeoipCacheStateTests(unittest.TestCase):
    def test_needs_update_reports_cache_miss(self):
        with (
            mock.patch("camoufox.geolocation.needs_update", return_value=True),
            mock.patch("camoufox.geolocation.get_mmdb_path") as get_path,
        ):
            self.assertFalse(browser_session._geoip_cache_ready())
            get_path.assert_not_called()

    def test_missing_mmdb_file_reports_cache_miss(self):
        path = mock.Mock()
        path.exists.return_value = False
        with (
            mock.patch("camoufox.geolocation.needs_update", return_value=False),
            mock.patch("camoufox.geolocation.get_mmdb_path", return_value=path),
        ):
            self.assertFalse(browser_session._geoip_cache_ready())

    def test_present_mmdb_files_report_cache_ready(self):
        path = mock.Mock()
        path.exists.return_value = True
        with (
            mock.patch("camoufox.geolocation.needs_update", return_value=False),
            mock.patch("camoufox.geolocation.get_mmdb_path", return_value=path),
        ):
            self.assertTrue(browser_session._geoip_cache_ready())

    def test_unreadable_state_is_unknown_not_ready(self):
        # 判定失败必须回退到 None（通用文案），不得谎报"缓存已就绪"。
        with mock.patch.dict("sys.modules", {"camoufox.geolocation": None}):
            self.assertIsNone(browser_session._geoip_cache_ready())


class BrowserLaunchHeartbeatTests(unittest.TestCase):
    def test_all_variants_carry_stage_prefix(self):
        for ready in (True, False, None):
            line = browser_session.browser_launch_heartbeat(ready)
            self.assertTrue(line.startswith(LOG_BROWSER_LAUNCHING_PREFIX), line)
            self.assertIn(STAGE_BROWSER_LAUNCHING, line)

    def test_cache_miss_wording_names_download_and_size(self):
        line = browser_session.browser_launch_heartbeat(False)
        self.assertIn("缓存未命中", line)
        self.assertIn("GeoLite2", line)
        self.assertIn(browser_session.GEOIP_DOWNLOAD_HINT, line)

    def test_ready_wording_does_not_claim_download(self):
        self.assertNotIn("缓存未命中", browser_session.browser_launch_heartbeat(True))
        self.assertNotIn("缓存未命中", browser_session.browser_launch_heartbeat(None))

    def test_heartbeat_precedes_browser_launch(self):
        events = []

        def fake_context(opts):
            events.append("launch")
            raise browser_session.BrowserBackendUnavailable("stop before real browser")

        logged = []

        def record(message):
            logged.append(message)
            events.append("log")

        with (
            mock.patch.object(browser_session, "create_camoufox_options", return_value={
                "headless": True,
                "locale": "en-US",
            }),
            mock.patch.object(browser_session, "_geoip_cache_ready", return_value=False),
            mock.patch.object(browser_session, "_launch_camoufox_context", side_effect=fake_context),
            mock.patch.object(browser_session, "_note_start_failure", return_value=1),
            mock.patch.object(browser_session, "_cleanup_profile_dir"),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            with self.assertRaisesRegex(Exception, "启动失败"):
                browser_session.start_browser(log_callback=record)

        self.assertEqual(events[:2], ["log", "launch"], "心跳必须先于浏览器启动发出")
        self.assertTrue(logged[0].startswith(LOG_BROWSER_LAUNCHING_PREFIX), logged)
        self.assertIn("缓存未命中", logged[0])

    def test_no_heartbeat_without_log_callback(self):
        # 无回调路径（如内部自愈重启）保持原行为：静默启动、只在失败时抛错。
        with (
            mock.patch.object(browser_session, "create_camoufox_options", return_value={
                "headless": True,
                "locale": "en-US",
            }),
            mock.patch.object(browser_session, "_launch_camoufox_context", side_effect=browser_session.BrowserBackendUnavailable("x")),
            mock.patch.object(browser_session, "_note_start_failure", return_value=1),
            mock.patch.object(browser_session, "_cleanup_profile_dir"),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            with self.assertRaisesRegex(Exception, "启动失败"):
                browser_session.start_browser()


class _FakeRepo:
    def __init__(self):
        self.saved = []

    def save_job_snapshot(self, payload):
        self.saved.append(dict(payload))


class CoordinatorStageTests(unittest.TestCase):
    def _coordinator(self):
        coord = RegistrationJobCoordinator()
        repo = _FakeRepo()
        coord.restore_from_database = lambda: None
        coord._repository = lambda: repo
        coord._current_stage = "任务启动中"
        coord._target_count = 1
        return coord, repo

    def test_heartbeat_moves_stage_and_persists_snapshot(self):
        coord, repo = self._coordinator()
        coord._append_log(browser_session.browser_launch_heartbeat(False))

        self.assertEqual(coord.status()["current_stage"], STAGE_BROWSER_LAUNCHING)
        self.assertTrue(repo.saved, "阶段变更必须强制落库，绕开 1 秒节流")
        self.assertEqual(repo.saved[-1]["current_stage"], STAGE_BROWSER_LAUNCHING)

    def test_stage_returns_to_registering_when_attempt_starts(self):
        coord, _ = self._coordinator()
        coord._append_log(browser_session.browser_launch_heartbeat(True))
        coord._append_log("--- 开始第 1/1 个账号（Profile X） ---")
        self.assertEqual(coord.status()["current_stage"], "注册中")

    def test_late_heartbeat_cannot_override_completed_stage(self):
        coord, _ = self._coordinator()
        coord._append_log(browser_session.browser_launch_heartbeat(True))
        coord._append_log("[+] Sub2API 注册成功: a@b.example（dashboard 证据确认）")
        coord._append_log("[+] 注册成功: a@b.example")
        self.assertEqual(coord.status()["current_stage"], "任务收尾中")

        coord._append_log(browser_session.browser_launch_heartbeat(False))
        self.assertEqual(coord.status()["current_stage"], "任务收尾中")

    def test_unrelated_logs_keep_stage_unchanged(self):
        coord, repo = self._coordinator()
        coord._append_log("[*] 任务批次: web-20260903_144813-4a1964")
        self.assertEqual(coord.status()["current_stage"], "任务启动中")
        self.assertEqual(repo.saved, [])


if __name__ == "__main__":
    unittest.main()
