import time
import unittest
from unittest import mock

from backend.automation import turnstile


class TurnstileClickTests(unittest.TestCase):
    def _runtime(self):
        frame = mock.Mock()
        frame.url = "https://challenges.cloudflare.com/turnstile/frame"
        frame.evaluate.return_value = {"w": 300, "h": 65}
        raw_page = mock.Mock(frames=[frame])
        return frame, raw_page

    def test_body_coordinate_click_forces_empty_frame_body(self):
        frame, raw_page = self._runtime()
        runtime_page = mock.Mock(raw_page=raw_page)

        with mock.patch.object(turnstile, "page", runtime_page):
            turnstile._try_click_turnstile_frame()

        frame.locator.assert_called_once_with("body")
        # 超时故意短：上游挑战页会反复重建 iframe，快速失败才能尽快重新定位
        frame.locator.return_value.click.assert_called_once_with(
            position={"x": 24, "y": 32.5},
            force=True,
            timeout=2000,
        )
        frame.frame_element.assert_not_called()

    def test_frame_element_fallback_does_not_depend_on_src_attribute(self):
        frame, raw_page = self._runtime()
        frame.locator.return_value.click.side_effect = RuntimeError("not actionable")
        iframe = frame.frame_element.return_value
        iframe.bounding_box.return_value = {"x": 100, "y": 200, "width": 300, "height": 65}
        runtime_page = mock.Mock(raw_page=raw_page)

        with mock.patch.object(turnstile, "page", runtime_page):
            turnstile._try_click_turnstile_frame()

        iframe.click.assert_called_once_with(
            position={"x": 24, "y": 32.5},
            force=True,
            timeout=2000,
        )
        raw_page.query_selector.assert_not_called()

    def test_page_level_fallback_is_time_bounded(self):
        frame, raw_page = self._runtime()
        frame.locator.return_value.click.side_effect = RuntimeError("frame body stuck")
        iframe = frame.frame_element.return_value
        iframe.bounding_box.return_value = {"x": 100, "y": 200, "width": 300, "height": 65}
        iframe.click.side_effect = RuntimeError("frame element stuck")
        runtime_page = mock.Mock(raw_page=raw_page)

        with mock.patch.object(turnstile, "page", runtime_page):
            turnstile._try_click_turnstile_frame()

        page_click = raw_page.locator.return_value.click
        self.assertTrue(page_click.called)
        self.assertEqual(page_click.call_args.kwargs.get("timeout"), 2000)
        self.assertTrue(page_click.call_args.kwargs.get("force"))
        # 无限等待的 mouse API 会吞掉整个有界预算，还要靠杀浏览器才能打断。
        raw_page.mouse.click.assert_not_called()

    def test_progress_watchdog_uses_a_short_fuse_and_is_disarmed_on_success(self):
        """浏览器卡住时按“有没有成功轮次”回收，不能等满预算。"""
        armed = []
        frame, raw_page = self._runtime()
        frame.locator.return_value.click.side_effect = RuntimeError("stuck")
        iframe = frame.frame_element.return_value
        iframe.bounding_box.return_value = {"x": 1, "y": 2, "width": 300, "height": 65}
        iframe.click.side_effect = RuntimeError("stuck")
        runtime_page = mock.Mock(raw_page=raw_page)
        runtime_page.run_js.side_effect = ["", "", "x" * 120]

        def fake_arm(seconds, profile_dir, log_callback=None):
            handle = mock.Mock()
            armed.append((seconds, handle))
            return handle

        with mock.patch.object(turnstile, "active_page", lambda: runtime_page), mock.patch.object(
            turnstile, "page", runtime_page
        ), mock.patch.object(
            turnstile, "current_profile_dir", lambda: "/tmp/fake-profile"
        ), mock.patch.object(
            turnstile, "arm_browser_watchdog", fake_arm
        ):
            token = turnstile._poll_turnstile_token(
                log_callback=None,
                cancel_callback=None,
                deadline=time.monotonic() + 30.0,
                budget_seconds=30.0,
            )

        self.assertEqual(token, "x" * 120)
        self.assertTrue(armed)
        self.assertTrue(all(seconds <= 20.0 for seconds, _ in armed), armed)
        self.assertTrue(all(handle.call_count >= 1 for _, handle in armed), "成功路径必须解除回收定时器")

    def test_click_path_uses_no_unbounded_api(self):
        import inspect

        source = inspect.getsource(turnstile)
        for banned in ("mouse.click(", "mouse.down(", "mouse.up("):
            self.assertNotIn(banned, source, f"点击路径不应出现无超时的 {banned}")


    def test_stuck_widget_exits_at_budget_without_killing_the_browser(self):
        """三种点击全部卡住时，轮询应在预算内退出，而不是靠杀浏览器。"""
        import time

        frame = mock.Mock()
        frame.url = "https://challenges.cloudflare.com/turnstile/frame"
        frame.evaluate.return_value = {"w": 300, "h": 65}
        frame.locator.return_value.click.side_effect = RuntimeError("Timeout 2000ms exceeded.")
        iframe = frame.frame_element.return_value
        iframe.bounding_box.return_value = {"x": 100, "y": 200, "width": 300, "height": 65}
        iframe.click.side_effect = RuntimeError("Timeout 2000ms exceeded.")
        raw_page = mock.Mock(frames=[frame])
        raw_page.locator.return_value.click.side_effect = RuntimeError("Timeout 2000ms exceeded.")
        runtime_page = mock.Mock(raw_page=raw_page)
        runtime_page.run_js.return_value = ""

        t0 = time.monotonic()
        with mock.patch.object(turnstile, "active_page", lambda: runtime_page), mock.patch.object(
            turnstile, "page", runtime_page
        ):
            with self.assertRaises(Exception) as raised:
                turnstile._poll_turnstile_token(
                    log_callback=None,
                    cancel_callback=None,
                    deadline=time.monotonic() + 6.0,
                    budget_seconds=6.0,
                )
        spent = time.monotonic() - t0
        self.assertLess(spent, 12.0, f"预算 6s 的轮询实际花了 {spent:.1f}s")
        self.assertIn("人工交互挑战", str(raised.exception))
        raw_page.mouse.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
