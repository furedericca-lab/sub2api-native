import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.mailbox import outlook_pool
from backend.registration import engine
from backend.registration.store import RegistrationRepository


def _fake_accounts():
    """模拟 OutlookEmail 返回：3 active + 1 inactive。"""
    return [
        {"email": "alice@qq.com", "status": "active"},
        {"email": "bob@foxmail.com", "status": "active"},
        {"email": "carol@a.qq.com", "status": "active"},  # a.qq.com != qq.com
        {"email": "dave@google.com", "status": "active"},
        {"email": "erin@qq.com", "status": "inactive"},
    ]


class WhitelistAcquireTests(unittest.TestCase):
    """白名单在池内过滤，精确匹配、大小写不敏感、不烧邮箱。"""

    def setUp(self):
        # 清空调度预留等运行时状态，避免跨测试串扰
        outlook_pool.reset_runtime_state()

    def _acquire(self, **kwargs):
        # sequential = 确定性按池顺序选取（随机模式会让“唯一匹配”类断言变 flaky）
        kwargs.setdefault("pick_mode", "sequential")
        kwargs.setdefault("preflight_messages", False)
        return outlook_pool.acquire_email(
            http_get=mock.Mock(),
            session_factory=mock.Mock(),
            api_base="http://x",
            **kwargs,
        )

    def test_exact_domain_match(self):
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            email, _ = self._acquire(allowed_domains=["qq.com"])
        self.assertEqual(email, "alice@qq.com")
        # 只可能拿到 qq.com（清空调度预留后重取）
        outlook_pool.reset_runtime_state()
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            email2, _ = self._acquire(allowed_domains=["QQ.COM"])
        self.assertEqual(email2, "alice@qq.com")

    def test_subdomain_does_not_match(self):
        """a.qq.com 不匹配白名单 qq.com（精确匹配，无通配）。"""
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            # 白名单同时含 qq.com 与 a.qq.com：两个域名都可取
            email, _ = self._acquire(allowed_domains=["qq.com", "a.qq.com"])
            self.assertIn(email, ("alice@qq.com", "carol@a.qq.com"))
            # 只有 qq.com 时，carol@a.qq.com 被排除（alice 已被本次预留）
            outlook_pool.reset_runtime_state()
            with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
                email2, _ = self._acquire(allowed_domains=["qq.com"])
        self.assertEqual(email2, "alice@qq.com")
        self.assertNotEqual(email2, "carol@a.qq.com")

    def test_empty_whitelist_keeps_unfiltered_behavior(self):
        """空/None 白名单 = 不过滤。"""
        accounts = _fake_accounts()
        with mock.patch.object(outlook_pool, "get_accounts", return_value=accounts):
            email, _ = self._acquire(allowed_domains=None)
            email2, _ = self._acquire(allowed_domains=[])
        self.assertIn(email, ("alice@qq.com", "bob@foxmail.com", "carol@a.qq.com", "dave@google.com"))
        self.assertIn(email2, ("alice@qq.com", "bob@foxmail.com", "carol@a.qq.com", "dave@google.com"))

    def test_no_matching_email_friendly_error(self):
        """无匹配邮箱 → 明确报错（非 generic empty-pool），含 matched/active 计数。"""
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            with self.assertRaises(Exception) as raised:
                self._acquire(allowed_domains=["example.com"])
        message = str(raised.exception)
        self.assertIn("example.com", message)
        self.assertIn("4", message)  # active 数
        self.assertNotIn("邮箱池为空", message)

    def test_mismatched_emails_stay_available(self):
        """非匹配邮箱保持 active、不进入消费/预留：下次可再取。"""
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            email, _ = self._acquire(allowed_domains=["foxmail.com"])
        self.assertEqual(email, "bob@foxmail.com")
        # qq.com 邮箱未被预留（白名单过滤在预留之前）
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            email2, _ = self._acquire(allowed_domains=["qq.com"])
        self.assertEqual(email2, "alice@qq.com")

    def test_domain_filter_applies_before_consumption_check(self):
        """顺序：active → 白名单 → 消费检查。已消费且域名不符的不计入已消耗。"""
        accounts = _fake_accounts()
        with mock.patch.object(outlook_pool, "get_accounts", return_value=accounts):
            # 全部 qq.com 邮箱已消费；foxmail 可用
            email, _ = self._acquire(
                allowed_domains=["qq.com", "foxmail.com"],
                is_unavailable=lambda e: e == "alice@qq.com",
            )
        self.assertEqual(email, "bob@foxmail.com")
        # 只有 qq.com 且已消费 → 报“已消耗”而非“域名不符”
        with mock.patch.object(outlook_pool, "get_accounts", return_value=_fake_accounts()):
            with self.assertRaises(Exception) as raised:
                self._acquire(
                    allowed_domains=["qq.com"],
                    is_unavailable=lambda e: e == "alice@qq.com",
                )
        self.assertIn("已消耗", str(raised.exception))


class Sub2apiAcquireWiringTests(unittest.TestCase):
    """engine Profile 感知获取接线（白名单 + profile_id 冻结）。"""

    def _store(self, tmp):
        return RegistrationRepository(Path(tmp) / "results.sqlite3")

    def test_acquire_freezes_scope_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ), mock.patch.object(
                engine, "get_outlookemail_source", return_value="temp"
            ), mock.patch.object(
                engine.outlookemail_provider, "acquire", return_value=("new@qq.com", "tok")
            ) as acquire_mock, mock.patch.object(
                engine, "get_outlookemail_api_base", return_value="http://x"
            ), mock.patch.object(
                engine, "get_outlookemail_api_key", return_value="k"
            ):
                email, _ = engine.acquire_email(
                    {"id": 3, "name": "P", "enabled": True, "whitelist": ["qq.com"]}
                )
            self.assertEqual(email, "new@qq.com")
            # 白名单与 Profile 作用域的消费判定都被传入
            # （is_unavailable 会访问仓库：必须在 mock 块内调用，不碰真实 data/ 库）
            kwargs = acquire_mock.call_args.kwargs
            self.assertEqual(kwargs["allowed_domains"], ["qq.com"])
            self.assertTrue(kwargs["preflight_messages"])
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ):
                self.assertTrue(kwargs["is_unavailable"]("new@qq.com") is False)
            self.assertEqual(engine._frozen_profile_id("new@qq.com"), 3)
            self.assertEqual(engine._frozen_mailbox_source("new@qq.com"), "temp")
            snapshot = engine.frozen_profile_snapshot("new@qq.com")
            self.assertEqual(snapshot["profile_id"], 3)
            self.assertTrue(snapshot["promo_configured"] is False)
            engine._forget_attempt_context("new@qq.com")

    def test_acquire_disabled_profile_refused(self):
        with self.assertRaises(Exception) as raised:
            engine.acquire_email({"id": 5, "enabled": False})
        self.assertIn("禁用", str(raised.exception))

    def test_acquire_missing_profile_refused(self):
        with self.assertRaises(Exception):
            engine.acquire_email({})

    def test_consumption_scoped_to_frozen_profile(self):
        """mark 使用冻结作用域：Profile 1 消费过的同邮箱在 Profile 2 仍可用。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ), mock.patch.object(
                engine, "get_outlookemail_source", return_value="accounts"
            ), mock.patch.object(
                engine.outlookemail_provider, "acquire", return_value=("same@qq.com", "tok")
            ), mock.patch.object(
                engine, "get_outlookemail_api_base", return_value="http://x"
            ), mock.patch.object(
                engine, "get_outlookemail_api_key", return_value="k"
            ):
                engine.acquire_email(
                    {"id": 1, "name": "P", "enabled": True, "whitelist": []}
                )
                # 冻结作用域 = Profile 1
                self.assertEqual(engine._frozen_profile_id("same@qq.com"), 1)
                # 冻结作用域标记消费（全部仓库访问在 mock 块内，不碰真实 DB）
                self.assertTrue(engine.mark_mailbox_consumed("same@qq.com"))
                self.assertTrue(
                    store.is_mailbox_consumed_any_source("same@qq.com", profile_id=1)
                )
                # 其它 Profile 作用域无该邮箱消费
                self.assertFalse(
                    store.is_mailbox_consumed_any_source("same@qq.com", profile_id=2)
                )
                # 同作用域重复标记 → False
                self.assertFalse(engine.mark_mailbox_consumed("same@qq.com"))
            engine._forget_attempt_context("same@qq.com")

    def test_consuming_one_alias_does_not_consume_another_full_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertTrue(
                store.mark_mailbox_consumed(
                    1, "accounts", "alias-a@qq.com", reason="accepted_submit"
                )
            )
            self.assertTrue(
                store.is_mailbox_consumed_any_source("alias-a@qq.com", profile_id=1)
            )
            self.assertFalse(
                store.is_mailbox_consumed_any_source(
                    "alias-b@foxmail.com", profile_id=1
                )
            )


if __name__ == "__main__":
    unittest.main()
