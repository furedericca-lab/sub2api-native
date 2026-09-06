"""浏览器进程回收与验证码挂钟预算的回归测试。

覆盖两类真实事故：上游把 Turnstile 降级成人工交互挑战后任务远超时限，以及
失败任务把闲置 Camoufox 进程留在容器里。
"""
import os
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.automation import session as browser_session
from backend.automation import turnstile as ts
from backend.integrations.sub2api_captcha import CamoufoxCaptchaSolver, CaptchaError
from backend.web.account_jobs import (
    AccountTaskRunner,
    AccountTaskSpec,
    SUCCEEDED,
    TERMINAL_STATUSES,
)

HAS_PROC = os.path.isdir("/proc")


def _spawn_marker_process(marker: str) -> subprocess.Popen:
    """启动一个 cmdline 中含 marker 的子进程，模拟本会话遗留的浏览器。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@unittest.skipUnless(HAS_PROC, "需要 /proc 才能按 profile 目录认领进程")
class BrowserProcessReclaimTest(unittest.TestCase):
    def tearDown(self) -> None:
        for attr in ("browser", "page", "profile_dir"):
            if hasattr(browser_session._tls, attr):
                setattr(browser_session._tls, attr, None)

    def test_profile_tree_only_matches_own_marker(self):
        mine = _spawn_marker_process("s2n-marker-mine")
        other = _spawn_marker_process("s2n-marker-other")
        try:
            pids = {pid for pid, _flag in browser_session.profile_process_tree("s2n-marker-mine")}
            self.assertIn(mine.pid, pids)
            self.assertNotIn(other.pid, pids)
        finally:
            for proc in (mine, other):
                proc.kill()

    def test_terminate_kills_stragglers(self):
        proc = _spawn_marker_process("s2n-marker-reap")
        try:
            self.assertEqual(
                browser_session.terminate_profile_processes("s2n-marker-reap"),
                1,
            )
            deadline = time.time() + 5
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll())
        finally:
            proc.kill()

    def test_stop_browser_reaps_when_quit_leaves_process(self):
        """quit() 成功或失效都必须确认进程真的消失。"""
        proc = _spawn_marker_process("s2n-marker-stop")
        quiet = lambda *_: None
        browser_session._tls.browser = SimpleNamespace(quit=lambda **_: None)
        browser_session._tls.page = object()
        browser_session._tls.profile_dir = "s2n-marker-stop"
        logs = []
        try:
            browser_session.stop_browser(force=True, log_callback=logs.append)
            self.assertIsNone(browser_session.active_browser())
            self.assertEqual(browser_session.profile_process_tree("s2n-marker-stop"), [])
            self.assertIn("回收残留浏览器进程", " ".join(logs))
        finally:
            proc.kill()

    def test_watchdog_fires_and_can_be_disarmed(self):
        proc = _spawn_marker_process("s2n-marker-watchdog")
        try:
            disarm = browser_session.arm_browser_watchdog(
                0.2, "s2n-marker-watchdog", lambda *_: None
            )
            deadline = time.time() + 5
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll())
            disarm()
        finally:
            proc.kill()

    def test_disarmed_watchdog_does_not_kill(self):
        proc = _spawn_marker_process("s2n-marker-disarmed")
        try:
            disarm = browser_session.arm_browser_watchdog(
                0.15, "s2n-marker-disarmed", lambda *_: None
            )
            disarm()
            time.sleep(0.4)
            self.assertIsNone(proc.poll())
        finally:
            proc.kill()


class TurnstileBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.armed = []
        self.disarmed = []

        class _FakePage:
            run_js = lambda self, script: ""  # noqa: ANN202 - 永远拿不到 token
            raw_page = SimpleNamespace(frames=[])

        self.patches = [
            mock.patch.object(ts, "active_page", lambda: object()),
            mock.patch.object(ts, "page", _FakePage()),
            mock.patch.object(ts, "current_profile_dir", lambda: "/tmp/s2n-fake-profile"),
            mock.patch.object(
                ts,
                "arm_browser_watchdog",
                lambda seconds, profile_dir, log_callback=None: (
                    self.armed.append(seconds),
                    lambda: self.disarmed.append(True),
                )[1],
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def test_budget_expiry_names_the_interactive_challenge(self):
        logs = []
        started = time.monotonic()
        with self.assertRaises(Exception) as raised:
            ts.get_turnstile_token(
                log_callback=logs.append, budget_seconds=0.01
            )
        self.assertIn("人工交互挑战", str(raised.exception))
        self.assertLess(time.monotonic() - started, 30.0)
        # 看门狗必须按预算挂上并解除，否则卡死的 Playwright 调用无人能打断；
        # 另外还有一个按“进度”续期的短 fuse，浏览器进程卡住时不等满预算。
        self.assertEqual(len(self.armed), 2)
        self.assertIn(5.01, [round(x, 3) for x in self.armed])
        self.assertLessEqual(min(self.armed), 20.0)
        self.assertEqual(len(self.disarmed), 2)

    def test_without_budget_behaviour_is_unchanged(self):
        with self.assertRaises(Exception):
            ts.get_turnstile_token(cancel_callback=lambda: True)
        self.assertEqual(self.armed, [])

    def test_cancellation_beats_budget(self):
        from backend.registration.runtime import RegistrationCancelled

        with self.assertRaises(RegistrationCancelled):
            ts.get_turnstile_token(cancel_callback=lambda: True, budget_seconds=60.0)


class SolverBudgetTest(unittest.TestCase):
    def test_no_task_deadline_means_no_budget_cap(self):
        self.assertIsNone(CamoufoxCaptchaSolver()._attempt_budget())
        self.assertIsNone(CamoufoxCaptchaSolver._poll_budget(None))

    def test_attempt_budget_is_capped_and_reserved(self):
        self.assertEqual(CamoufoxCaptchaSolver(deadline_callback=lambda: 300.0)._attempt_budget(), 120.0)
        self.assertEqual(CamoufoxCaptchaSolver(deadline_callback=lambda: 60.0)._attempt_budget(), 52.0)

    def test_poll_budget_leaves_room_to_finish(self):
        now = time.monotonic()
        self.assertEqual(CamoufoxCaptchaSolver._poll_budget(now + 120), 60.0)
        self.assertAlmostEqual(CamoufoxCaptchaSolver._poll_budget(now + 10), 7.0, places=1)
        self.assertEqual(CamoufoxCaptchaSolver._poll_budget(now - 5), 5.0)

    def test_insufficient_remaining_time_stops_before_launch(self):
        solver = CamoufoxCaptchaSolver(deadline_callback=lambda: 8.0)
        with self.assertRaises(CaptchaError):
            solver._attempt_budget()

    def test_budget_reaches_token_wait(self):
        solver = CamoufoxCaptchaSolver(deadline_callback=lambda: 300.0)
        seen = {}

        def _fake(**kwargs):
            seen.update(kwargs)
            return "t" * 120


        import backend.integrations.sub2api_captcha as cap

        raw_page = SimpleNamespace(route=lambda *a, **k: None, unroute=lambda *a, **k: None,
                                   goto=lambda *a, **k: None,
                                   wait_for_selector=lambda *a, **k: None)
        armed = []
        with mock.patch.object(cap, "get_turnstile_token", _fake), mock.patch.object(
            cap.browser_session,
            "arm_browser_watchdog",
            lambda seconds, profile_dir, log_callback=None: armed.append(seconds) or (lambda: None),
        ), mock.patch.object(
            solver, "_ensure_page", lambda: SimpleNamespace(raw_page=raw_page)
        ):
            token = solver._solve_turnstile_page(
                {"captcha_site_key": "k"}, "https://site.example/login"
            )
        self.assertEqual(token, "t" * 120)
        # 渲染完就进入轮询，预算上限 60s；外层看门狗只覆盖启动+渲染（≤55s），
        # 轮询自己再挂一个，否则卡住要等到整个尝试到期（实测 180s）。
        self.assertAlmostEqual(seen.get("budget_seconds"), 60.0, places=1)
        self.assertTrue(seen.get("arm_watchdog", True))
        self.assertEqual(len(armed), 1)
        self.assertLessEqual(armed[0], 55.0)


    def test_launch_overrun_stops_instead_of_continuing(self):
        """冷启动拖过预算时必须停下来，不能继续等 token（实测 20s 时限跑出 72s 的回归）。"""
        solver = CamoufoxCaptchaSolver(deadline_callback=lambda: 20.0)
        page = SimpleNamespace(
            route=lambda *a, **k: None,
            unroute=lambda *a, **k: None,
            goto=lambda *a, **k: None,
            wait_for_selector=lambda *a, **k: None,
        )

        def slow_launch():
            time.sleep(13.0)
            return SimpleNamespace(raw_page=page)

        with mock.patch.object(solver, "_ensure_page", slow_launch):
            with self.assertRaises(CaptchaError) as raised:
                solver._solve_turnstile_page(
                    {"captcha_site_key": "k"}, "https://site.example/login"
                )
        self.assertIn("超过任务时限", str(raised.exception))


class TaskRemainingTimeTest(unittest.TestCase):
    def test_context_reports_remaining_deadline(self):
        runner = AccountTaskRunner()
        runner.register(
            AccountTaskSpec(
                kind="probe",
                timeout_seconds=42.0,
                run=lambda ctx, task: {"remaining": ctx.remaining_seconds()} or {},
            )
        )
        submitted = runner.submit("probe", payload={})
        for _ in range(200):
            current = runner.get_task(submitted["id"])
            if current and current["status"] in TERMINAL_STATUSES:
                break
            time.sleep(0.01)
        else:
            self.fail("任务未在预期时间内结束")
        self.assertEqual(current["status"], SUCCEEDED)
        remaining = current["summary"]["remaining"]
        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, 42.0)
        self.assertGreater(remaining, 35.0)
        runner.stop()


if __name__ == "__main__":
    unittest.main()
