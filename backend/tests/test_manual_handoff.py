"""manual-handoff 瘦身回归测试（Profile 单业务模型）。

覆盖：消费账本边界（profile_id 作用域）、取消语义、凭据导出、
运行时代码残留扫描。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registration import engine as gr
from backend.web import account_exports


class ConsumptionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)
        gr.config.update({"outlookemail_source": "accounts"})

    def tearDown(self):
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_mark_mailbox_consumed_persists_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gr, "RESULTS_DB_FILE", str(Path(tmp) / "results.sqlite3")), \
                 mock.patch.object(gr, "_repository", None):
                # acquire 冻结 Profile 作用域与来源（真实路径由 engine.acquire_email 完成）
                gr._freeze_profile_id("user@outlook.com", 1)
                gr._freeze_mailbox_source("user@outlook.com", "accounts")
                try:
                    logs = []
                    self.assertTrue(
                        gr.mark_mailbox_consumed(
                            "user@outlook.com",
                            batch_id="batch-1",
                            reason="已提交注册",
                            log_callback=logs.append,
                        )
                    )
                    # 幂等
                    self.assertFalse(
                        gr.mark_mailbox_consumed("user@outlook.com", batch_id="batch-1")
                    )
                    repo = gr.get_registration_repository()
                    self.assertTrue(repo.is_mailbox_consumed(1, "accounts", "user@outlook.com"))
                    self.assertIn("consumed", "\n".join(logs))

                    # acquire 过滤回调必须命中账本（同 Profile 作用域）
                    self.assertTrue(
                        gr.email_registered_successfully("user@outlook.com", profile_id=1)
                    )
                    # 其它 Profile 作用域不受影响
                    self.assertFalse(
                        gr.email_registered_successfully("user@outlook.com", profile_id=2)
                    )
                finally:
                    gr._forget_attempt_context("user@outlook.com")

    def test_ledger_failure_is_fail_closed(self):
        """账本查询异常（UNKNOWN）必须阻断 acquire，绝不能当作可用邮箱放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gr, "RESULTS_DB_FILE", str(Path(tmp) / "results.sqlite3")), \
                 mock.patch.object(gr, "_repository", None), \
                 mock.patch.object(
                    type(gr.get_registration_repository()),
                    "is_mailbox_consumed_any_source",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                with self.assertRaises(sqlite3.OperationalError):
                    gr.email_registered_successfully("blocked@outlook.com", profile_id=1)

    def test_legacy_fallback_failure_is_fail_closed(self):
        """历史兼容查询（has_success/has_registered_or_consumed）异常同样不得降级为可用。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gr, "RESULTS_DB_FILE", str(Path(tmp) / "results.sqlite3")), \
                 mock.patch.object(gr, "_repository", None), \
                 mock.patch.object(
                    type(gr.get_registration_repository()),
                    "has_registered_or_consumed",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                with self.assertRaises(sqlite3.OperationalError):
                    gr.email_registered_successfully("legacy@outlook.com", profile_id=1)

    def test_unavailable_callback_failure_blocks_acquire(self):
        """is_unavailable 回调抛异常时 acquire 必须响亮失败，不得返回邮箱。"""
        def boom(email):
            raise sqlite3.OperationalError("database is locked")

        accounts = [{"email": "a@outlook.com", "status": "active"}]
        with mock.patch.object(
            gr.outlookemail_provider, "get_accounts", return_value=accounts
        ):
            with self.assertRaises(sqlite3.OperationalError):
                gr.outlookemail_provider.acquire_email(
                    http_get=lambda *a, **k: None,
                    session_factory=lambda: None,
                    api_base="http://x",
                    api_key="k",
                    source="accounts",
                    is_unavailable=boom,
                )

    def test_cancelled_before_submit_releases_reservation(self):
        with mock.patch.object(gr.outlookemail_provider, "release_email") as release:
            gr.handle_cancelled_email("pending@outlook.com", submitted=False)
            release.assert_called_once_with("pending@outlook.com")

    def test_cancelled_after_submit_keeps_consumed_without_remote_disable(self):
        """提交后取消：不 remote disable、不 release，账本已在提交时写入。"""
        with mock.patch.object(gr.outlookemail_provider, "release_email") as release:
            gr.handle_cancelled_email("used@outlook.com", submitted=True)
            release.assert_not_called()
        # 远程停用能力已整体删除
        self.assertFalse(hasattr(gr.outlookemail_provider, "disable_account"))

    def test_no_disable_machinery_remains_on_engine(self):
        """远程自动停用能力必须整体消失，而不是留一个开关。"""
        self.assertFalse(hasattr(gr, "disable_outlookemail_consumed"))
        self.assertFalse(hasattr(gr.outlookemail_provider, "disable_account"))
        self.assertFalse(hasattr(gr.outlookemail_provider, "consume"))
        self.assertFalse(hasattr(gr, "ensure_sso_oauth_eligible"))
        self.assertFalse(hasattr(gr, "add_sso_to_cpa"))
        self.assertFalse(hasattr(gr, "retry_pending_cpa_deliveries"))
        self.assertFalse(hasattr(gr, "RegistrationRiskDenied"))
        self.assertNotIn("cpa_auto_add", gr.DEFAULT_CONFIG)
        self.assertNotIn("cpa_remote_url", gr.DEFAULT_CONFIG)

    def test_frozen_mailbox_source_survives_config_flip(self):
        """acquire 时冻结来源：任务运行中改 Settings 不影响账本写入的 source。"""
        gr._forget_attempt_context("frozen@outlook.com")
        try:
            gr._freeze_mailbox_source("Frozen@Outlook.com", "accounts")
            self.assertEqual(gr._frozen_mailbox_source("frozen@outlook.com"), "accounts")
            # 运行中把全局配置切到 temp，冻结值不变
            gr.config["outlookemail_source"] = "temp"
            self.assertEqual(gr._frozen_mailbox_source("frozen@outlook.com"), "accounts")
            # 未冻结邮箱回退到当前配置
            self.assertEqual(gr._frozen_mailbox_source("other@outlook.com"), "temp")
        finally:
            gr.config["outlookemail_source"] = "accounts"
            gr._forget_attempt_context("frozen@outlook.com")
        self.assertEqual(gr._frozen_mailbox_source("frozen@outlook.com"), "accounts")  # 回退生效

    def test_mark_uses_frozen_source_not_current_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gr, "RESULTS_DB_FILE", str(Path(tmp) / "results.sqlite3")), \
                 mock.patch.object(gr, "_repository", None):
                gr._forget_attempt_context("switch@outlook.com")
                gr._freeze_profile_id("switch@outlook.com", 1)
                gr._freeze_mailbox_source("switch@outlook.com", "accounts")
                try:
                    gr.config["outlookemail_source"] = "temp"
                    gr.mark_mailbox_consumed("switch@outlook.com", batch_id="b9", reason="冻结测试")
                    repo = gr.get_registration_repository()
                    # 账本写入 acquire 时冻结的 accounts，而非当前 temp
                    self.assertTrue(repo.is_mailbox_consumed(1, "accounts", "switch@outlook.com"))
                    self.assertFalse(repo.is_mailbox_consumed(1, "temp", "switch@outlook.com"))
                finally:
                    gr.config["outlookemail_source"] = "accounts"
                    gr._forget_attempt_context("switch@outlook.com")

    def test_mark_without_frozen_profile_fails_closed(self):
        """无冻结 Profile 作用域 → 拒绝写账本（绝不猜一个作用域）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gr, "RESULTS_DB_FILE", str(Path(tmp) / "results.sqlite3")), \
                 mock.patch.object(gr, "_repository", None):
                gr._forget_attempt_context("orphan@outlook.com")
                with self.assertRaises(Exception) as raised:
                    gr.mark_mailbox_consumed("orphan@outlook.com", batch_id="b1")
                self.assertIn("Profile 作用域", str(raised.exception))


class ManualHandoffExportTests(unittest.TestCase):
    """凭据导出：只导 success，格式 email----password（单业务无 SSO）。"""

    def test_credentials_text_success_only(self):
        records = [
            {"email": "a@outlook.com", "password": "PwA1", "registration_status": "success"},
            {"email": "b@outlook.com", "password": "PwB2", "registration_status": "success"},
            {"email": "c@outlook.com", "password": "PwC3", "registration_status": "failure"},
        ]
        blob, exported = account_exports.build_credentials_text(records)
        self.assertEqual(exported, 2)
        lines = blob.decode("utf-8").strip().splitlines()
        self.assertEqual(lines, ["a@outlook.com----PwA1", "b@outlook.com----PwB2"])
        self.assertNotIn("PwC3", blob.decode("utf-8"))

    def test_credentials_text_empty_for_no_success(self):
        blob, exported = account_exports.build_credentials_text(
            [{"email": "x@outlook.com", "password": "p", "registration_status": "failure"}]
        )
        self.assertEqual(exported, 0)
        self.assertEqual(blob, b"")


class ResidualReferenceTests(unittest.TestCase):
    """运行时代码不得残留已删除链路的引用（schema 历史列与 wiki 白名单除外）。"""

    ALLOWED_FILES = {
        "backend/registration/store.py",  # 旧 schema 说明注释
        "backend/registration/artifacts.py",  # 历史文件清理名单
    }
    FORBIDDEN_TOKENS = (
        "auth_exchange",
        "add_sso_to_cpa",
        "retry_pending_cpa",
        "ensure_sso_oauth_eligible",
        "RegistrationRiskDenied",
        "botFlagSource",
        "cpa_remote_url",
        "cpa_management_key",
        "disable_outlookemail_consumed",
        "authorize_device_in_browser",
        "retry-delivery",
        "response_error_detail",
        "seed_session_cookie",
        "_reserved_accounts",
        # 消费边界 fail-open 模式禁止回归（UNKNOWN ≠ AVAILABLE）
        "继续使用当前邮箱",
    )

    def test_no_runtime_residue(self):
        backend_root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in backend_root.rglob("*.py"):
            rel = path.relative_to(backend_root.parent).as_posix()
            if rel in self.ALLOWED_FILES or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for token in self.FORBIDDEN_TOKENS:
                if token in text:
                    offenders.append(f"{rel}: {token}")
        self.assertEqual(offenders, [])

    def test_business_key_scope_is_gone(self):
        """单业务模型：运行时代码不再存在 business_key 作用域。"""
        backend_root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in backend_root.rglob("*.py"):
            rel = path.relative_to(backend_root.parent).as_posix()
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "business_key" in text:
                offenders.append(rel)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
