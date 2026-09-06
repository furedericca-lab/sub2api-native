import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.registration.store import RegistrationRepository
from backend.web import application
from backend.web.account_jobs import (
    CANCELLED,
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TIMED_OUT,
    AccountTaskDuplicate,
    AccountTaskNotFound,
    AccountTaskNotTerminal,
    AccountTaskRunner,
    AccountTaskSpec,
)
from backend.integrations.sub2api_account_operations import (
    classify_account_operation_error,
)
from backend.integrations.sub2api_transport import Sub2ApiApiError, Sub2ApiNetworkError

PASSWORD = "super-secret-value"


class FakeSummary:
    @staticmethod
    def as_dict():
        return {"discovered": 2, "synced": 2, "unavailable": 0, "missing": 0}


def wait_on(get_snapshot, *, timeout=8.0, label="task"):
    """轮询直到终态；超时视为失败，绝不让测试静默挂着。"""
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        last = get_snapshot()
        if not last["active"]:
            return last
        time.sleep(0.02)
    raise AssertionError(f"{label} 未在 {timeout}s 内结束: {last}")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.runner = AccountTaskRunner(history_limit=5, log_limit=50)
        self.addCleanup(self.runner.stop)

    def register(self, *, timeout=10.0, run=None, before=None, after=None, classify=None):
        self.runner.register(
            AccountTaskSpec(
                kind="demo",
                run=run or self._default_run,
                timeout_seconds=timeout,
                before=before,
                after=after,
                classify=classify,
            )
        )

    @staticmethod
    def _default_run(ctx, task):
        ctx.log(f"run {task['email']}")
        return {"account_id": 7, "discovered": 1}

    def submit(self, *, email="operator@example.com"):
        return self.runner.submit(
            "demo",
            payload={
                "profile_id": 1,
                "profile_name": "站点",
                "email": email,
                "_secrets": {"password": PASSWORD},
            },
            dedupe_key=f"demo:1:{email}",
        )

    def finish(self, task_id):
        return wait_on(lambda: self.runner.get_task(task_id), label=f"任务 {task_id}")

    def test_submit_returns_immediately_and_completes_in_background(self):
        self.register()
        task = self.submit()
        self.assertEqual(task["status"], QUEUED)
        self.assertEqual(task["email"], "operator@example.com")
        finished = self.finish(task["id"])
        self.assertEqual(finished["status"], SUCCEEDED)
        self.assertEqual(finished["account_id"], 7)
        self.assertEqual(finished["summary"]["discovered"], 1)

    def test_snapshot_and_logs_never_carry_the_password(self):
        self.register()
        task = self.submit()
        finished = self.finish(task["id"])
        detail = self.runner.get_task(task["id"], after_log_id=0)
        for payload in (task, finished, detail):
            self.assertNotIn("_secrets", payload)
            self.assertNotIn("password", payload)
            self.assertNotIn(PASSWORD, repr(payload))

    def test_failure_is_classified_and_stays_visible(self):
        def boom(ctx, task):
            raise Sub2ApiApiError(401, "invalid email or password")

        self.register(run=boom, classify=classify_account_operation_error)
        finished = self.finish(self.submit(email="bad@example.com")["id"])
        self.assertEqual(finished["status"], FAILED)
        self.assertEqual(finished["error_code"], "authentication_failure")
        self.assertIn("invalid email or password", finished["message"])

    def test_discard_removes_a_finished_task_and_its_inputs(self):
        self.register()
        task = self.submit()
        self.finish(task["id"])
        self.assertEqual(
            self.runner.discard(task["id"]), {"id": task["id"], "status": SUCCEEDED}
        )
        self.assertEqual([item["id"] for item in self.runner.list_tasks()], [])
        with self.assertRaises(AccountTaskNotFound):
            self.runner.get_task(task["id"])
        # 任务对象整体丢弃，一次性输入不留在内存里
        self.assertEqual(self.runner._tasks, [])

    def test_discard_rejects_an_active_task(self):
        gate = threading.Event()

        def slow(ctx, task):
            self.assertFalse(gate.is_set())
            gate.wait(5)
            return {}

        self.register(run=slow)
        task = self.submit(email="active@example.com")
        self.finish_first_tick(task["id"])
        with self.assertRaises(AccountTaskNotTerminal):
            self.runner.discard(task["id"])
        gate.set()
        self.finish(task["id"])
        self.assertEqual(self.runner.discard(task["id"])["status"], SUCCEEDED)

    def test_prune_clears_only_the_requested_finished_statuses(self):
        def chooser(ctx, task):
            if task["email"].startswith("bad"):
                raise Sub2ApiApiError(401, "invalid email or password")
            return {"account_id": 3}

        self.register(run=chooser, classify=classify_account_operation_error)
        bad = self.submit(email="bad@example.com")
        self.finish(bad["id"])
        good = self.submit(email="good@example.com")
        self.finish(good["id"])

        self.assertEqual(self.runner.prune({FAILED}), [bad["id"]])
        remaining = {item["id"]: item["status"] for item in self.runner.list_tasks()}
        self.assertEqual(remaining, {good["id"]: SUCCEEDED})

    def finish_first_tick(self, task_id):
        for _ in range(200):
            if self.runner.get_task(task_id)["status"] == RUNNING:
                return
            time.sleep(0.01)
        self.fail("任务未进入执行中")

    def test_duplicate_active_submission_is_rejected(self):
        gate = threading.Event()

        def slow(ctx, task):
            gate.wait(5)
            return {}

        self.register(run=slow)
        first = self.submit(email="busy@example.com")
        with self.assertRaises(AccountTaskDuplicate):
            self.submit(email="busy@example.com")
        gate.set()
        self.finish(first["id"])

    def test_cancel_stops_a_running_task_at_the_next_boundary(self):
        started = threading.Event()

        def looping(ctx, task):
            started.set()
            for _ in range(500):
                ctx.check_cancelled()
                time.sleep(0.01)
            return {}

        self.register(run=looping)
        task = self.submit(email="cancel@example.com")
        self.assertTrue(started.wait(3))
        self.runner.cancel(task["id"])
        finished = self.finish(task["id"])
        self.assertEqual(finished["status"], CANCELLED)
        self.assertEqual(finished["error_code"], "cancelled")

    def test_deadline_marks_task_as_timeout(self):
        def looping(ctx, task):
            for _ in range(500):
                ctx.check_cancelled()
                time.sleep(0.01)
            return {}

        self.register(run=looping, timeout=0.2)
        finished = self.finish(self.submit(email="slow@example.com")["id"])
        self.assertEqual(finished["status"], TIMED_OUT)
        self.assertEqual(finished["error_code"], "timeout")

    def test_runtime_hooks_run_even_when_the_task_fails(self):
        calls = []

        def failing(ctx, task):
            raise RuntimeError("boom")

        self.register(
            run=failing,
            before=lambda ctx: calls.append("before"),
            after=lambda ctx: calls.append("after"),
        )
        self.finish(self.submit(email="hooks@example.com")["id"])
        self.assertEqual(calls, ["before", "after"])

    def test_retry_creates_a_new_task_with_the_same_input(self):
        self.register()
        first = self.finish(self.submit(email="retry@example.com")["id"])
        retried = self.runner.retry(first["id"])
        second = self.finish(retried["id"])
        self.assertEqual(second["email"], first["email"])
        self.assertEqual(second["retried_from"], first["id"])
        self.assertNotEqual(second["id"], first["id"])

    def test_retry_requires_a_finished_task_and_known_id(self):
        gate = threading.Event()

        def slow(ctx, task):
            gate.wait(5)
            return {}

        self.register(run=slow)
        task = self.submit(email="pending@example.com")
        with self.assertRaises(AccountTaskDuplicate):
            self.runner.retry(task["id"])
        gate.set()
        with self.assertRaises(AccountTaskNotFound):
            self.runner.retry(424242)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(Exception):
            self.runner.submit("nope", payload={})


class IntakeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_root = Path(self.tmp.name) / "data"
        self.store = RegistrationRepository(
            self.data_root / "accounts" / "registration_results.sqlite3"
        )
        self.profile = self.store.create_profile(
            {"name": "TrueSOTA", "site_key": "true-sota"}
        )

        gr = mock.Mock()
        gr.get_registration_repository.return_value = self.store
        gr.get_proxies.return_value = {}
        gr.load_config.return_value = None
        gr._wire_runtime_modules.return_value = None

        for patch in (
            mock.patch.object(application, "DATA_DIR", self.data_root),
            mock.patch.object(application, "_gr", return_value=gr),
            mock.patch.object(application, "_valid_session", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        os.environ["SUB2API_ACCOUNT_INTAKE_TIMEOUT_SECONDS"] = "30"
        self.addCleanup(
            os.environ.pop, "SUB2API_ACCOUNT_INTAKE_TIMEOUT_SECONDS", None
        )
        application._account_remote_guard = threading.Lock()

        self.client = TestClient(application.create_app())
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------ helper

    def fake_service(self, *, account=None, error=None, blocker=None):
        service = mock.Mock()
        if error is not None:
            service.add_account.side_effect = error
        else:
            created = account or self.store.create_account(
                self.profile["id"], "manual@example.com", PASSWORD, "manual"
            )

            def add_account(profile_id, email, password, *, progress=None):
                if blocker is not None:
                    self.assertTrue(blocker.wait(5))
                if progress is not None:
                    progress.stage("启动浏览器并过验证码登录")
                return created, FakeSummary()

            service.add_account = mock.Mock(side_effect=add_account)
        patcher = mock.patch(
            "backend.integrations.sub2api_account_operations.AccountOperationsService",
            return_value=service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    def submit(self, email="manual@example.com", password=PASSWORD, profile_id=None):
        return self.client.post(
            "/api/account-pool",
            json={
                "profile_id": profile_id or self.profile["id"],
                "email": email,
                "password": password,
            },
        )

    def finish(self, task_id):
        return wait_on(
            lambda: self.client.get(f"/api/account-pool/tasks/{task_id}").json()["task"],
            label=f"任务 {task_id}",
        )

    def test_discard_endpoint_clears_a_finished_card(self):
        self.fake_service()
        task = self.submit("clean@example.com").json()["task"]
        self.finish(task["id"])
        removed = self.client.delete(f"/api/account-pool/tasks/{task['id']}")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(
            [item["id"] for item in self.client.get("/api/account-pool/tasks").json()["tasks"]],
            [],
        )
        self.assertEqual(
            self.client.get(f"/api/account-pool/tasks/{task['id']}").status_code, 404
        )

    def test_discard_endpoint_refuses_an_active_task(self):
        gate = threading.Event()
        self.fake_service(blocker=gate)
        task = self.submit("busy@example.com").json()["task"]
        blocked = self.client.delete(f"/api/account-pool/tasks/{task['id']}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("尚未结束", blocked.json()["detail"])
        gate.set()
        self.finish(task["id"])
        self.assertEqual(
            self.client.delete(f"/api/account-pool/tasks/{task['id']}").status_code, 200
        )

    def test_prune_endpoint_keeps_success_and_clears_failures(self):
        created = self.store.create_account(
            self.profile["id"], "ok@example.com", PASSWORD, "manual"
        )
        service = mock.Mock()

        def add_account(profile_id, email, password, *, progress=None):
            if email == "failed@example.com":
                raise Sub2ApiApiError(401, "invalid email or password")
            return created, FakeSummary()

        service.add_account = mock.Mock(side_effect=add_account)
        patcher = mock.patch(
            "backend.integrations.sub2api_account_operations.AccountOperationsService",
            return_value=service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        failed = self.submit("failed@example.com").json()["task"]
        self.finish(failed["id"])
        ok = self.submit("ok@example.com").json()["task"]
        self.finish(ok["id"])

        pruned = self.client.post("/api/account-pool/tasks/prune")
        self.assertEqual(pruned.status_code, 200, pruned.text)
        self.assertEqual(pruned.json()["removed"], [failed["id"]])
        self.assertEqual(
            [item["id"] for item in self.client.get("/api/account-pool/tasks").json()["tasks"]],
            [ok["id"]],
        )

    def accounts_for(self, email):
        return [
            item
            for item in self.client.get("/api/account-pool").json()["accounts"]
            if item["email"] == email
        ]

    # ------------------------------------------------------------------- tests

    def test_create_returns_accepted_without_waiting_for_the_remote_work(self):
        self.fake_service()
        started = time.time()
        response = self.submit()
        elapsed = time.time() - started

        self.assertEqual(response.status_code, 202, response.text)
        self.assertLess(elapsed, 2.0)
        task = response.json()["task"]
        self.assertTrue(task["active"])
        self.assertNotIn("password", response.text)
        self.assertNotIn(PASSWORD, response.text)

        finished = self.finish(task["id"])
        self.assertEqual(finished["status"], SUCCEEDED, finished)
        self.assertEqual(finished["summary"]["synced"], 2)
        self.assertEqual(self.accounts_for("manual@example.com")[0]["id"], finished["account_id"])

    def test_authentication_failure_is_a_typed_terminal_error_not_a_generic_one(self):
        self.fake_service(error=Sub2ApiApiError(401, "invalid email or password"))
        response = self.submit(email="wrong@example.com")
        task = self.finish(response.json()["task"]["id"])

        self.assertEqual(task["status"], FAILED)
        self.assertEqual(task["error_code"], "authentication_failure")
        self.assertIn("invalid email or password", task["message"])
        self.assertEqual(self.accounts_for("wrong@example.com"), [])

    def test_upstream_unreachable_is_distinguishable_from_auth_failure(self):
        self.fake_service(error=Sub2ApiNetworkError("上游请求超时或网络不可达"))
        response = self.submit(email="offline@example.com")
        task = self.finish(response.json()["task"]["id"])
        self.assertEqual(task["error_code"], "upstream_unreachable")

    def test_second_submission_for_the_same_email_conflicts(self):
        gate = threading.Event()
        self.fake_service(blocker=gate)
        first = self.submit(email="busy@example.com")
        self.assertEqual(first.status_code, 202, first.text)
        second = self.submit(email="busy@example.com")
        self.assertEqual(second.status_code, 409, second.text)
        gate.set()
        self.finish(first.json()["task"]["id"])

    def test_task_detail_streams_logs_with_a_cursor(self):
        self.fake_service()
        response = self.submit(email="logs@example.com")
        task_id = response.json()["task"]["id"]
        self.finish(task_id)

        detail = self.client.get(f"/api/account-pool/tasks/{task_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        task = detail.json()["task"]
        self.assertGreater(task["log_cursor"], 0)
        self.assertTrue(any("[stage]" in item["message"] for item in task["logs"]))
        self.assertNotIn("password", detail.text)
        self.assertNotIn(PASSWORD, detail.text)

        tail = self.client.get(f"/api/account-pool/tasks/{task_id}?after_log_id=0")
        self.assertEqual(tail.status_code, 200, tail.text)

    def test_retry_requeues_the_same_input(self):
        self.fake_service(error=Sub2ApiApiError(401, "invalid email or password"))
        task = self.finish(self.submit(email="retry@example.com").json()["task"]["id"])
        self.assertEqual(task["error_code"], "authentication_failure")

        created = self.store.create_account(
            self.profile["id"], "retry@example.com", PASSWORD, "manual"
        )
        self.fake_service(account=created)
        retried = self.client.post(f"/api/account-pool/tasks/{task['id']}/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        second = self.finish(retried.json()["task"]["id"])
        self.assertEqual(second["status"], SUCCEEDED)
        self.assertEqual(second["email"], "retry@example.com")
        self.assertEqual(second["retried_from"], task["id"])

    def test_cancel_endpoint_stops_a_task_that_is_waiting_for_the_channel(self):
        holder = threading.Lock()
        self.assertTrue(holder.acquire(blocking=False))
        previous_guard = application._account_remote_guard
        application._account_remote_guard = holder
        try:
            response = self.submit(email="queued@example.com")
            self.assertEqual(response.status_code, 202, response.text)
            task_id = response.json()["task"]["id"]
            queued = self.client.get(f"/api/account-pool/tasks/{task_id}").json()["task"]
            self.assertTrue(queued["active"])
            cancelled = self.client.post(f"/api/account-pool/tasks/{task_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
        finally:
            application._account_remote_guard = previous_guard
            holder.release()
        finished = self.finish(task_id)
        self.assertEqual(finished["status"], CANCELLED)

    def test_profile_validation_fails_before_a_task_is_created(self):
        missing = self.submit(profile_id=999)
        self.assertEqual(missing.status_code, 404, missing.text)

        self.store.update_profile(self.profile["id"], {"enabled": False})
        disabled = self.submit(email="disabled@example.com")
        self.assertEqual(disabled.status_code, 422, disabled.text)
        self.assertEqual(self.client.get("/api/account-pool/tasks").json()["tasks"], [])

    def test_task_routes_are_not_swallowed_by_account_id_routes(self):
        paths = [getattr(route, "path", "") for route in self.client.app.routes]
        self.assertIn("/api/account-pool/tasks", paths)
        self.assertLess(
            paths.index("/api/account-pool/tasks"),
            paths.index("/api/account-pool/{account_id}"),
        )
        self.assertEqual(self.client.get("/api/account-pool/tasks").status_code, 200)
        self.assertEqual(self.client.get("/api/account-pool/tasks/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
