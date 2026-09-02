import tempfile
import unittest
import re
from pathlib import Path
from unittest import mock

from backend.registration import engine
from backend.registration import sub2api_flow as flow
from backend.registration.store import RegistrationRepository
from backend.automation.page_adapter import CamoufoxPage


class FakePage:
    """模拟 Sub2API 站点页面：register → (submit) → email-verify / dashboard。

    通过 run_js 脚本特征分派到各页面行为；可配置字段存在性、Turnstile、
    提交后去向，用于验证消费边界与成功判定。
    """

    def __init__(self, post_submit="email-verify", fields=None, turnstile=False):
        self.url = "https://site.example/register"
        self.post_submit = post_submit
        self.turnstile = turnstile
        self.page = "register"
        self.submitted = False
        self.values = {}
        self.clicks = 0
        self.verify_failure_body = ""
        self.fields = {
            "email": True,
            "password": True,
            "promo": False,
            "invitation": False,
            "code": False,
        }
        if fields:
            self.fields.update(fields)
        self.raw_page = _FakeRawPage([])

    class _Wait:
        def doc_loaded(self):
            pass

    @property
    def wait(self):
        return self._Wait()

    @property
    def title(self):
        return "Sub2API"

    def get(self, url, **kw):
        self.url = url
        if "/email-verify" in url:
            self.page = "email-verify"
            self.fields["code"] = True

    def _field_for(self, ids, names, page):
        targets = list(ids) + list(names)
        text = " ".join(targets).lower()
        if page == "email-verify":
            if any(t in text for t in ("code", "otp", "verification")):
                return "code" if self.fields.get("code") else None
            return None
        # register 页
        if "email" in text:
            return "email" if self.fields.get("email") else None
        if "password" in text:
            return "password" if self.fields.get("password") else None
        if any(t in text for t in ("promo",)):
            return "promo" if self.fields.get("promo") else None
        if any(t in text for t in ("invitation", "invite")):
            return "invitation" if self.fields.get("invitation") else None
        return None

    def run_js(self, script, *args):
        s = script
        if "document.readyState" in s:
            return "complete"
        if "location.href, title: document.title" in s:
            # visible snapshot
            return {
                "url": self.url,
                "title": "Sub2API",
                "inputs": [],
                "buttons": (
                    [{"text": "Create", "disabled": False, "type": "submit", "id": ""}]
                    if self.page in ("register", "email-verify")
                    else []
                ),
                "body": (
                    "register form"
                    if self.page == "register"
                    else self.verify_failure_body
                ),
            }
        if "data-sub2api-native-field" in s:
            ids, names, _types, _autocompletes, _placeholders, marker = args
            field = self._field_for(ids, names, self.page)
            if field is None:
                return {"ok": False}
            return {
                "ok": True,
                "selector": f'[data-sub2api-native-field="sub2api-{marker}"]',
                "id": "",
                "name": "",
                "type": "text",
            }
        if "Object.getOwnPropertyDescriptor(proto, 'value')" in s:
            selector, value = args
            matched = re.search(r'="sub2api-([^"]+)"', selector)
            key = matched.group(1) if matched else selector
            key = {"verification-code": "code"}.get(key, key)
            self.values[key] = value
            return True
        if "const hints = ['create'" in s:
            if self.page == "register":
                return {"ok": True, "selector": "__text__:Create", "text": "Create"}
            if self.page == "email-verify" and self.values.get("code"):
                return {"ok": True, "selector": "__text__:Verify", "text": "Verify"}
            return {"ok": False}
        if "arg_text" in s:
            # click by text
            if self.page == "register" and not self.submitted:
                self.submitted = True
                self.clicks += 1
                self.url = self._post_submit_url()
                self.page = self.page_after_submit  # register -> email-verify / dashboard
                if self.page_after_submit == "email-verify":
                    self.fields["code"] = True
            elif self.page == "email-verify" and self.values.get("code"):
                if not self.verify_failure_body:
                    # 验证码提交成功 → 进入 dashboard
                    self.page = "dashboard"
                    self.url = "https://site.example/dashboard"
            return True
        if "document.querySelectorAll(arg_sel)" in s:
            # turnstile presence
            return 1 if self.turnstile else 0
        if "node.click()" in s:
            return True
        if "getResponse()" in s:
            return "x" * 90 if self.turnstile else ""
        return ""

    def _post_submit_url(self):
        self.page_after_submit = self.post_submit
        if self.post_submit == "dashboard":
            return "https://site.example/dashboard"
        if self.post_submit == "email-verify":
            return "https://site.example/email-verify"
        return "https://site.example/register"


class _FakeRawPage:
    def __init__(self, frames):
        self.frames = frames


class _EvaluateProbe:
    """Expose the exact Playwright wrapper generated by CamoufoxPage."""

    context = object()

    def __init__(self, result):
        self.result = result
        self.calls = []

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        return self.result


def _base_profile(**over):
    p = {
        "id": 1,
        "name": "TestSite",
        "register_url": "https://site.example/register",
        "register_origin": "https://site.example",
        "promo_code": "",
        "invitation_code": "",
        "aff_code": "",
        "whitelist": [],
        "enabled": True,
    }
    p.update(over)
    return p


class Sub2apiFlowTests(unittest.TestCase):
    def test_ctai_send_code_uses_timeout_and_classifies_email_exists(self):
        class CtaiPage:
            def __init__(self):
                self.script = ""

            def run_js(self, script, *args):
                self.script = script
                return {
                    "status": 409,
                    "body": {"code": "EMAIL_EXISTS", "message": "email already exists"},
                }

        page = CtaiPage()
        with self.assertRaises(flow.Sub2apiFlowError) as raised:
            flow._ctai_send_verify_code(
                page,
                origin="https://site.example",
                email="fixture@example.com",
            )
        self.assertEqual(raised.exception.failure_type, flow.FAIL_ALREADY_REGISTERED)
        self.assertIn("AbortController", page.script)
        self.assertIn("30000", page.script)

    def test_build_registration_url_uses_site_aff_query_key(self):
        url = flow.build_registration_url({
            "register_url": "https://site.example/register?plan=free",
            "aff_code": "AFF-123",
        })
        self.assertEqual(url, "https://site.example/register?plan=free&aff=AFF-123")

    def test_input_locator_returns_value_through_camoufox_run_js_contract(self):
        raw = _EvaluateProbe({
            "ok": True,
            "selector": '[data-sub2api-native-field="sub2api-email"]',
            "id": "email",
            "name": "",
            "type": "email",
        })
        page = CamoufoxPage(raw)

        found = flow._find_input(
            page,
            ids=("email",),
            names=("email",),
            types=("email",),
            marker="email",
        )

        self.assertEqual(found["id"], "email")
        wrapped, args = raw.calls[0]
        self.assertIn("return (function", wrapped)
        self.assertEqual(args[0], ["email"])
        self.assertEqual(args[5], "email")

    def _store(self, tmp):
        return RegistrationRepository(Path(tmp) / "results.sqlite3")

    def _run(self, page, profile, *, acquired=("acct@example.com", "tok"), code_timeout=None):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            consumed = []

            def fake_acquire(prof):
                # 冻结作用域/来源（模拟 engine.acquire_email 的副作用）
                email = acquired[0]
                engine._freeze_profile_id(email, int(prof["id"]))
                engine._freeze_mailbox_source(email, "accounts")
                return email, "tok"

            def fake_mark(email, *, batch_id="", reason="", profile_id=None, log_callback=None):
                consumed.append((email, profile_id))
                return True

            patchers = [
                mock.patch.object(engine, "get_registration_repository", return_value=store),
                mock.patch.object(flow, "_require_page", return_value=page),
                mock.patch.object(flow._engine, "acquire_email", side_effect=fake_acquire),
                mock.patch.object(flow._engine, "mark_mailbox_consumed", side_effect=fake_mark),
                mock.patch.object(flow._engine, "capture_failure_screenshot", return_value=""),
                mock.patch.object(flow, "sleep_with_cancel", lambda *a, **k: None),
                mock.patch.object(flow, "raise_if_cancelled", lambda cb=None: None),
            ]
            if code_timeout is not None:
                patchers.append(mock.patch.object(flow, "CODE_POLL_TIMEOUT", code_timeout))
            for patcher in patchers:
                patcher.start()
            try:
                result = flow.run_sub2api_registration(
                    profile, batch_id="b1", acquire=fake_acquire
                )
            finally:
                for patcher in patchers:
                    patcher.stop()
        return result, consumed, store

    def test_dashboard_success_without_verification(self):
        page = FakePage(post_submit="dashboard")
        result, consumed, _ = self._run(page, _base_profile())
        self.assertEqual(result.status, "success")
        self.assertTrue(result.consumed)
        self.assertIn(result.email, [c[0] for c in consumed])
        self.assertIn("/dashboard", result.final_url)

    def test_email_verify_marks_consumed_before_code(self):
        page = FakePage(post_submit="email-verify")
        code_calls = []
        click_observed_at = []
        real_click = flow._click_button

        def fake_code(email, **kw):
            code_calls.append((email, kw))
            return "123456"

        def observed_click(current_page, selector):
            click_observed_at.append(flow.time.time())
            return real_click(current_page, selector)

        with mock.patch.object(
            flow._engine, "outlookemail_get_oai_code", side_effect=fake_code
        ), mock.patch.object(flow, "_click_button", side_effect=observed_click):
            result, consumed, _ = self._run(page, _base_profile())
        # 已消费且消费发生在取码之前（consumed 非空）
        self.assertTrue(result.consumed)
        self.assertEqual(len(code_calls), 1)
        self.assertGreater(code_calls[0][1]["min_received_at"], 0)
        self.assertLessEqual(code_calls[0][1]["min_received_at"], click_observed_at[0])
        self.assertTrue(consumed)
        # 取码 → 填码 → 提交 → dashboard 证据 → 成功
        self.assertEqual(result.status, "success")
        self.assertIn("/dashboard", result.final_url)

    def test_ctai_enters_code_polling_with_initially_disabled_verify_button(self):
        page = FakePage(post_submit="email-verify")

        with mock.patch.object(flow, "_ctai_send_verify_code"), mock.patch.object(
            flow._engine, "outlookemail_get_oai_code", return_value="123456"
        ):
            result, consumed, _ = self._run(
                page, _base_profile(site_key="ctai")
            )

        self.assertEqual(result.status, "success")
        self.assertTrue(result.consumed)
        self.assertEqual(len(consumed), 1)

    def test_ctai_consumes_only_exact_email_when_site_reports_already_registered(self):
        page = FakePage(post_submit="email-verify")
        page.verify_failure_body = "Email already exists"

        with mock.patch.object(flow, "_ctai_send_verify_code"), mock.patch.object(
            flow._engine, "outlookemail_get_oai_code", return_value="123456"
        ):
            result, consumed, _ = self._run(
                page, _base_profile(site_key="ctai")
            )

        self.assertEqual(result.failure_type, flow.FAIL_ALREADY_REGISTERED)
        self.assertTrue(result.consumed)
        self.assertEqual(consumed, [(result.email, None)])

    def test_pre_submit_failure_not_consumed(self):
        # promo 配置但字段缺失 → form_mismatch，提交前失败，不消费
        page = FakePage(post_submit="email-verify", fields={"promo": False})
        result, consumed, _ = self._run(page, _base_profile(promo_code="P10"))
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_type, flow.FAIL_FORM)
        self.assertFalse(result.consumed)
        self.assertEqual(consumed, [])

    def test_invitation_missing_field_is_form_failure(self):
        page = FakePage(post_submit="email-verify", fields={"invitation": False})
        result, consumed, _ = self._run(
            page, _base_profile(invitation_code="INV1")
        )
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_type, flow.FAIL_FORM)
        self.assertFalse(result.consumed)

    def test_post_submit_code_timeout_keeps_consumed(self):
        page = FakePage(post_submit="email-verify")

        def no_code(email, **kw):
            raise Exception("OutlookEmail 在 5s 内未收到验证码邮件")

        with mock.patch.object(
            flow._engine, "outlookemail_get_oai_code", side_effect=no_code
        ), mock.patch.object(flow, "CODE_POLL_TIMEOUT", 0.05), \
             mock.patch.object(flow, "CODE_POLL_INTERVAL", 0.01):
            result, consumed, _ = self._run(page, _base_profile())
        self.assertTrue(result.consumed)
        self.assertEqual(len(consumed), 1)
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_type, flow.FAIL_CODE)
        self.assertIn("总轮询窗口", result.failure_reason)
        self.assertIn("最后一次轮询", result.failure_reason)

    def test_cancel_post_submit_keeps_consumed(self):
        """取码阶段被取消：提交边界已消费，mark 必须已写入（consumed 保持）。"""
        page = FakePage(post_submit="email-verify")
        consumed = []

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)

            def fake_acquire(prof):
                email = "acct@example.com"
                engine._freeze_profile_id(email, int(prof["id"]))
                engine._freeze_mailbox_source(email, "accounts")
                return email, "tok"

            def fake_mark(email, *, batch_id="", reason="", profile_id=None, log_callback=None):
                consumed.append((email, profile_id))
                return True

            def cancel_in_code(email, **kw):
                raise engine.RegistrationCancelled()

            with mock.patch.object(engine, "get_registration_repository", return_value=store), \
                 mock.patch.object(flow, "_require_page", return_value=page), \
                 mock.patch.object(flow._engine, "acquire_email", side_effect=fake_acquire), \
                 mock.patch.object(flow._engine, "mark_mailbox_consumed", side_effect=fake_mark), \
                 mock.patch.object(flow._engine, "outlookemail_get_oai_code", side_effect=cancel_in_code), \
                 mock.patch.object(flow._engine, "capture_failure_screenshot", return_value=""), \
                 mock.patch.object(flow, "sleep_with_cancel", lambda *a, **k: None), \
                 mock.patch.object(flow, "raise_if_cancelled", lambda cb=None: None):
                with self.assertRaises(engine.RegistrationCancelled):
                    flow.run_sub2api_registration(
                        _base_profile(), batch_id="b1", acquire=fake_acquire
                    )
        # 消费边界在取码之前写入；flow 不显式传 profile_id，
        # 真实 mark 依赖 acquire 时冻结的 Profile 作用域（fake_mark 收到 None）。
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0][0], "acct@example.com")
        self.assertIsNone(consumed[0][1])
        self.assertEqual(engine._frozen_profile_id("acct@example.com"), 1)
        engine._forget_attempt_context("acct@example.com")

    def test_missing_submit_button_is_form_failure(self):
        # 无提交按钮 → form_mismatch，不消费
        page = FakePage(post_submit="email-verify")

        def no_button(p):
            return None

        with mock.patch.object(flow, "_find_submit_button", side_effect=lambda p: None):
            result, consumed, _ = self._run(page, _base_profile())
        self.assertEqual(result.status, "failure")
        self.assertEqual(result.failure_type, flow.FAIL_FORM)
        self.assertFalse(result.consumed)
        self.assertEqual(consumed, [])

    def test_turnstile_absent_skipped_and_present_invokes_solver(self):
        calls = []

        def fake_solver(log_callback=None, cancel_callback=None, force_reset=False):
            calls.append(1)
            return "y" * 90

        with mock.patch.object(flow, "get_turnstile_token", side_effect=fake_solver):
            # 无 Turnstile
            page = FakePage(post_submit="dashboard", turnstile=False)
            self._run(page, _base_profile())
            self.assertEqual(calls, [])
            # 有 Turnstile
            page2 = FakePage(post_submit="dashboard", turnstile=True)
            self._run(page2, _base_profile())
            self.assertEqual(len(calls), 1)

    def test_credentials_output_format(self):
        page = FakePage(post_submit="dashboard")
        result, _, _ = self._run(page, _base_profile())
        # 交付格式 email----password（sub2api 无 SSO）
        self.assertTrue(result.email)
        self.assertTrue(result.password)
        line = f"{result.email}----{result.password}"
        self.assertIn("@", line)

    def test_profile_id_persisted_for_sub2api(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            profile = _base_profile()

            def fake_acquire(prof):
                email = "acct@example.com"
                engine._freeze_profile_id(email, int(prof["id"]))
                engine._freeze_mailbox_source(email, "accounts")
                return email, "tok"

            with mock.patch.object(engine, "get_registration_repository", return_value=store), \
                 mock.patch.object(flow, "_require_page", return_value=FakePage(post_submit="dashboard")), \
                 mock.patch.object(flow._engine, "acquire_email", side_effect=fake_acquire), \
                 mock.patch.object(flow._engine, "capture_failure_screenshot", return_value=""), \
                 mock.patch.object(flow, "sleep_with_cancel", lambda *a, **k: None), \
                 mock.patch.object(flow, "raise_if_cancelled", lambda cb=None: None):
                # 真实 mark_mailbox_consumed 路径（写真实账本，profile_id=1）
                engine._forget_attempt_context("acct@example.com")
                res = flow.run_sub2api_registration(profile, batch_id="b1", acquire=fake_acquire)
                self.assertTrue(res.consumed)
                # 账本行落在冻结的 Profile 1 作用域
                self.assertTrue(
                    store.is_mailbox_consumed_any_source("acct@example.com", profile_id=1)
                )
                # 直接走 engine 分发持久化（任务 runner 传入 Profile 快照的 id）
                rid = engine.persist_registration_result(
                    batch_id="b1",
                    source="web",
                    started_at=0,
                    email=res.email,
                    password=res.password,
                    status=res.status,
                    consumed_at="2026-08-24 00:00:00" if res.consumed else "",
                    extra={},
                    profile_id=1,
                )
            row = store.get_results_by_ids([rid])[0]
            self.assertEqual(row["profile_id"], 1)
            self.assertEqual(row["mail_status"], "consumed")
            self.assertEqual(row["consumed_at"], "2026-08-24 00:00:00")
            engine._forget_attempt_context("acct@example.com")


class Sub2apiJobDispatchTests(unittest.TestCase):
    """任务分发：Profile 解析 + 启动门禁 + 快照冻结（单业务）。"""

    def setUp(self):
        from backend.web import jobs

        self._jobs = jobs
        # 用全新的协调器实例，避免污染全局单例；
        # _repository 置空：runner 线程不做快照持久化（避免测试 DB 生命周期竞争）
        self.coordinator = jobs.RegistrationJobCoordinator()
        self.coordinator._repository = lambda: None
        self.coordinator.restore_from_database = lambda: None

    def _mock_engine(self, store):
        gr = mock.Mock()
        gr.get_registration_repository.return_value = store
        gr.check_ledger_guard = lambda: {}  # 无账本失败守卫
        gr.check_result_guard = lambda: {}  # 无凭据恢复守卫
        gr.config = {"register_count": 1}
        gr.load_config = lambda: None
        gr._bs = mock.Mock()
        gr._wire_runtime_modules = lambda: None
        gr.new_registration_batch_id = lambda source="web": "batch-test"
        gr.run_sub2api_registration_job = mock.Mock()
        gr.registration_log = lambda m: None
        gr.RegistrationStopController = type("RegistrationStopController", (), {})
        gr.TRACEBACK_LOG_MAX_CHARS = 100
        gr.current_exception_traceback = lambda *a: ""
        return gr

    def test_missing_profile_rejected(self):
        import backend.registration.engine as engine

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            gr = self._mock_engine(store)
            with mock.patch("backend.registration.engine", gr, create=True):
                with self.assertRaises(ValueError) as raised:
                    self.coordinator.start(count=1)
        self.assertIn("必须指定有效 Profile", str(raised.exception))

    def test_sub2api_job_missing_profile_404_path(self):
        import backend.registration.engine as engine

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            gr = self._mock_engine(store)
            with mock.patch("backend.registration.engine", gr, create=True):
                with self.assertRaises(ValueError) as raised:
                    self.coordinator.start(count=1, profile_id=99)
        self.assertIn("不存在", str(raised.exception))

    def test_sub2api_job_disabled_profile_rejected(self):
        import backend.registration.engine as engine

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            profile = store.create_profile(
                {"name": "Disabled", "site_key": "true-sota", "enabled": False}
            )
            gr = self._mock_engine(store)
            with mock.patch("backend.registration.engine", gr, create=True):
                with self.assertRaises(ValueError) as raised:
                    self.coordinator.start(count=1, profile_id=profile["id"])
        self.assertIn("禁用", str(raised.exception))

    def test_sub2api_job_freezes_profile_snapshot_at_start(self):
        import backend.registration.engine as engine
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            profile = store.create_profile(
                {
                    "name": "FreezeMe",
                    "site_key": "true-sota",
                    "promo_code": "OLD",
                }
            )
            started = threading.Event()

            def fake_job(count, snapshot):
                # 任务运行中修改 Profile（改 URL + promo）：不影响运行中任务
                store.update_profile(
                    profile["id"], {"site_key": "ctai", "promo_code": "NEW"}
                )
                fake_job.snapshot = dict(snapshot)
                fake_job.count = count
                started.set()

            gr = self._mock_engine(store)
            gr.run_sub2api_registration_job = fake_job
            with mock.patch("backend.registration.engine", gr, create=True):
                self.coordinator.start(count=1, profile_id=profile["id"])
                assert started.wait(timeout=10)
                # 等 runner 线程完整收尾（含最后一次 _persist_snapshot），
                # 避免 DB 连接/WAL 文件在 temp 清理时仍被持有
                _t = __import__("time")
                deadline = _t.time() + 15
                while _t.time() < deadline and self.coordinator.status().get("running"):
                    _t.sleep(0.1)
                _t.sleep(0.5)
            snapshot = fake_job.snapshot
            self.assertEqual(snapshot["id"], profile["id"])
            self.assertEqual(snapshot["register_url"], "https://true-sota.com/register")
            self.assertEqual(snapshot["promo_code"], "OLD")
            self.assertEqual(snapshot["name"], "FreezeMe")
            # 协调器状态含 Profile 信息
            status = self.coordinator.status()
            self.assertEqual(status["profile_id"], profile["id"])
            self.assertEqual(status["profile_name"], "FreezeMe")


if __name__ == "__main__":
    unittest.main()
