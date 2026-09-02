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
        frame.locator.return_value.click.assert_called_once_with(
            position={"x": 24, "y": 32.5},
            force=True,
            timeout=3000,
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
            timeout=3000,
        )
        raw_page.query_selector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
