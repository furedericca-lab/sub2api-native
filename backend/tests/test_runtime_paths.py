import unittest
from pathlib import Path

from backend.web import application
from backend.registration import engine as gr


class RuntimeLayoutTests(unittest.TestCase):
    def test_runtime_data_is_separate_from_front_and_backend(self):
        root = Path(__file__).resolve().parents[2]

        self.assertEqual(Path(gr.APP_DIR).resolve(), root)
        self.assertEqual(Path(gr.DATA_DIR).resolve(), root / "data")
        self.assertEqual(Path(gr.ACCOUNTS_DIR).resolve(), root / "data" / "accounts")
        self.assertEqual(
            (Path(gr.DATA_DIR) / "screenshots" / "registration-failures").resolve(),
            root / "data" / "screenshots" / "registration-failures",
        )
        self.assertEqual(application.WEB_AUTH_FILE.resolve(), root / "data" / "web_auth.json")
        self.assertEqual(application.STATIC_DIR.resolve(), root / "front" / "dist")


if __name__ == "__main__":
    unittest.main()
