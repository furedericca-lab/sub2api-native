"""Profile 作用域隔离回归（单业务模型）。

覆盖：
- 凭据 TXT 导出：只导出 success 记录，email----password 格式；
- 删除 Profile A 记录（含文件清理）不触碰 Profile B 的同邮箱账本行；
- collect_related_file_paths 按记录收集产物（单业务无跨业务泄漏概念）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registration.store import RegistrationRepository


def _make_client(store: RegistrationRepository, tmp_root: Path):
    from backend.web import application
    from fastapi.testclient import TestClient

    gr_mock = mock.Mock()
    gr_mock.get_registration_repository.return_value = store
    gr_mock.ACCOUNTS_DIR = str(tmp_root / "accounts")
    gr_mock.DATA_DIR = str(tmp_root / "data")
    return application, gr_mock, TestClient(application.create_app())


class ProfileScopeIsolationTests(unittest.TestCase):
    """同邮箱在不同 Profile 作用域在 Web/文件/导出层的隔离。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = RegistrationRepository(self.root / "results.sqlite3")
        self.application, self.gr_mock, self.client = _make_client(
            self.store, self.root
        )
        self._gr_patcher = mock.patch.object(self.application, "_gr", return_value=self.gr_mock)
        self._sess_patcher = mock.patch.object(self.application, "_valid_session", return_value=True)
        self._gr_patcher.start()
        self._sess_patcher.start()
        self.addCleanup(self._gr_patcher.stop)
        self.addCleanup(self._sess_patcher.stop)
        # DATA_DIR 决定账号文件的按邮箱回退路径
        self._data_patcher = mock.patch.object(
            self.application, "DATA_DIR", self.root / "data"
        )
        self._data_patcher.start()
        self.addCleanup(self._data_patcher.stop)
        (self.root / "data" / "accounts").mkdir(parents=True, exist_ok=True)

    def _create_profile(self, name="Site A", url="https://a.example/register") -> int:
        site_key = "ctai" if "b.example" in url else "true-sota"
        created = self.client.post(
            "/api/sub2api/profiles", json={"name": name, "site_key": site_key}
        )
        self.assertEqual(created.status_code, 200)
        return created.json()["profile"]["id"]

    # ---- 导出 ----

    def test_credentials_export_only_success_records(self):
        profile_id = self._create_profile()
        ok_id = self.store.add_result(
            {
                "profile_id": profile_id,
                "email": "s0@x.com",
                "password": "SubPass0",
                "status": "success",
            }
        )
        fail_id = self.store.add_result(
            {
                "profile_id": profile_id,
                "email": "s1@x.com",
                "password": "SubPass1",
                "status": "failure",
            }
        )
        response = self.client.post(
            "/api/accounts/credentials-txt/download", json={"ids": [ok_id, fail_id]}
        )
        self.assertEqual(response.status_code, 200)
        text = response.text.strip()
        self.assertIn("s0@x.com----SubPass0", text)
        self.assertNotIn("SubPass1", text)

    def test_credentials_export_email_password_format(self):
        profile_id = self._create_profile()
        ids = [
            self.store.add_result(
                {
                    "profile_id": profile_id,
                    "email": f"s{i}@x.com",
                    "password": f"SubPass{i}",
                    "status": "success",
                }
            )
            for i in range(2)
        ]
        response = self.client.post(
            "/api/accounts/credentials-txt/download", json={"ids": ids}
        )
        self.assertEqual(response.status_code, 200)
        text = response.text.strip()
        self.assertIn("s0@x.com----SubPass0", text)
        self.assertIn("s1@x.com----SubPass1", text)

    # ---- 删除文件隔离（跨 Profile 同邮箱） ----

    def test_deleting_profile_a_record_keeps_profile_b_ledger(self):
        profile_a = self._create_profile("Site A", "https://a.example/register")
        profile_b = self._create_profile("Site B", "https://b.example/register")
        email = "same@x.com"
        # 同邮箱在 Profile B 也有消费账本（跨 Profile 隔离验证基准）
        self.store.mark_mailbox_consumed(profile_b, "accounts", email)
        # Profile A 失败记录：允许释放（同作用域无成功记录）
        sub_id = self.store.add_result(
            {
                "profile_id": profile_a,
                "email": email,
                "password": "SubPass1",
                "status": "failure",
            }
        )
        response = self.client.post(
            "/api/accounts/delete",
            json={"ids": [sub_id], "delete_files": True, "release_email": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        # Profile A 账本行已释放
        self.assertFalse(
            self.store.is_mailbox_consumed(profile_a, "accounts", email)
        )
        # Profile B 账本行不受影响（跨 Profile 互不干扰）
        self.assertTrue(self.store.is_mailbox_consumed(profile_b, "accounts", email))

    def test_success_record_delete_refuses_release(self):
        profile_a = self._create_profile("Site A", "https://a.example/register")
        email = "ok@x.com"
        self.store.mark_mailbox_consumed(profile_a, "accounts", email)
        sub_id = self.store.add_result(
            {
                "profile_id": profile_a,
                "email": email,
                "password": "SubPass1",
                "status": "success",
            }
        )
        response = self.client.post(
            "/api/accounts/delete",
            json={"ids": [sub_id], "delete_files": True, "release_email": True},
        )
        # 成功记录 → 释放被拒（409），但记录本身未删
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("成功记录", response.json()["detail"])
        self.assertTrue(
            self.store.get_results_by_ids([sub_id]), "记录应保留（释放被拒）"
        )
        self.assertTrue(self.store.is_mailbox_consumed(profile_a, "accounts", email))

    # ---- collect_related_file_paths ----

    def test_collect_related_paths_gathers_screenshot(self):
        from backend.registration.artifacts import collect_related_file_paths

        shot = self.root / "data" / "shot.png"
        shot.write_bytes(b"png")
        paths = collect_related_file_paths(
            {"email": "gate@x.com", "profile_id": 1, "screenshot_path": str(shot)},
            accounts_dir=str(self.root / "data" / "accounts"),
        )
        self.assertIn(str(shot.resolve()), [p for p in paths])

    def test_collect_related_paths_no_account_file_by_email(self):
        """Sub2API 无按邮箱命名的账号文件：仅邮箱的记录不收集产物。"""
        from backend.registration.artifacts import collect_related_file_paths

        accounts_dir = self.root / "data" / "accounts"
        paths = collect_related_file_paths(
            {"email": "ghost@x.com", "profile_id": 1},
            accounts_dir=str(accounts_dir),
        )
        self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
