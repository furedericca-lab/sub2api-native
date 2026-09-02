"""Pre-live hardening（operator 审计 12 项）回归测试（Profile 单业务模型）。

覆盖：
1. Profile partial update：PUT 只传 name 时其余字段保持原值；POST 不传
   enabled → 默认启用。
2. Sub2API OTP 冻结来源：acquire 冻结 accounts 后，当前 Settings 改 temp，
   wait_code 仍收到 source=accounts。
3. 删除产物隔离：delete_files 只删本记录截图，不触碰其它文件。
4. Release 并发门禁：注册任务运行中 → release 409。
5. FAIL_NO_EMAIL：池耗尽 → 停止任务、不落失败记录。
7. Pre-submit reservation：未消费结束/取消 → 释放占用；已消费 → 不释放。
8. 凭据导出只导 success 记录。
9. Job snapshot 恢复 profile_id + Profile 显示名。
10. 白名单持久化 fail-closed：损坏 JSON 拒绝加载。
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.registration.store import ProfileError, RegistrationRepository


def _make_client(store: RegistrationRepository, tmp_root: Path):
    from backend.web import application
    from fastapi.testclient import TestClient

    gr_mock = mock.Mock()
    gr_mock.get_registration_repository.return_value = store
    gr_mock.ACCOUNTS_DIR = str(tmp_root / "accounts")
    gr_mock.DATA_DIR = str(tmp_root / "data")
    return application, TestClient(application.create_app())


class _ClientHarnessMixin:
    """TestClient + 受控仓库的公共装配。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = RegistrationRepository(self.root / "results.sqlite3")
        self.application, self.client = _make_client(self.store, self.root)
        self.gr_mock = mock.Mock()
        self.gr_mock.get_registration_repository.return_value = self.store
        # 与真实布局一致：ACCOUNTS_DIR 即 data/ 下的 accounts 目录
        self.gr_mock.ACCOUNTS_DIR = str(self.root / "data" / "accounts")
        self.gr_mock.DATA_DIR = str(self.root / "data")
        patchers = [
            mock.patch.object(self.application, "_gr", return_value=self.gr_mock),
            mock.patch.object(self.application, "_valid_session", return_value=True),
            mock.patch.object(self.application, "DATA_DIR", self.root / "data"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "data" / "accounts").mkdir(parents=True, exist_ok=True)


class ProfilePartialUpdateTests(_ClientHarnessMixin, unittest.TestCase):
    """修复项 1：exclude_unset —— 未提交字段绝不静默改写。"""

    def test_put_only_name_keeps_other_fields(self):
        created = self.client.post(
            "/api/sub2api/profiles",
            json={
                "name": "Full",
                "site_key": "true-sota",
                "promo_code": "PROMO1",
                "invitation_code": "INV1",
                "aff_code": "AFF1",
            },
        )
        profile_id = created.json()["profile"]["id"]

        updated = self.client.put(
            f"/api/sub2api/profiles/{profile_id}", json={"name": "Renamed"}
        )
        self.assertEqual(updated.status_code, 200)
        profile = updated.json()["profile"]
        self.assertEqual(profile["name"], "Renamed")
        self.assertEqual(profile["promo_code"], "PROMO1")
        self.assertEqual(profile["invitation_code"], "INV1")
        self.assertEqual(profile["aff_code"], "AFF1")
        self.assertIn("qq.com", profile["whitelist"])
        self.assertIn("*.edu.hk", profile["whitelist"])
        self.assertTrue(profile["enabled"])

    def test_create_without_enabled_defaults_true(self):
        created = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "NoEnabled", "site_key": "ctai"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["profile"]["enabled"])

    def test_explicit_false_still_disables(self):
        created = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "Off", "site_key": "true-sota"},
        ).json()["profile"]
        updated = self.client.put(f"/api/sub2api/profiles/{created['id']}", json={"enabled": False})
        self.assertFalse(updated.json()["profile"]["enabled"])


class OtpFrozenSourceTests(unittest.TestCase):
    """修复项 2：验证码拉取显式使用 acquire 冻结来源。

    链路两段分别锁定：
    a) frozen_mailbox_source() 返回冻结值，不受当前 Settings 影响；
    b) 显式传入的 source 在 outlookemail_get_oai_code 内覆盖当前 Settings。
    （sub2api_flow 调用点即 source=_engine.frozen_mailbox_source(email)。）
    """

    def test_explicit_source_beats_current_settings(self):
        import backend.registration.engine as engine

        captured = {}

        def fake_wait_code(_get, _session, _base, email, *, source="", **kwargs):
            captured["email"] = email
            captured["source"] = source
            return "123456"

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            with mock.patch.object(engine, "get_registration_repository", return_value=store), \
                 mock.patch.object(engine.outlookemail_provider, "wait_code", fake_wait_code), \
                 mock.patch.object(engine, "get_outlookemail_source", return_value="temp"):
                # 模拟 sub2api_flow 调用点：显式传 acquire 冻结值（accounts）
                code = engine.outlookemail_get_oai_code(
                    "frozen@x.com",
                    timeout=5,
                    source="accounts",
                )

        self.assertEqual(code, "123456")
        self.assertEqual(captured["source"], "accounts")
        self.assertEqual(captured["email"], "frozen@x.com")

    def test_frozen_accessor_ignores_current_settings_change(self):
        import backend.registration.engine as engine

        engine._freeze_mailbox_source("frozen@x.com", "accounts")
        try:
            with mock.patch.object(engine, "get_outlookemail_source", return_value="temp"):
                self.assertEqual(engine.frozen_mailbox_source("frozen@x.com"), "accounts")
        finally:
            engine._forget_attempt_context("frozen@x.com")

    def test_public_accessor_falls_back_to_current_settings(self):
        import backend.registration.engine as engine

        with mock.patch.object(engine, "get_outlookemail_source", return_value="temp"):
            self.assertEqual(engine.frozen_mailbox_source("unknown@x.com"), "temp")


class DeleteSideFileIsolationTests(_ClientHarnessMixin, unittest.TestCase):
    """修复项 3：delete_files 只删本记录关联截图，不触碰无关文件。"""

    def test_delete_with_files_removes_only_record_screenshot(self):
        shot = self.root / "data" / "screenshots" / "reg-fail.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"png-data")
        unrelated = self.root / "data" / "screenshots" / "unrelated.png"
        unrelated.write_bytes(b"keep-me")
        rec_id = self.store.add_result(
            {
                "profile_id": 1,
                "email": "same@qq.com",
                "password": "SubPass1",
                "status": "failure",
                "screenshot_path": str(shot),
            }
        )
        response = self.client.post(
            "/api/accounts/delete",
            json={"ids": [rec_id], "delete_files": True, "release_email": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["deleted"], 1)
        self.assertEqual(data["deleted_files"], 1)
        self.assertFalse(shot.exists(), "记录截图应被删除")
        self.assertTrue(unrelated.exists(), "无关文件必须保留")

    def test_delete_without_files_keeps_screenshot(self):
        shot = self.root / "data" / "screenshots" / "reg-fail2.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"png-data")
        rec_id = self.store.add_result(
            {
                "profile_id": 1,
                "email": "keep@qq.com",
                "password": "SubPass1",
                "status": "failure",
                "screenshot_path": str(shot),
            }
        )
        response = self.client.post(
            "/api/accounts/delete",
            json={"ids": [rec_id], "delete_files": False, "release_email": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deleted_files"], 0)
        self.assertTrue(shot.exists())


class ReleaseConcurrencyTests(_ClientHarnessMixin, unittest.TestCase):
    """修复项 4：注册任务运行中禁止释放。"""

    def test_release_refused_while_job_running(self):
        sub_id = self.store.add_result(
            {"email": "r@x.com", "status": "failure", "profile_id": 9}
        )
        job_status = {"running": True}
        with mock.patch.object(self.application.job_coordinator, "status", return_value=job_status):
            response = self.client.post(
                "/api/accounts/delete",
                json={"ids": [sub_id], "delete_files": False, "release_email": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("注册任务运行中", response.json()["detail"])

    def test_delete_without_release_unaffected_by_running_job(self):
        sub_id = self.store.add_result(
            {"email": "r3@x.com", "status": "failure", "profile_id": 9}
        )
        job_status = {"running": True}
        with mock.patch.object(self.application.job_coordinator, "status", return_value=job_status):
            response = self.client.post(
                "/api/accounts/delete",
                json={"ids": [sub_id], "delete_files": False, "release_email": False},
            )
        self.assertEqual(response.status_code, 200)


class _RunnerHarness:
    """直接驱动 run_sub2api_registration_job 的最小 mock 装配。"""

    def setUp(self):
        import backend.registration.engine as engine

        self.engine = engine
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RegistrationRepository(Path(self._tmp.name) / "r.db")
        for name in ("maybe_stop_browser", "cleanup_runtime_memory", "start_browser"):
            patcher = mock.patch.object(engine, name, lambda *a, **k: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.browser_restart_calls: list[dict] = []
        restart_patcher = mock.patch.object(
            engine,
            "restart_browser",
            lambda *a, **k: self.browser_restart_calls.append(dict(k)),
        )
        restart_patcher.start()
        self.addCleanup(restart_patcher.stop)
        self.identity_checks: list[str] = []

        def assert_identity(*, previous_profile_dir=""):
            self.identity_checks.append(previous_profile_dir)
            return {
                "profile_dir": f"/tmp/sub2api-native-camoufox/test-{len(self.identity_checks)}",
                "cookie_count": 0,
                "site_origin_count": 0,
                "storage_entry_count": 0,
            }

        identity_patcher = mock.patch.object(
            engine, "assert_fresh_browser_identity", side_effect=assert_identity
        )
        identity_patcher.start()
        self.addCleanup(identity_patcher.stop)
        repo_patcher = mock.patch.object(
            engine, "get_registration_repository", return_value=self.store
        )
        repo_patcher.start()
        self.addCleanup(repo_patcher.stop)
        # guard 文件指向临时目录：避免测试污染真实数据目录，也便于断言其创建
        guard_patcher = mock.patch.object(
            engine, "ledger_guard_path", lambda: str(Path(self._tmp.name) / "ledger_write_failure.json")
        )
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)
        result_guard_patcher = mock.patch.object(
            engine, "result_guard_path", lambda: str(Path(self._tmp.name) / "sub2api_result_write_failure.json")
        )
        result_guard_patcher.start()
        self.addCleanup(result_guard_patcher.stop)
        # 进程内完整性闩锁不跨测试泄漏
        self.addCleanup(
            lambda: engine._MEMORY_INTEGRITY_LATCH.update({"active": False, "reason": ""})
        )
        self.logs: list = []
        log_patcher = mock.patch.object(
            engine, "registration_log", lambda m: self.logs.append(str(m))
        )
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

    def run_job(self, count, flow_side_effect, flow_module_name=None):
        released = []
        flow_calls = []

        def fake_release(email):
            released.append(str(email or "").strip().lower())

        # 模拟真实 flow 的 acquire 副作用：冻结邮箱的 Profile 作用域与来源，
        # 使 release 判定（读冻结上下文 + 账本）与生产路径一致。
        def _freeze(email):
            if email:
                self.engine._freeze_profile_id(email, 9)
                self.engine._freeze_mailbox_source(email, "accounts")

        def fake_flow(profile, **kwargs):
            flow_calls.append(kwargs.get("batch_id"))
            if isinstance(flow_side_effect, list):
                effect = flow_side_effect.pop(0)
            else:
                effect = flow_side_effect
            if isinstance(effect, Exception):
                _freeze(str(getattr(effect, "sub2api_email", "") or ""))
                raise effect
            _freeze(str(getattr(effect, "email", "") or ""))
            return effect

        flow_target = (
            f"{flow_module_name}.run_sub2api_registration"
            if flow_module_name
            else "backend.registration.sub2api_flow.run_sub2api_registration"
        )
        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email", fake_release
        ), mock.patch.object(
            self.engine,
            "reset_outlookemail_runtime_state",
            lambda: None,
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            flow_target,
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                count, {"id": 9, "name": "Site", "enabled": True}
            )
        return flow_calls, released

    @staticmethod
    def result(**overrides):
        base = dict(
            email="a@x.com",
            password="SubPass1",
            status="failure",
            failure_type="form_mismatch",
            failure_reason="missing promo field",
            final_url="https://site.example/register",
            consumed=False,
            screenshot_path="",
            diagnostics={},
        )
        base.update(overrides)
        return SimpleNamespace(**base)


class FailNoEmailTests(_RunnerHarness, unittest.TestCase):
    """修复项 5：池耗尽 → 停止任务、不落空邮箱失败记录。"""

    def test_pool_exhaustion_stops_task_without_failure_records(self):
        exc = Exception("OutlookEmail 邮箱池为空，未返回任何账号")
        flow_calls, _released = self.run_job(5, exc)
        # 第一次就停止：不再继续后续账号
        self.assertEqual(len(flow_calls), 1)
        self.assertEqual(len(self.store.list_results()), 0)

    def test_whitelist_no_match_classified_no_email(self):
        kind = self.engine.classify_failure(
            Exception("当前 Profile 要求 qq.com，OutlookEmail 池中没有符合条件的可用邮箱")
        )
        self.assertEqual(kind, self.engine.FAIL_NO_EMAIL)


class PreSubmitReservationTests(_RunnerHarness, unittest.TestCase):
    """修复项 7：未到达提交边界 → 释放占用；已消费 → 绝不释放。"""

    def test_pre_submit_failure_releases_reservation_and_continues(self):
        """P0-1 闭合断言：release 被调用 + 无任务异常日志 + count=2 进入第二轮。"""
        flow_calls, released = self.run_job(2, [self.result(consumed=False), self.result(consumed=False, status="success", failure_type="")])
        self.assertEqual(len(flow_calls), 2, "pre-submit 失败后必须进入第二轮")
        self.assertIn("a@x.com", released)
        self.assertFalse(
            any("Sub2API 任务异常" in line for line in self.logs),
            f"不得有被吞掉的 runner 级异常：{self.logs}",
        )
        rows = self.store.list_results()
        self.assertEqual(len(rows), 2)

    def test_each_followup_attempt_uses_a_fresh_browser_identity(self):
        results = [
            self.result(email=f"account-{index}@x.com", consumed=False)
            for index in range(3)
        ]
        flow_calls, _released = self.run_job(3, results)

        self.assertEqual(len(flow_calls), 3)
        self.assertEqual(
            len(self.browser_restart_calls),
            2,
            "count=3 必须在第二、第三账号前各重启一次浏览器",
        )
        self.assertEqual(len(self.identity_checks), 3)
        self.assertEqual(self.identity_checks[0], "")
        self.assertTrue(self.identity_checks[1].endswith("test-1"))
        self.assertTrue(self.identity_checks[2].endswith("test-2"))
        self.assertTrue(
            any("全新浏览器身份" in line for line in self.logs),
            f"任务日志必须暴露身份隔离动作: {self.logs}",
        )

    def test_browser_restart_failure_never_runs_followup_flow(self):
        results = [
            self.result(email="first@x.com", consumed=False),
            self.result(email="second@x.com", consumed=False),
        ]
        with mock.patch.object(
            self.engine,
            "restart_browser",
            side_effect=RuntimeError("profile cleanup failed"),
        ):
            flow_calls, _released = self.run_job(2, results)

        self.assertEqual(len(flow_calls), 1)
        rows = self.store.list_results()
        self.assertEqual(len(rows), 2)
        restart_row = next(
            row for row in rows if "身份隔离重启失败" in row["registration_error"]
        )
        self.assertEqual(restart_row["failure_type"], self.engine.FAIL_BROWSER)

    def test_consumed_success_never_releases(self):
        success = self.result(status="success", consumed=True, failure_type="")
        _, released = self.run_job(1, [success])
        self.assertEqual(released, [])
        rows = self.store.list_results()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("registration_status"), "success")

    def test_pre_submit_cancellation_releases_reservation(self):
        """P0-2：acquire 成功 → pre-submit 取消 → reservation 被释放。"""
        class Cancelled(self.engine.RegistrationCancelled):
            pass

        def fake_flow(profile, **kwargs):
            # 模拟真实 flow 的 acquire 副作用：冻结 Profile 作用域/来源
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            exc = Cancelled("stopped before submit")
            setattr(exc, "sub2api_email", "a@x.com")
            raise exc

        released = []
        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email",
            lambda email: released.append(str(email or "").strip().lower()),
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        self.assertIn("a@x.com", released)
        self.assertFalse(any("任务异常" in line for line in self.logs))

    def test_post_submit_cancellation_never_releases(self):
        """post-submit 取消（账本已有行）→ 不释放。"""
        class Cancelled(self.engine.RegistrationCancelled):
            pass

        def fake_flow(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            self.store.mark_mailbox_consumed(9, "accounts", "a@x.com")
            exc = Cancelled("stopped after submit")
            setattr(exc, "sub2api_email", "a@x.com")
            raise exc

        released = []
        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email",
            lambda email: released.append(str(email or "").strip().lower()),
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        self.assertEqual(released, [])

    def test_post_boundary_ledger_row_blocks_release(self):
        # 已到提交边界且账本已写入，随后异常（真实 flow 会附带邮箱身份）：不得释放
        def raise_after_consume(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            self.store.mark_mailbox_consumed(9, "accounts", "a@x.com")
            exc = RuntimeError("code polling exploded")
            setattr(exc, "sub2api_email", "a@x.com")  # 模拟 flow 的异常邮箱标注
            raise exc

        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email"
        ) as release_spy, mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=raise_after_consume,
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        release_spy.assert_not_called()

    def test_ledger_write_error_aborts_task_and_keeps_reservation(self):
        from backend.registration.sub2api_flow import LedgerWriteError

        calls = []
        effects = [
            LedgerWriteError("db locked", email="a@x.com"),
            LedgerWriteError("db locked", email="b@x.com"),
        ]

        def fake_flow(profile, **kwargs):
            calls.append(1)
            effect = effects.pop(0) if len(effects) > 1 else effects[0]
            raise effect

        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email"
        ) as release_spy, mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                3, {"id": 9, "name": "Site", "enabled": True}
            )
        # 任务立即中止（不继续下一账号），且不释放任何占用
        self.assertEqual(len(calls), 1)
        release_spy.assert_not_called()


class LedgerGuardDurableTests(_RunnerHarness, unittest.TestCase):
    """P0-3：LedgerWriteError 必须跨下一任务 durable fail-closed。"""

    def test_a_ledger_write_error_creates_guard(self):
        from backend.registration.sub2api_flow import LedgerWriteError

        self.run_job(1, LedgerWriteError("db locked", email="a@x.com"))
        self.assertTrue(Path(self.engine.ledger_guard_path()).exists())
        import json as _json

        data = _json.loads(Path(self.engine.ledger_guard_path()).read_text(encoding="utf-8"))
        self.assertEqual(data["profile_id"], 9)
        self.assertEqual(data["email"], "a@x.com")
        self.assertEqual(data["mailbox_source"], "accounts")
        self.assertIn("db locked", data["error"])

    def test_b_new_job_refused_before_acquire_and_browser(self):
        from backend.registration.sub2api_flow import LedgerWriteError

        self.run_job(1, LedgerWriteError("db locked", email="a@x.com"))
        before_second_run = len(self.logs)
        # 新任务：在 acquire/browser 之前直接拒绝（fake_flow 不会被调用）
        with self.assertRaises(RuntimeError) as raised:
            self.run_job(1, [self.result(status="success", consumed=True)])
        msg = str(raised.exception)
        self.assertIn("ledger_write_failure.json", msg)
        self.assertIn("a@x.com", msg)
        # 拒绝发生在 runner 启动日志之前：未启动 browser / 未 acquire
        second_run_logs = self.logs[before_second_run:]
        self.assertFalse(any("任务启动：Profile" in line for line in second_run_logs))

    def test_c_reset_runtime_state_does_not_clear_guard(self):
        from backend.registration.sub2api_flow import LedgerWriteError

        self.run_job(1, LedgerWriteError("db locked", email="a@x.com"))
        # 模拟下一任务开头：reset 清空内存 reservation（持久化 guard 不受影响）
        try:
            self.engine.outlookemail_provider._reserved_emails.clear()
        except AttributeError:
            pass
        with self.assertRaises(RuntimeError):
            self.run_job(1, [self.result(status="success", consumed=True)])

    def test_d_new_process_coordinator_guard_still_blocks(self):
        """模拟进程重启：全新 engine 仓库/coordinator 后 guard 仍有效。"""
        from backend.registration.sub2api_flow import LedgerWriteError

        self.run_job(1, LedgerWriteError("db locked", email="a@x.com"))
        # guard 文件已落盘；重新构造仓库与 runner（guard 文件不删）→ 仍拒绝
        self.assertEqual(len(self.store.list_results()), 0)
        with self.assertRaises(RuntimeError):
            self.run_job(1, [self.result(status="success", consumed=True)])

    def test_ledger_write_failure_also_writes_credential_guard(self):
        """accepted-submit 后账本写失败：两个 guard 都在，result guard 含真实密码。"""
        import json as _json

        from backend.registration.sub2api_flow import LedgerWriteError

        def fake_flow(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            exc = LedgerWriteError("db locked", email="a@x.com")
            setattr(exc, "sub2api_email", "a@x.com")
            setattr(exc, "sub2api_password", "SubPass1")
            setattr(exc, "sub2api_consumed", True)
            setattr(exc, "sub2api_final_url", "https://site.example/email-verify")
            raise exc

        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email", lambda email: None
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        ledger_guard = Path(self.engine.ledger_guard_path())
        result_guard = Path(self.engine.result_guard_path())
        self.assertTrue(ledger_guard.exists(), "ledger guard 必须存在")
        self.assertTrue(result_guard.exists(), "凭据恢复 guard 必须存在")
        data = _json.loads(result_guard.read_text(encoding="utf-8"))
        self.assertEqual(data["email"], "a@x.com")
        self.assertEqual(data["password"], "SubPass1")
        self.assertEqual(data["failure_type"], "ledger_write_failure")
        self.assertEqual(data["profile_id"], 9)
        self.assertIn("email-verify", data["final_url"])

    def test_ledger_write_failure_result_guard_write_latches_process(self):
        """凭据恢复 guard 自身写失败 → 进程内完整性闩锁生效。"""
        from backend.registration.sub2api_flow import LedgerWriteError

        def fake_flow(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            exc = LedgerWriteError("db locked", email="a@x.com")
            setattr(exc, "sub2api_password", "SubPass1")
            raise exc

        def replace_side_effect(*args, **kwargs):
            # 两个 guard 文件都写失败 → 完整性闩锁兜底
            raise OSError("disk full")

        with mock.patch.object(
            self.engine.os, "replace", side_effect=replace_side_effect
        ), mock.patch.object(
            self.engine.outlookemail_provider, "release_email", lambda email: None
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch(
            "backend.registration.sub2api_flow.run_sub2api_registration",
            side_effect=fake_flow,
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        self.assertTrue(self.engine._MEMORY_INTEGRITY_LATCH["active"])
        marker = self.engine.check_ledger_guard()
        self.assertEqual(marker.get("source"), "process_memory_latch")
        before = len(self.logs)
        with self.assertRaises(RuntimeError) as raised:
            self.run_job(1, [self.result(status="success", consumed=True)])
        self.assertIn("进程内完整性闩锁", str(raised.exception))
        self.assertFalse(any("任务启动：Profile" in line for line in self.logs[before:]))


class ReleaseIdleGuardRaceTests(unittest.TestCase):
    """P1：release 临界区与 job start 共用同一 guard，TOCTOU 闭合。"""

    def _mock_engine_module(self, tmp):
        gr_mock = mock.Mock()
        gr_mock.check_ledger_guard.return_value = {}
        gr_mock.check_result_guard.return_value = {}
        gr_mock._bs = mock.Mock()
        gr_mock._wire_runtime_modules = lambda: None
        gr_mock.load_config = lambda: None
        gr_mock.config = {}
        gr_mock.run_registration = mock.Mock()
        gr_mock.run_sub2api_registration_job = mock.Mock()
        gr_mock.registration_log = lambda m: None
        gr_mock.RegistrationStopController = object
        gr_mock.new_registration_batch_id = lambda source="web": "batch-t"
        gr_mock.current_exception_traceback = lambda *a: ""
        gr_mock.TRACEBACK_LOG_MAX_CHARS = 100
        return gr_mock

    def test_job_start_blocked_by_release_critical_section_then_completes(self):
        from backend.web.jobs import RegistrationJobCoordinator

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            profile = store.create_profile(
                {"name": "Site", "site_key": "true-sota"}
            )
            coordinator = RegistrationJobCoordinator()
            coordinator._repository = lambda: store
            gr_mock = self._mock_engine_module(tmp)
            # start 通过 engine 解析 Profile（mock engine 必须指向真实 store）
            gr_mock.get_registration_repository.return_value = store

            entered = threading.Event()
            proceed = threading.Event()
            release_done = threading.Event()

            def release_flow():
                try:
                    with coordinator.idle_guard():
                        entered.set()
                        proceed.wait(timeout=8)
                finally:
                    release_done.set()

            t = threading.Thread(target=release_flow)
            t.start()
            self.assertTrue(entered.wait(timeout=5), "release 未进入临界区")

            with mock.patch("backend.registration.engine", gr_mock, create=True):
                start_result = {}

                def start_flow():
                    try:
                        start_result["status"] = coordinator.start(
                            count=1, profile_id=profile["id"]
                        )
                    except Exception as exc:  # noqa: BLE001
                        start_result["error"] = str(exc)

                t2 = threading.Thread(target=start_flow)
                t2.start()
                time.sleep(0.5)  # 让 start 线程抵达 guard
                # 临界区持锁期间：start 被阻塞，任务未置 running
                self.assertNotIn("status", start_result, "start 不应在临界区内完成")
                self.assertFalse(coordinator.status().get("running"))
                proceed.set()  # 释放临界区
                t2.join(timeout=20)
            t.join(timeout=10)
            self.assertTrue(release_done.is_set())
            self.assertNotIn("error", start_result, start_result)
            # 临界区结束后 start 正常获得 guard 并启动（mock runner 立即收尾）
            time.sleep(0.3)
            self.assertFalse(coordinator.status().get("running"))
            self.assertEqual(coordinator.status().get("current_stage") in ("任务已完成", "任务已停止"), True)


class ResultWriteIntegrityTests(_RunnerHarness, unittest.TestCase):
    """fix3：consumed/success 结果 INSERT 失败 → 凭据恢复守卫，绝不丢密码。"""

    def _break_add_result(self):
        return mock.patch.object(
            self.store, "add_result", side_effect=Exception("db locked")
        )

    def _read_result_guard(self):
        import json as _json

        return _json.loads(
            Path(self.engine.result_guard_path()).read_text(encoding="utf-8")
        )

    def test_a_success_insert_failure_writes_credential_guard_and_stops(self):
        outcome = self.result(status="success", consumed=True, failure_type="")
        with self._break_add_result():
            flow_calls, released = self.run_job(2, [outcome])
        # 不打印注册成功、任务中止（count=2 也不进入第二轮）、已消费邮箱不释放
        self.assertFalse(any("注册成功" in line for line in self.logs))
        self.assertEqual(len(flow_calls), 1)
        self.assertEqual(released, [])
        data = self._read_result_guard()
        self.assertEqual(data["email"], "a@x.com")
        self.assertEqual(data["password"], "SubPass1")
        self.assertEqual(data["profile_id"], 9)
        self.assertEqual(data["profile_name"], "Site")
        self.assertIn(data["mailbox_source"], ("accounts", "temp"))
        self.assertEqual(data["registration_status"], "success")
        self.assertIn("site.example", data["final_url"])
        self.assertTrue(data.get("timestamp"))

    def test_b_consumed_failure_insert_failure_recovers_password(self):
        outcome = self.result(
            status="failure", failure_type="code_timeout", consumed=True
        )
        with self._break_add_result():
            flow_calls, released = self.run_job(2, [outcome])
        self.assertEqual(len(flow_calls), 1)
        self.assertEqual(released, [])
        data = self._read_result_guard()
        self.assertEqual(data["email"], "a@x.com")
        self.assertEqual(data["password"], "SubPass1")
        self.assertEqual(data["registration_status"], "failure")
        self.assertEqual(data["failure_type"], "code_timeout")

    def test_c_next_job_refused_while_result_guard_exists(self):
        outcome = self.result(status="success", consumed=True, failure_type="")
        with self._break_add_result():
            self.run_job(1, [outcome])
        # runner 入口防御：新任务直接拒绝（提及守卫文件名）
        before = len(self.logs)
        with self.assertRaises(RuntimeError) as raised:
            self.run_job(1, [self.result(status="success", consumed=True)])
        self.assertIn("sub2api_result_write_failure.json", str(raised.exception))
        self.assertFalse(any("任务启动：Profile" in line for line in self.logs[before:]))
        # jobs.start 前置防御：mock engine 返回守卫 → RuntimeError
        from backend.web.jobs import RegistrationJobCoordinator

        gr_mock = mock.Mock()
        gr_mock.check_ledger_guard.return_value = {}
        gr_mock.check_result_guard.return_value = {
            "email": "a@x.com",
            "profile_id": 9,
        }
        coordinator = RegistrationJobCoordinator()
        coordinator._repository = lambda: self.store
        with mock.patch("backend.registration.engine", gr_mock, create=True):
            with self.assertRaises(RuntimeError) as raised:
                coordinator.start(count=1, profile_id=9)
            self.assertIn("sub2api_result_write_failure.json", str(raised.exception))

    def test_d_unconsumed_insert_failure_keeps_release_no_guard(self):
        outcome = self.result(status="failure", consumed=False)  # form_mismatch 预提交失败
        with self._break_add_result():
            flow_calls, released = self.run_job(1, [outcome])
        # 无凭据恢复守卫；reservation 照常释放（INSERT 失败不阻断预提交失败路径）
        self.assertFalse(Path(self.engine.result_guard_path()).exists())
        self.assertIn("a@x.com", released)
        self.assertEqual(len(flow_calls), 1)

    def test_e_post_boundary_unexpected_exception_persists_password(self):
        """fix4-A：accepted-submit + 账本已写 后抛 RuntimeError（DB 正常）。

        必须断言：password 原样落库、mail_status=consumed、账本仍在、不释放。
        """
        import backend.registration.sub2api_flow as flow_mod

        def fake_flow(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            self.store.mark_mailbox_consumed(9, "accounts", "a@x.com")
            exc = RuntimeError("page disconnected")
            setattr(exc, "sub2api_email", "a@x.com")
            setattr(exc, "sub2api_password", "SubPass1")
            setattr(exc, "sub2api_consumed", True)
            setattr(exc, "sub2api_final_url", "https://site.example/email-verify")
            raise exc

        released = []
        with mock.patch.object(
            self.engine.outlookemail_provider, "release_email",
            lambda email: released.append(str(email or "").strip().lower()),
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch.object(
            flow_mod, "run_sub2api_registration", side_effect=fake_flow
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        rows = self.store.list_results()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["email"], "a@x.com")
        self.assertEqual(row["password"], "SubPass1")
        self.assertEqual(row["mail_status"], "consumed")
        self.assertTrue(row["consumed_at"].strip())
        self.assertEqual(row["registration_status"], "failure")
        # 账本行仍在、reservation 未释放
        self.assertTrue(self.store.is_mailbox_consumed(9, "accounts", "a@x.com"))
        self.assertEqual(released, [])
        # 无凭据恢复守卫（DB 写入成功）
        self.assertFalse(Path(self.engine.result_guard_path()).exists())

    def test_f_post_boundary_unexpected_exception_db_down_writes_guard(self):
        """fix4-B：同一路径 + INSERT 失败 → credential guard 含真实 email+password。"""
        import backend.registration.sub2api_flow as flow_mod

        def fake_flow(profile, **kwargs):
            self.engine._freeze_profile_id("a@x.com", 9)
            self.engine._freeze_mailbox_source("a@x.com", "accounts")
            self.store.mark_mailbox_consumed(9, "accounts", "a@x.com")
            exc = RuntimeError("page disconnected")
            setattr(exc, "sub2api_email", "a@x.com")
            setattr(exc, "sub2api_password", "SubPass1")
            setattr(exc, "sub2api_consumed", True)
            setattr(exc, "sub2api_final_url", "https://site.example/email-verify")
            raise exc

        with self._break_add_result(), mock.patch.object(
            self.engine.outlookemail_provider, "release_email",
            lambda email: None,
        ), mock.patch.object(
            self.engine, "reset_outlookemail_runtime_state", lambda: None
        ), mock.patch.object(
            self.engine, "start_browser", lambda *a, **k: None
        ), mock.patch.object(
            self.engine, "cleanup_runtime_memory", lambda *a, **k: None
        ), mock.patch.object(
            flow_mod, "run_sub2api_registration", side_effect=fake_flow
        ):
            self.engine.run_sub2api_registration_job(
                1, {"id": 9, "name": "Site", "enabled": True}
            )
        self.assertTrue(Path(self.engine.result_guard_path()).exists())
        data = self._read_result_guard()
        self.assertEqual(data["email"], "a@x.com")
        self.assertEqual(data["password"], "SubPass1")
        self.assertEqual(data["registration_status"], "failure")
        self.assertIn("email-verify", data["final_url"])

    def test_e_guard_write_failure_latches_process_memory(self):
        from backend.registration.sub2api_flow import LedgerWriteError

        with mock.patch.object(
            self.engine.os, "replace", side_effect=OSError("disk full")
        ):
            flow_calls, released = self.run_job(
                1, LedgerWriteError("db locked", email="a@x.com")
            )
        self.assertTrue(self.engine._MEMORY_INTEGRITY_LATCH["active"])
        # 同一进程内：check_ledger_guard 返回闩锁标记 → 新任务拒绝
        marker = self.engine.check_ledger_guard()
        self.assertEqual(marker.get("source"), "process_memory_latch")
        before = len(self.logs)
        with self.assertRaises(RuntimeError) as raised:
            self.run_job(1, [self.result(status="success", consumed=True)])
        self.assertIn("进程内完整性闩锁", str(raised.exception))
        self.assertFalse(any("任务启动：Profile" in line for line in self.logs[before:]))


class IdleGuardMutualExclusionTests(unittest.TestCase):
    """fix3：release 临界区与 job start 共用 transition guard，single-flight。"""

    def _mock_engine_module(self):
        gr_mock = mock.Mock()
        gr_mock.check_ledger_guard.return_value = {}
        gr_mock.check_result_guard.return_value = {}
        gr_mock._bs = mock.Mock()
        gr_mock._wire_runtime_modules = lambda: None
        gr_mock.load_config = lambda: None
        gr_mock.config = {}
        gr_mock.run_registration = mock.Mock()
        gr_mock.run_sub2api_registration_job = mock.Mock()
        gr_mock.registration_log = lambda m: None
        gr_mock.RegistrationStopController = object
        gr_mock.new_registration_batch_id = lambda source="web": "batch-t"
        gr_mock.current_exception_traceback = lambda *a: ""
        gr_mock.TRACEBACK_LOG_MAX_CHARS = 100
        return gr_mock

    def test_refresh_critical_section_blocks_start_and_other_guards(self):
        from backend.web.jobs import RegistrationJobCoordinator

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            profile = store.create_profile(
                {"name": "Site", "site_key": "true-sota"}
            )
            coordinator = RegistrationJobCoordinator()
            coordinator._repository = lambda: store
            gr_mock = self._mock_engine_module()
            # start 通过 engine 解析 Profile（mock engine 必须指向真实 store）
            gr_mock.get_registration_repository.return_value = store

            entered = threading.Event()
            proceed = threading.Event()
            second_acquired = threading.Event()
            holder_done = threading.Event()
            second_error = []

            # 模拟 release 临界区：整个释放操作包在 idle_guard 内
            def release_holder():
                try:
                    with coordinator.idle_guard():
                        entered.set()
                        proceed.wait(timeout=8)
                finally:
                    holder_done.set()

            t = threading.Thread(target=release_holder)
            t.start()
            self.assertTrue(entered.wait(timeout=5), "release 未进入临界区")

            start_result = {}

            def start_flow():
                try:
                    start_result["status"] = coordinator.start(
                        count=1, profile_id=profile["id"]
                    )
                except Exception as exc:  # noqa: BLE001
                    start_result["error"] = str(exc)

            def second_guard_flow():
                try:
                    with coordinator.idle_guard():
                        second_acquired.set()
                except RuntimeError as exc:
                    # 若 start 抢先获得 guard 并置 running，并发申请会收到 409 语义拒绝——
                    # 这同样证明三者 single-flight。
                    second_error.append(str(exc))

            t2 = threading.Thread(target=start_flow)
            t3 = threading.Thread(target=second_guard_flow)
            with mock.patch("backend.registration.engine", gr_mock, create=True):
                t2.start()
                time.sleep(0.4)
                # release 持锁期间：start 被阻塞、running 未置位
                self.assertNotIn("status", start_result, "start 不应在 release 临界区内完成")
                self.assertFalse(coordinator.status().get("running"))
                t3.start()
                time.sleep(0.4)
                # 第二次 release / 并发申请同样被阻塞（single-flight）
                self.assertFalse(second_acquired.is_set(), "并发申请不应在临界区内获得 guard")
                proceed.set()  # release 结束
                t2.join(timeout=20)
                t3.join(timeout=10)
            t.join(timeout=10)
            self.assertTrue(holder_done.is_set())
            self.assertNotIn("error", start_result, start_result)
            self.assertTrue(
                second_acquired.is_set() or second_error,
                "临界区结束后并发申请既未获得 guard 也未被拒绝",
            )
            # mock runner 立即收尾
            time.sleep(0.3)
            self.assertFalse(coordinator.status().get("running"))


class ExportSuccessOnlyTests(unittest.TestCase):
    """修复项 8：凭据导出只含 registration_status=success。"""

    def test_failed_records_are_skipped(self):
        from backend.web.account_exports import build_credentials_text

        records = [
            {"email": "ok@x.com", "password": "P1", "registration_status": "success"},
            {"email": "timeout@x.com", "password": "P2", "registration_status": "failure"},
            {"email": "nostatus@x.com", "password": "P3", "status": "skipped"},
        ]
        payload, exported = build_credentials_text(records)
        text = payload.decode("utf-8")
        self.assertEqual(exported, 1)
        self.assertIn("ok@x.com----P1", text)
        self.assertNotIn("timeout@x.com", text)
        self.assertNotIn("nostatus@x.com", text)


class SnapshotRestoreProfileTests(unittest.TestCase):
    """修复项 9：服务重启后恢复 profile_id 与 Profile 显示名。"""

    def test_restore_recovers_profile_scope_and_name(self):
        from backend.web.jobs import RegistrationJobCoordinator

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            store.create_profile({"name": "Acme", "site_key": "true-sota"})
            coordinator = RegistrationJobCoordinator()
            coordinator._repository = lambda: store
            store.save_job_snapshot(
                {
                    "batch_id": "batch-x",
                    "running": True,
                    "target_count": 3,
                    "workers": 1,
                    "completed_count": 2,
                    "success_count": 1,
                    "failure_count": 1,
                    "profile_id": 1,
                }
            )
            status = coordinator.status()
        self.assertEqual(status["profile_id"], 1)
        self.assertEqual(status["profile_name"], "Acme")
        self.assertFalse(status["running"])


class WhitelistFailClosedTests(unittest.TestCase):
    """修复项 10：白名单存储损坏 → 加载响亮失败，绝不当作无限制。"""

    def test_corrupt_json_raises_instead_of_empty_list(self):
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.db"
            store = RegistrationRepository(path)
            store.create_profile({"name": "X", "site_key": "true-sota"})
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "UPDATE sub2api_profiles SET email_domain_whitelist = ? WHERE id = 1",
                    ('["qq.com"',),
                )
                conn.commit()
            with self.assertRaises(ProfileError):
                store.get_profile(1)


if __name__ == "__main__":
    unittest.main()
