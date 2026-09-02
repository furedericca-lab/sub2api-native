import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "sync-outlookemail-runtime-password.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("sync_outlookemail_runtime_password", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyncOutlookEmailRuntimePasswordTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.runtime_path = Path(self.tempdir.name) / "runtime.env"
        self.runtime_path.write_text(
            "LOGIN_PASSWORD=old-password\nSECRET_KEY=secret-key\n",
            encoding="utf-8",
        )
        os.chmod(self.runtime_path, 0o600)

    def test_sync_validates_over_http_before_atomic_runtime_update(self):
        with mock.patch.object(self.module, "verify_password", return_value=True) as verified:
            result = self.module.sync_runtime_password(
                self.runtime_path,
                "http://127.0.0.1:5000",
                "new-password",
            )
        self.assertTrue(result)
        verified.assert_called_once_with("http://127.0.0.1:5000", "new-password")
        values = self.module.read_runtime_env(self.runtime_path)
        self.assertEqual(values["LOGIN_PASSWORD"], "new-password")
        self.assertEqual(values["SECRET_KEY"], "secret-key")
        self.assertEqual(self.runtime_path.stat().st_mode & 0o777, 0o600)

    def test_sync_leaves_runtime_file_unchanged_when_validation_fails(self):
        original = self.runtime_path.read_text(encoding="utf-8")
        with mock.patch.object(self.module, "verify_password", return_value=False):
            result = self.module.sync_runtime_password(
                self.runtime_path,
                "http://127.0.0.1:5000",
                "wrong-password",
            )
        self.assertFalse(result)
        self.assertEqual(self.runtime_path.read_text(encoding="utf-8"), original)
