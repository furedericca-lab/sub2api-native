import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.web import application as web_app
from backend.web.application import _create_auth_record, _sign_session, _valid_session


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.auth_file = tempfile.NamedTemporaryFile(delete=False)
        self.auth_file.close()
        self.original_auth_file = web_app.WEB_AUTH_FILE
        web_app.WEB_AUTH_FILE = web_app.Path(self.auth_file.name)

    def tearDown(self):
        web_app.WEB_AUTH_FILE = self.original_auth_file
        try:
            os.unlink(self.auth_file.name)
        except FileNotFoundError:
            pass

    def test_signed_session_validates_and_rejects_tampering(self):
        expires = int(time.time()) + 60
        record = _create_auth_record("admin", "password")
        web_app._save_auth_record(record)
        token = _sign_session("admin", expires, record["session_secret"])
        self.assertTrue(_valid_session(token))
        self.assertFalse(_valid_session(token[:-1] + ("0" if token[-1] != "0" else "1")))
        self.assertFalse(_valid_session(_sign_session("other", expires, record["session_secret"])))

    def test_expired_session_is_rejected(self):
        record = _create_auth_record("admin", "password")
        web_app._save_auth_record(record)
        token = _sign_session("admin", int(time.time()) - 1, record["session_secret"])
        self.assertFalse(_valid_session(token))


class FreshInstallAuthHttpTests(unittest.TestCase):
    """fresh-install 管理初始化 HTTP 路径回归。

    conftest 的 autouse fixture 默认假设“管理员已存在”（让其余 API 测试
    hermetic）；本类显式覆盖该假设，直接验证未初始化时的 401 setup_required
    与 /api/auth/setup → 受保护路径放行的完整链路。
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        # 全新安装：auth 文件不存在（不预写任何内容）
        self.auth_file = Path(self._tmp_dir.name) / "web_auth.json"
        self.original_auth_file = web_app.WEB_AUTH_FILE
        web_app.WEB_AUTH_FILE = self.auth_file
        self.addCleanup(
            lambda: setattr(web_app, "WEB_AUTH_FILE", self.original_auth_file)
        )

        from backend.registration.store import RegistrationRepository
        from fastapi.testclient import TestClient

        self.store = RegistrationRepository(Path(self._tmp_dir.name) / "r.sqlite3")
        gr_mock = mock.Mock()
        gr_mock.get_registration_repository.return_value = self.store
        self._gr_patcher = mock.patch.object(web_app, "_gr", return_value=gr_mock)
        self._gr_patcher.start()
        self.addCleanup(self._gr_patcher.stop)
        # 与本地 Docker 部署一致（deploy/.env 设 0）：http 下 session cookie 不强制
        # Secure，否则 TestClient 的 http 请求不会回传该 cookie。
        self._secure_patcher = mock.patch.dict(
            os.environ, {"SUB2API_WEB_COOKIE_SECURE": "0"}
        )
        self._secure_patcher.start()
        self.addCleanup(self._secure_patcher.stop)
        self.client = TestClient(web_app.create_app())

    def _admin_gate_off(self):
        """覆盖 conftest 的“管理员已存在”假设：模拟 fresh install。"""
        return mock.patch.object(web_app, "_web_auth_enabled", return_value=False)

    def test_fresh_install_protected_api_returns_401_setup_required(self):
        with self._admin_gate_off():
            resp = self.client.get("/api/sub2api/profiles")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertTrue(body["setup_required"])
        self.assertTrue(body["auth_required"])

    def test_setup_creates_admin_then_protected_api_is_open(self):
        # 1) 未初始化：受保护 API 401 setup_required
        with self._admin_gate_off():
            fresh = self.client.get("/api/sub2api/profiles")
        self.assertEqual(fresh.status_code, 401)
        self.assertTrue(fresh.json()["setup_required"])

        # 2) POST /api/auth/setup 创建管理员（写真实临时 auth 文件）
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "sup3r-secret", "confirm_password": "sup3r-secret"},
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        self.assertTrue(self.auth_file.is_file())
        # 真实 _web_auth_enabled（非 patch）现在应为 True
        self.assertTrue(web_app._web_auth_enabled())

        # 3) setup 签发的 session cookie 使受保护 API 放行
        listed = self.client.get("/api/sub2api/profiles")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertIn("profiles", listed.json())

        # 4) 重复 setup → 409
        again = self.client.post(
            "/api/auth/setup",
            json={"username": "other", "password": "sup3r-secret", "confirm_password": "sup3r-secret"},
        )
        self.assertEqual(again.status_code, 409)


if __name__ == "__main__":
    unittest.main()
