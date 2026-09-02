import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registration import engine
from backend.registration.artifacts import collect_related_file_paths, delete_related_files
from backend.web import application


class FakePage:
    def __init__(self):
        self.calls = []

    def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        Path(kwargs["path"]).write_bytes(b"fixture-png")


class FailureScreenshotTests(unittest.TestCase):
    def test_capture_failure_screenshot_uses_data_directory(self):
        page = FakePage()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "DATA_DIR", tmp), mock.patch.object(
            engine, "_active_page", return_value=page
        ):
            result = engine.capture_failure_screenshot(
                batch_id="web-batch",
                worker_id=1,
                email="fixture@example.com",
                failure_type=engine.FAIL_BROWSER,
            )

            path = Path(result)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, Path(tmp) / "screenshots" / "registration-failures")
            self.assertIn("fixture@example.com", path.name)
            self.assertEqual(page.calls[0]["full_page"], True)

    def test_capture_failure_screenshot_skips_missing_page(self):
        with mock.patch.object(engine, "_active_page", return_value=None):
            self.assertEqual(engine.capture_failure_screenshot(batch_id="fixture"), "")

    def test_serialized_record_exposes_protected_screenshot_url(self):
        item = application._serialize_record({"id": 42, "screenshot_path": "/app/data/screenshots/failure.png"})
        self.assertEqual(item["screenshot_url"], "/api/accounts/42/failure-screenshot")

    def test_current_exception_traceback_returns_active_exception_only(self):
        self.assertEqual(engine.current_exception_traceback(), "")
        try:
            raise RuntimeError("fixture failure")
        except RuntimeError:
            result = engine.current_exception_traceback()
        self.assertIn("Traceback (most recent call last)", result)
        self.assertIn("RuntimeError: fixture failure", result)

    def test_current_exception_traceback_truncates_and_keeps_exception_type(self):
        try:
            raise RuntimeError("x" * 4_000)
        except RuntimeError:
            result = engine.current_exception_traceback(1_000)
        self.assertIn("异常堆栈过长，已截断", result)
        self.assertTrue(result.endswith("x" * 100))

    def test_current_exception_traceback_preserves_original_exception_message(self):
        message = "Authorization: Bearer fixture-token password=fixture-password"
        try:
            raise RuntimeError(message)
        except RuntimeError:
            result = engine.current_exception_traceback()
        self.assertIn(message, result)

    def test_serialized_record_exposes_exception_traceback(self):
        item = application._serialize_record(
            {
                "id": 43,
                "extra_json": '{"exception_traceback": "Traceback fixture", "exception_type": "RuntimeError: fixture"}',
            }
        )
        self.assertEqual(item["exception_traceback"], "Traceback fixture")
        self.assertEqual(item["exception_type"], "RuntimeError: fixture")
        self.assertTrue(item["has_exception_traceback"])

    def test_screenshot_reader_restricts_files_to_failure_directory(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(application, "DATA_DIR", Path(tmp)):
            root = Path(tmp) / "screenshots" / "registration-failures"
            root.mkdir(parents=True)
            image = root / "failure.png"
            image.write_bytes(b"fixture-png")
            resolved, media_type = application._failure_screenshot_file(
                {"screenshot_path": str(image)}
            )
            self.assertEqual(resolved, image.resolve())
            self.assertEqual(media_type, "image/png")

            outside = Path(tmp) / "outside.png"
            outside.write_bytes(b"fixture-png")
            with self.assertRaises(FileNotFoundError):
                application._failure_screenshot_file({"screenshot_path": str(outside)})

    def test_screenshot_is_collected_and_deleted_with_account_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accounts = root / "accounts"
            accounts.mkdir()
            screenshot = root / "screenshots" / "registration-failures" / "failure.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(b"fixture-png")
            paths = collect_related_file_paths(
                {"screenshot_path": str(screenshot)},
                accounts_dir=str(accounts),
                app_dir=str(root),
            )
            self.assertEqual(paths, [str(screenshot.resolve())])
            deleted, errors = delete_related_files(paths)
            self.assertEqual(deleted, [str(screenshot.resolve())])
            self.assertEqual(errors, [])
            self.assertFalse(screenshot.exists())


if __name__ == "__main__":
    unittest.main()
