import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.registration.store import RegistrationRepository, normalize_profile_id


LEGACY_SCHEMA = """
CREATE TABLE registration_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT UNIQUE,
    batch_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'gui',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL DEFAULT 0,
    email TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'failure',
    success INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    worker_id INTEGER NOT NULL DEFAULT 0,
    extra_json TEXT NOT NULL DEFAULT '{}'
);
PRAGMA user_version = 5;
"""


def _make_v8_database(path) -> None:
    """构造一个旧版 v8 形态的 DB fixture（fail-closed 测试用）。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE registration_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT UNIQUE,
                batch_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'web',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                email TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                registration_status TEXT NOT NULL DEFAULT 'failure',
                success INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'outlookemail',
                worker_id INTEGER NOT NULL DEFAULT 0,
                failure_type TEXT NOT NULL DEFAULT '',
                registration_error TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL DEFAULT '',
                account_file TEXT NOT NULL DEFAULT '',
                sso_saved INTEGER NOT NULL DEFAULT 0,
                mail_account_id TEXT NOT NULL DEFAULT '',
                mail_status TEXT NOT NULL DEFAULT 'not_attempted',
                mail_disabled_at TEXT NOT NULL DEFAULT '',
                mail_error TEXT NOT NULL DEFAULT '',
                cpa_enabled INTEGER NOT NULL DEFAULT 0,
                cpa_status TEXT NOT NULL DEFAULT 'disabled',
                cpa_error TEXT NOT NULL DEFAULT '',
                auth_info TEXT NOT NULL DEFAULT '',
                auth_path TEXT NOT NULL DEFAULT '',
                cpa_auth_path TEXT NOT NULL DEFAULT '',
                cpa_record_json TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'skipped',
                delivery_imported_at TEXT NOT NULL DEFAULT '',
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                delivery_error TEXT NOT NULL DEFAULT '',
                bot_risk INTEGER NOT NULL DEFAULT 0,
                bfs TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE mailbox_consumptions (
                source TEXT NOT NULL DEFAULT 'accounts',
                email TEXT NOT NULL COLLATE NOCASE,
                consumed_at TEXT NOT NULL,
                batch_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source, email)
            );
            CREATE TABLE registration_job_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                batch_id TEXT NOT NULL DEFAULT '',
                running INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                finished_at REAL,
                target_count INTEGER NOT NULL DEFAULT 0,
                workers INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'web',
                last_error TEXT NOT NULL DEFAULT '',
                completed_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT NOT NULL DEFAULT '',
                current_email TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            PRAGMA user_version = 8;
            """
        )
        conn.commit()
    finally:
        conn.close()


class RegistrationRepositorySchemaTests(unittest.TestCase):
    def test_legacy_database_fails_loudly_with_clear_error(self):
        """旧版 schema 不再自动改名：直接报错并要求手动处理。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(LEGACY_SCHEMA)
                conn.commit()

            with self.assertRaises(RuntimeError) as raised:
                RegistrationRepository(path)
            message = str(raised.exception)
            self.assertIn("非 sub2api-native", message)
            self.assertIn("手动删除或改名", message)
            # 旧文件保持原样，未被改动
            self.assertTrue(path.exists())
            self.assertFalse(list(Path(tmp).glob("results.sqlite3.bak-*")))

    def test_legacy_v8_database_is_rejected(self):
        """旧版 v8 数据库 → fail-closed（不迁移、不自动改写）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            _make_v8_database(path)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    """INSERT INTO registration_results
                       (email, password, registration_status, success,
                        started_at, finished_at)
                       VALUES (?, ?, 'success', 1,
                               '2026-07-01 00:00:00', '2026-07-01 00:00:00')""",
                    ("legacy@example.test", "pw"),
                )
                conn.execute(
                    """INSERT INTO mailbox_consumptions (source, email, consumed_at)
                       VALUES ('accounts', 'legacy@example.test', '2026-07-01 00:01:00')"""
                )
                conn.commit()
            with self.assertRaises(RuntimeError) as raised:
                RegistrationRepository(path)
            message = str(raised.exception)
            self.assertIn("user_version=8", message)
            self.assertIn("手动删除或改名", message)

    def test_fresh_database_creates_v2_schema_and_supports_core_flows(self):
        """全新 DB 直接建 v2（三级资源池模型）并支撑核心读写流。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            store = RegistrationRepository(path)
            with closing(sqlite3.connect(path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(registration_results)")}
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 2)
            for column in ("profile_id", "registration_status", "mail_status", "consumed_at"):
                self.assertIn(column, columns)
            # 单业务模型：不再有 business_key 列
            self.assertNotIn("business_key", columns)

            store.add_result(
                {
                    "profile_id": 1,
                    "email": "consumed@outlook.com",
                    "status": "success",
                    "provider": "outlookemail",
                    "mail_status": "consumed",
                    "consumed_at": "2026-08-01 01:02:03",
                    "screenshot_path": "/tmp/failure.png",
                }
            )
            store.add_result(
                {
                    "profile_id": 2,
                    "email": "released@outlook.com",
                    "status": "failure",
                    "provider": "outlookemail",
                    "failure_type": "domain_rejected",
                    "failure_reason": "fixture error",
                }
            )

            filtered = store.list_results(mail_status="consumed")
            self.assertEqual([row["email"] for row in filtered], ["consumed@outlook.com"])
            self.assertEqual(store.count_results(), 2)
            self.assertEqual(store.count_results(profile_id=1), 1)
            # 消费统计以账本为真相源：先在账本登记同一邮箱（Profile 作用域）
            store.mark_mailbox_consumed(1, "accounts", "consumed@outlook.com", reason="fixture")
            stats = store.stats()
            self.assertEqual(stats["mailbox_consumed"], 1)
            self.assertEqual(stats["orphan_consumptions"], 0)
            consumed = next(row for row in store.list_results() if row["email"] == "consumed@outlook.com")
            self.assertEqual(consumed["screenshot_path"], "/tmp/failure.png")
            self.assertEqual(consumed["profile_id"], 1)

    def test_native_v1_database_migrates_to_v2_with_assets_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            store = RegistrationRepository(path)
            profile = store.create_profile({"name": "Migration", "site_key": "bmapi"})
            result_id = store.add_result(
                {
                    "profile_id": profile["id"],
                    "email": "migration@example.com",
                    "password": "password-1",
                    "status": "success",
                }
            )
            account = store.account_for_result(result_id)
            key_id = store.upsert_account_key(
                account["id"], 91, "codex-relay", "ciphertext", 2, "active"
            )
            store.set_relay_key(account["id"], key_id)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA user_version = 1")
                conn.commit()

            migrated = RegistrationRepository(path)
            migrated_account = migrated.account_for_result(result_id)
            self.assertEqual(migrated_account["id"], account["id"])
            self.assertEqual(migrated_account["relay_key_id"], key_id)
            self.assertEqual(migrated.list_account_keys(account["id"])[0]["id"], key_id)

            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                account_targets = {
                    row[2] for row in conn.execute("PRAGMA foreign_key_list(accounts)")
                }
                key_targets = {
                    row[2]
                    for row in conn.execute("PRAGMA foreign_key_list(account_api_keys)")
                }
                self.assertEqual(
                    account_targets, {"sub2api_profiles", "account_api_keys"}
                )
                self.assertEqual(key_targets, {"accounts"})
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

            with self.assertRaises(sqlite3.IntegrityError):
                with migrated._connect() as conn:
                    conn.execute(
                        """INSERT INTO accounts(
                               profile_id,email,created_at,updated_at)
                           VALUES(999999,'orphan@example.com','now','now')"""
                    )

    def test_pagination_filters_and_large_id_batches_share_consistent_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            for index in range(5):
                store.add_result(
                    {
                        "profile_id": 1,
                        "email": f"user-{index}@example.com",
                        "status": "success" if index < 4 else "failure",
                        "provider": "fixture",
                        "finished_at": f"2026-08-04 00:00:0{index}",
                    }
                )

            self.assertEqual(
                store.count_results(status="success", keyword="user-", profile_id=1), 4
            )
            page = store.list_results(
                status="success",
                keyword="user-",
                profile_id=1,
                limit=2,
                offset=2,
            )
            self.assertEqual(
                [row["email"] for row in page],
                ["user-1@example.com", "user-0@example.com"],
            )
            records = store.get_results_by_ids(range(1, 1006))
            self.assertEqual([row["id"] for row in records], [1, 2, 3, 4, 5])
            self.assertEqual(len(store.delete_results(range(1, 1006))), 5)
            self.assertEqual(store.count_results(), 0)

    def test_list_result_ids_matches_filters_and_list_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            first = store.add_result(
                {"profile_id": 1, "email": "first@example.com", "status": "success", "provider": "fixture"}
            )
            second = store.add_result(
                {"profile_id": 1, "email": "second@example.com", "status": "failure", "provider": "fixture"}
            )
            third = store.add_result(
                {"profile_id": 2, "email": "third@example.com", "status": "success", "provider": "other"}
            )

            expected = [
                row["id"]
                for row in store.list_results(status="success", keyword="fixture", profile_id=1)
            ]
            self.assertEqual(
                store.list_result_ids(status="success", keyword="fixture", profile_id=1), expected
            )
            self.assertEqual(expected, [first])
            self.assertNotIn(second, expected)
            self.assertNotIn(third, expected)

    def test_registration_risk_email_is_treated_as_consumed(self):
        """历史 registration_results 的消费兼容语义保持不变（Profile 作用域）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "profile_id": 1,
                    "email": "risk@outlook.com",
                    "status": "failure",
                    "failure_type": "registration_risk",
                    "failure_reason": "注册风控拒绝",
                }
            )
            store.add_result(
                {
                    "profile_id": 1,
                    "email": "already@outlook.com",
                    "status": "failure",
                    "failure_type": "already_registered",
                    "failure_reason": "邮箱已注册",
                }
            )
            store.add_result(
                {
                    "profile_id": 1,
                    "email": "timeout@outlook.com",
                    "status": "failure",
                    "failure_type": "code_timeout",
                    "failure_reason": "未收到验证码",
                }
            )

            self.assertTrue(store.has_registered_or_consumed("risk@outlook.com", profile_id=1))
            self.assertTrue(store.has_registered_or_consumed("already@outlook.com", profile_id=1))
            self.assertFalse(store.has_registered_or_consumed("timeout@outlook.com", profile_id=1))


class MailboxConsumptionLedgerTests(unittest.TestCase):
    """消费账本：remote active ≠ registration available 的持久化硬边界。

    主键 (profile_id, email)：同一 Profile 内一个邮箱只消费一次；
    同一邮箱可在不同 Profile 各消费一次。
    """

    def test_mark_and_is_consumed_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            self.assertFalse(store.is_mailbox_consumed(1, "accounts", "user@outlook.com"))
            self.assertTrue(
                store.mark_mailbox_consumed(
                    1, "accounts", "user@outlook.com", batch_id="b1", reason="已提交注册"
                )
            )
            self.assertTrue(store.is_mailbox_consumed(1, "accounts", "user@outlook.com"))
            self.assertTrue(
                store.is_mailbox_consumed(1, "accounts", "USER@OUTLOOK.COM")
            )  # NOCASE
            # 幂等：重复标记返回 False，不产生重复行
            self.assertFalse(
                store.mark_mailbox_consumed(1, "accounts", "user@outlook.com", batch_id="b2")
            )

    def test_release_consumptions_makes_email_reusable(self):
        """人工释放：删除消费标记后邮箱可重新标记（应对确认未建号的失败）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            self.assertTrue(
                store.mark_mailbox_consumed(1, "accounts", "burned@qq.com", batch_id="b1")
            )
            self.assertTrue(store.is_mailbox_consumed(1, "accounts", "burned@qq.com"))
            # 释放（大小写不敏感），返回库内原样邮箱
            released = store.release_consumptions(
                ["BURNED@QQ.com", "other@qq.com"], profile_id=1
            )
            self.assertEqual(released, ["burned@qq.com"])
            self.assertFalse(store.is_mailbox_consumed(1, "accounts", "burned@qq.com"))
            # 释放后可重新消费；幂等释放不抛错
            self.assertTrue(
                store.mark_mailbox_consumed(1, "accounts", "burned@qq.com", batch_id="b2")
            )
            self.assertEqual(store.release_consumptions([], profile_id=1), [])

    def test_source_is_audit_metadata_only(self):
        """source 仅是首次消费来源的审计元数据，不构成可注册性的命名空间。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            self.assertTrue(store.mark_mailbox_consumed(1, "accounts", "shared@example.com"))
            # 第二次用不同 source 标记同 email：拒绝，不产生第二条记录
            self.assertFalse(store.mark_mailbox_consumed(1, "temp", "shared@example.com"))
            store.mark_mailbox_consumed(1, "accounts", "shared@example.com")  # 同 source 幂等
            with closing(sqlite3.connect(Path(tmp) / "results.sqlite3")) as conn:
                rows = conn.execute(
                    "SELECT source, email FROM mailbox_consumptions WHERE email COLLATE NOCASE = ?",
                    ("shared@example.com",),
                ).fetchall()
            # 账本身份总数仍为 1，首次 source 保持 accounts
            self.assertEqual(rows, [("accounts", "shared@example.com")])

    def test_ledger_survives_reconnect(self):
        """durable：同一 DB 文件重新打开后消费记录仍然生效（进程重启等价）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            RegistrationRepository(path).mark_mailbox_consumed(
                1, "accounts", "durable@outlook.com", reason="restart-test"
            )
            reopened = RegistrationRepository(path)
            self.assertTrue(
                reopened.is_mailbox_consumed(1, "accounts", "durable@outlook.com")
            )

    def test_empty_email_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            self.assertFalse(store.mark_mailbox_consumed(1, "accounts", ""))
            self.assertFalse(store.is_mailbox_consumed(1, "accounts", ""))

    def test_consumption_is_global_across_sources(self):
        """一个 email 在一个 Profile 内就是一个身份：accounts 消费过的邮箱在 temp 中同样不可复用。

        source 仅保留为审计元数据；availability 判断必须跨来源生效。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.mark_mailbox_consumed(1, "accounts", "Shared@Example.com", reason="audit")
            # 任意来源查询都命中（NOCASE）
            self.assertTrue(store.is_mailbox_consumed_any_source("shared@example.com", profile_id=1))
            self.assertTrue(store.is_mailbox_consumed_any_source("SHARED@example.com", profile_id=1))
            # 未消费邮箱不命中
            self.assertFalse(store.is_mailbox_consumed_any_source("other@example.com", profile_id=1))
            self.assertFalse(store.is_mailbox_consumed_any_source("", profile_id=1))

    def test_has_registered_or_consumed_includes_new_consumed_status(self):
        """旧记录 fallback：mail_status='consumed' 的历史行同样禁止复用。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "profile_id": 1,
                    "batch_id": "b1",
                    "source": "test",
                    "email": "legacy@outlook.com",
                    "registration_status": "failure",
                    "success": 0,
                    "failure_type": "code_timeout",
                    "mail_status": "consumed",
                }
            )
            self.assertTrue(store.has_registered_or_consumed("legacy@outlook.com", profile_id=1))
            self.assertFalse(store.has_registered_or_consumed("fresh@outlook.com", profile_id=1))

    def test_concurrent_cross_source_marks_yield_single_row(self):
        """并发 mark 同一 email（不同 source）最终只能有一行（BEGIN IMMEDIATE 临界区）。"""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            store = RegistrationRepository(path)
            results: list[bool] = []
            lock = threading.Lock()
            barrier = threading.Barrier(4)

            def worker(source_name):
                barrier.wait()
                first = store.mark_mailbox_consumed(1, source_name, "race@outlook.com")
                with lock:
                    results.append(first)

            threads = [
                threading.Thread(target=worker, args=(name,))
                for name in ("accounts", "temp", "accounts", "temp")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(results.count(True), 1)
            with closing(sqlite3.connect(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM mailbox_consumptions WHERE email COLLATE NOCASE = ?",
                    ("race@outlook.com",),
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_concurrent_same_profile_email_marks_yield_single_row(self):
        """并发 mark 同一 (profile_id, email) 最终只能有一行；跨 Profile 互不干扰。"""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            store = RegistrationRepository(path)
            results: list[bool] = []
            lock = threading.Lock()
            barrier = threading.Barrier(4)

            def worker(source_name):
                barrier.wait()
                first = store.mark_mailbox_consumed(
                    1, source_name, "race2@outlook.com"
                )
                with lock:
                    results.append(first)

            threads = [
                threading.Thread(target=worker, args=(name,))
                for name in ("accounts", "temp", "accounts", "temp")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(results.count(True), 1)
            with closing(sqlite3.connect(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM mailbox_consumptions "
                    "WHERE profile_id = ? AND email COLLATE NOCASE = ?",
                    (1, "race2@outlook.com"),
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_v1_ledger_cannot_hold_cross_source_duplicates(self):
        """v1 账本 PK (profile_id, email)：同 Profile 内同邮箱物理上只剩一行。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            self.assertTrue(store.mark_mailbox_consumed(1, "accounts", "Dup@Example.com"))
            # 同 Profile 第二次（无论 source）必为 False，不产生第二行
            self.assertFalse(store.mark_mailbox_consumed(1, "temp", "dup@example.com"))
            with closing(sqlite3.connect(Path(tmp) / "results.sqlite3")) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM mailbox_consumptions WHERE email COLLATE NOCASE = ?",
                    ("dup@example.com",),
                ).fetchone()[0]
            self.assertEqual(count, 1)
            stats = store.stats()
            self.assertEqual(stats["mailbox_consumed"], 1)
            self.assertEqual(stats["orphan_consumptions"], 1)

    def test_stats_count_consumptions_from_ledger(self):
        """消费统计以账本为真相源：无 registration_result 行也计数，并报告 orphan。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            # submit 后 crash：只有账本行，没有注册结果行
            store.mark_mailbox_consumed(1, "accounts", "orphan@outlook.com", reason="crash")
            stats = store.stats()
            self.assertEqual(stats["mailbox_consumed"], 1)
            self.assertEqual(stats["orphan_consumptions"], 1)
            # 有对应结果行的消费不算 orphan
            store.mark_mailbox_consumed(1, "accounts", "matched@outlook.com", reason="ok")
            store.add_result(
                {
                    "profile_id": 1,
                    "batch_id": "b1",
                    "source": "test",
                    "email": "matched@outlook.com",
                    "registration_status": "success",
                    "success": 1,
                    "mail_status": "consumed",
                }
            )
            stats = store.stats()
            self.assertEqual(stats["mailbox_consumed"], 2)
            self.assertEqual(stats["orphan_consumptions"], 1)
            # 删除结果行不影响消费统计（账本独立持久）
            store.delete_results(store.list_result_ids(keyword="matched@outlook.com"))
            stats = store.stats()
            self.assertEqual(stats["mailbox_consumed"], 2)


class ReleaseSafetyTests(unittest.TestCase):
    """释放判定必须看同 Profile 作用域全部历史记录，不只选中的行。"""

    def _store(self, tmp):
        return RegistrationRepository(Path(tmp) / "results.sqlite3")

    def test_selected_failure_but_sibling_success_blocks_release(self):
        """A：同邮箱选中 failure 行，但同 Profile 另存在 success 记录 → 拒绝释放。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_result(
                {
                    "profile_id": 1,
                    "email": "mixed@outlook.com",
                    "status": "success",
                    "batch_id": "b1",
                }
            )
            failure_id = store.add_result(
                {"profile_id": 1, "email": "mixed@outlook.com", "status": "failure", "batch_id": "b2"}
            )
            store.mark_mailbox_consumed(1, "accounts", "mixed@outlook.com")
            blocked = store.can_release_consumption(["mixed@outlook.com"], profile_id=1)
            self.assertIn("mixed@outlook.com", blocked)
            self.assertIn("成功", blocked["mixed@outlook.com"])

            # 端到端语义：即使只删除选中的 failure 行，释放也必须被拒。
            records = store.get_results_by_ids([failure_id])
            self.assertEqual(records[0]["registration_status"], "failure")
            self.assertTrue(
                store.can_release_consumption([records[0]["email"]], profile_id=1)
            )

    def test_failure_only_email_releases_successfully(self):
        """B：仅存在失败记录的邮箱 → 允许释放，账本清除后可重新消费。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_result({"profile_id": 1, "email": "failed@qq.com", "status": "failure"})
            store.mark_mailbox_consumed(1, "accounts", "failed@qq.com")
            self.assertEqual(
                store.can_release_consumption(["failed@qq.com"], profile_id=1), {}
            )
            released = store.release_consumptions(["FAILED@qq.com"], profile_id=1)
            self.assertEqual(released, ["failed@qq.com"])
            self.assertFalse(store.is_mailbox_consumed(1, "accounts", "failed@qq.com"))

    def test_success_record_blocks_release(self):
        """success 记录单独存在时同样禁止释放（站点账号真实存在）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_result(
                {
                    "profile_id": 1,
                    "email": "ok@qq.com",
                    "status": "success",
                }
            )
            blocked = store.can_release_consumption(["ok@qq.com"], profile_id=1)
            self.assertIn("ok@qq.com", blocked)
            self.assertIn("成功", blocked["ok@qq.com"])

    def test_empty_and_unknown_emails_are_releasable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertEqual(store.can_release_consumption([], profile_id=1), {})
            self.assertEqual(
                store.can_release_consumption(["never-seen@qq.com"], profile_id=1), {}
            )

    def test_consumption_sources_reads_ledger_audit_metadata(self):
        """去重列出该邮箱在 Profile 作用域内的全部历史审计来源（排序稳定）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.mark_mailbox_consumed(1, "temp", "src@qq.com")
            self.assertEqual(store.consumption_sources("src@qq.com", profile_id=1), ["temp"])
            self.assertEqual(store.consumption_sources("missing@qq.com", profile_id=1), [])
            self.assertEqual(store.consumption_sources("", profile_id=1), [])

    def test_consumption_sources_scoped_per_profile(self):
        """同邮箱在不同 Profile 作用域可各有消费行；查询严格按作用域。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.mark_mailbox_consumed(2, "temp", "multi@qq.com")  # Profile 2 作用域
            store.mark_mailbox_consumed(1, "accounts", "multi@qq.com")
            self.assertEqual(
                store.consumption_sources("multi@qq.com", profile_id=1), ["accounts"]
            )
            self.assertEqual(
                store.consumption_sources("multi@qq.com", profile_id=2), ["temp"]
            )
            self.assertEqual(store.consumption_sources("multi@qq.com", profile_id=3), [])


class ProfileScopedLedgerTests(unittest.TestCase):
    """v1 核心语义：一个 email 在一个 Profile 内就是一个身份，跨 Profile 互不阻塞。"""

    def _store(self, tmp):
        return RegistrationRepository(Path(tmp) / "results.sqlite3")

    def test_same_email_markable_once_per_profile(self):
        """同邮箱在 Profile 1 / 2 / 3 各可消费一次，各自第二次拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            email = "shared@example.com"
            for profile_id in (1, 2, 3):
                self.assertTrue(store.mark_mailbox_consumed(profile_id, "accounts", email))
                self.assertTrue(
                    store.is_mailbox_consumed_any_source(email, profile_id=profile_id)
                )
            for profile_id in (1, 2, 3):
                self.assertFalse(
                    store.mark_mailbox_consumed(profile_id, "temp", email)
                )
            with closing(sqlite3.connect(Path(tmp) / "results.sqlite3")) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM mailbox_consumptions WHERE email = ? COLLATE NOCASE",
                    (email,),
                ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_same_profile_accounts_then_temp_refuses(self):
        """同一 Profile 内 accounts 消费后，temp 再标记同邮箱 → False（source 降为审计元数据）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertTrue(store.mark_mailbox_consumed(1, "accounts", "once@example.com"))
            self.assertFalse(store.mark_mailbox_consumed(1, "temp", "once@example.com"))
            # 首条 source 保持 accounts（审计）
            self.assertEqual(
                store.consumption_sources("once@example.com", profile_id=1), ["accounts"]
            )

    def test_release_is_scoped_to_profile(self):
        """释放只删本 Profile 账本行：释放 Profile 1 后 Profile 2 / 3 行保持。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            email = "scope@example.com"
            store.mark_mailbox_consumed(2, "accounts", email)
            store.mark_mailbox_consumed(1, "temp", email)
            released = store.release_consumptions([email], profile_id=1)
            self.assertEqual(released, [email])
            self.assertFalse(store.is_mailbox_consumed_any_source(email, profile_id=1))
            self.assertTrue(store.is_mailbox_consumed_any_source(email, profile_id=2))
            # 从未在 Profile 3 消费过
            self.assertFalse(store.is_mailbox_consumed_any_source(email, profile_id=3))

    def test_success_does_not_block_other_profile_scope(self):
        """跨 Profile 隔离：Profile 1 成功记录不阻塞 Profile 2 作用域的重判。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_result({"profile_id": 1, "email": "iso@example.com", "status": "success"})
            self.assertTrue(store.has_success("iso@example.com", profile_id=1))
            self.assertFalse(store.has_success("iso@example.com", profile_id=2))
            self.assertTrue(
                store.has_registered_or_consumed("iso@example.com", profile_id=1)
            )
            self.assertFalse(
                store.has_registered_or_consumed("iso@example.com", profile_id=2)
            )

    def test_result_queries_support_profile_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.add_result({"profile_id": 1, "email": "a@x.com", "status": "success"})
            store.add_result({"profile_id": 7, "email": "a@x.com", "status": "success"})
            self.assertEqual(store.count_results(), 2)
            self.assertEqual(store.count_results(profile_id=""), 2)
            self.assertEqual(store.count_results(profile_id=1), 1)
            self.assertEqual(store.count_results(profile_id=7), 1)
            ids = store.list_result_ids(profile_id=7)
            rows = store.get_results_by_ids(ids)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["profile_id"], 7)
            # 未过滤时两行都在
            self.assertEqual(len(store.list_results()), 2)


class LegacyDatabaseTests(unittest.TestCase):
    """旧版数据库一律 fail-closed：不迁移、不改写、要求手动处理。"""

    def test_legacy_database_fails_closed(self):
        """旧版 schema（非 v1）：fail-closed 要求手动处理。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(LEGACY_SCHEMA)
                conn.commit()
            with self.assertRaises(RuntimeError) as raised:
                RegistrationRepository(path)
            self.assertIn("手动删除或改名", str(raised.exception))

    def test_legacy_v9_database_also_rejected(self):
        """旧版 v9（带 business_key）数据库同样不兼容：fail-closed。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "CREATE TABLE registration_results ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "email TEXT NOT NULL,"
                    "registration_status TEXT NOT NULL,"
                    "business_key TEXT NOT NULL DEFAULT 'legacy') "
                )
                conn.execute("PRAGMA user_version = 9")
                conn.commit()
            with self.assertRaises(RuntimeError) as raised:
                RegistrationRepository(path)
            self.assertIn("非 sub2api-native", str(raised.exception))


class ProfileIdHelperTests(unittest.TestCase):
    """Profile ID 规范化：作用域身份的唯一入口，fail-closed。"""

    def test_normalize_valid(self):
        self.assertEqual(normalize_profile_id(17), 17)
        self.assertEqual(normalize_profile_id("31"), 31)

    def test_normalize_invalid_refused(self):
        for bad in (0, -1, "", "abc", None, "1.5"):
            with self.assertRaises(ValueError):
                normalize_profile_id(bad)


class Sub2apiProfileTests(unittest.TestCase):
    """Profile CRUD 与生命周期规则（identity = id，name 仅展示）。"""

    def _store(self, tmp):
        return RegistrationRepository(Path(tmp) / "results.sqlite3")

    def _fresh(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return self._store(self._tmp.name)

    def test_whitelist_normalization(self):
        from backend.registration.store import normalize_domain_whitelist

        self.assertEqual(
            normalize_domain_whitelist("@qq.com; @foxmail.com,163.com\nqq.com"),
            ["qq.com", "foxmail.com", "163.com"],
        )
        self.assertEqual(normalize_domain_whitelist(""), [])
        self.assertEqual(normalize_domain_whitelist(None), [])
        self.assertEqual(normalize_domain_whitelist(["QQ.com", "@qq.com"]), ["qq.com"])
        self.assertEqual(normalize_domain_whitelist(["*.EDU.CN"]), ["*.edu.cn"])
        self.assertEqual(normalize_domain_whitelist(["*"]), ["*"])
        with self.assertRaises(ValueError):
            normalize_domain_whitelist("bad@domain")
        with self.assertRaises(ValueError):
            normalize_domain_whitelist("mail.*.edu.cn")

    def test_register_url_validation(self):
        from backend.registration.store import (
            normalize_register_url,
            register_url_origin,
        )

        self.assertEqual(
            normalize_register_url("  https://site.example/register#top  "),
            "https://site.example/register",
        )
        for bad in ("", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "not a url"):
            with self.assertRaises(ValueError):
                normalize_register_url(bad)
        self.assertEqual(register_url_origin("HTTPS://Site.Example:8443/a?x=1"), "https://site.example:8443")
        self.assertEqual(register_url_origin("http://a.example/"), "http://a.example")

    def test_crud_roundtrip(self):
        store = self._fresh()
        profile = store.create_profile(
            {
                "name": "  Test Site  ",
                "site_key": "true-sota",
                "promo_code": "P10",
                "invitation_code": "INV9",
                "aff_code": "AFF1",
            }
        )
        self.assertEqual(profile["name"], "Test Site")
        self.assertEqual(profile["register_url"], "https://true-sota.com/register")
        self.assertEqual(profile["site_key"], "true-sota")
        self.assertEqual(profile["register_origin"], "https://true-sota.com")
        self.assertIn("qq.com", profile["whitelist"])
        self.assertIn("*.edu.hk", profile["whitelist"])
        self.assertTrue(profile["enabled"])
        self.assertEqual(store.list_profiles()[0]["id"], profile["id"])
        self.assertEqual(store.get_profile_by_name("test site")["id"], profile["id"])
        # 名称冲突
        with self.assertRaises(Exception):
            store.create_profile({"name": "TEST SITE", "site_key": "ctai"})
        # 更新名称/禁用（不影响 identity）
        updated = store.update_profile(profile["id"], {"name": "Renamed", "enabled": False})
        self.assertEqual(updated["name"], "Renamed")
        self.assertFalse(updated["enabled"])
        self.assertEqual(store.get_profile(profile["id"])["id"], profile["id"])

    def test_rename_never_changes_scope(self):
        store = self._fresh()
        profile = store.create_profile(
            {"name": "A", "site_key": "true-sota"}
        )
        scope_before = profile["id"]
        store.update_profile(profile["id"], {"name": "B"})
        scope_after = store.get_profile(profile["id"])["id"]
        self.assertEqual(scope_before, scope_after)

    def test_unused_profile_can_change_origin_and_be_deleted(self):
        store = self._fresh()
        profile = store.create_profile(
            {"name": "X", "site_key": "true-sota"}
        )
        updated = store.update_profile(profile["id"], {"site_key": "ctai"})
        self.assertEqual(updated["register_origin"], "https://ai.chengtingkj.org")
        self.assertTrue(store.delete_profile(profile["id"]))
        self.assertIsNone(store.get_profile(profile["id"]))

    def test_used_profile_origin_frozen_and_not_deletable(self):
        """使用判定 = 任一 registration_results / mailbox_consumptions 行在该 profile_id 下。"""
        store = self._fresh()
        profile = store.create_profile(
            {"name": "Used", "site_key": "true-sota"}
        )
        store.add_result({"profile_id": profile["id"], "email": "u@x.com", "status": "failure"})
        self.assertTrue(store.profile_has_usage(profile["id"]))
        with self.assertRaises(Exception) as locked:
            store.update_profile(profile["id"], {"site_key": "ctai"})
        self.assertIn("origin", str(locked.exception))
        # 同 origin 的路径变化允许
        ok = store.update_profile(profile["id"], {"site_key": "true-sota"})
        self.assertEqual(ok["register_url"], "https://true-sota.com/register")
        with self.assertRaises(Exception) as in_use:
            store.delete_profile(profile["id"])
        self.assertIn("禁用", str(in_use.exception))

    def test_promote_verified_credentials_is_cas_and_audited(self):
        store = self._fresh()
        result_id = store.add_result(
            {
                "profile_id": 1,
                "email": "known@example.com",
                "password": "old-password",
                "status": "failure",
                "failure_type": "already_registered",
            }
        )
        updated = store.promote_verified_credentials(
            result_id, "new-password", verified_at="2026-08-30 08:30:00"
        )
        self.assertEqual(updated["registration_status"], "success")
        self.assertEqual(updated["success"], 1)
        self.assertEqual(updated["password"], "new-password")
        audit = json.loads(updated["extra_json"])
        self.assertEqual(audit["credential_verification"], "live_login")
        self.assertEqual(audit["credential_origin"], "externally_registered")
        with self.assertRaises(ValueError):
            store.promote_verified_credentials(result_id, "another-password")

    def test_usage_via_ledger_only(self):
        store = self._fresh()
        profile = store.create_profile(
            {"name": "Ledger", "site_key": "true-sota"}
        )
        store.mark_mailbox_consumed(profile["id"], "accounts", "led@x.com")
        self.assertTrue(store.profile_has_usage(profile["id"]))

    def test_manual_account_marks_profile_in_use(self):
        store = self._fresh()
        profile = store.create_profile({"name": "Manual", "site_key": "bmapi"})
        store.create_account(
            profile["id"], "manual@example.com", "password-1", "manual"
        )
        self.assertTrue(store.profile_has_usage(profile["id"]))
        with self.assertRaises(Exception):
            store.delete_profile(profile["id"])

    def test_legacy_verified_url_is_backfilled(self):
        store = self._fresh()
        profile = store.create_profile({"name": "Legacy", "site_key": "bmapi", "aff_code": ""})
        with store._connect() as conn:
            conn.execute("UPDATE sub2api_profiles SET site_key = '', aff_code = '' WHERE id = ?", (profile["id"],))
        reopened = RegistrationRepository(store.database_path)
        migrated = reopened.get_profile(profile["id"])
        self.assertEqual(migrated["site_key"], "bmapi")
        self.assertEqual(migrated["register_url"], "https://bmapi.020212.xyz/register")
        self.assertEqual(migrated["aff_code"], "WMHL43737MPD")

    def test_success_attempt_creates_account_but_failure_does_not(self):
        store = self._fresh()
        profile = store.create_profile({"name": "Assets", "site_key": "bmapi"})
        failed_id = store.add_result({"profile_id": profile["id"], "email": "failed@example.com", "password": "password-1", "status": "failure"})
        success_id = store.add_result({"profile_id": profile["id"], "email": "active@example.com", "password": "password-2", "status": "success"})
        self.assertIsNone(store.account_for_result(failed_id))
        account = store.account_for_result(success_id)
        self.assertEqual(account["status"], "active")
        key_id = store.upsert_account_key(account["id"], 7, "codex", "ciphertext", 2, "active")
        store.set_relay_key(account["id"], key_id)
        assets = store.relay_assets()
        self.assertEqual([(row["account_id"], row["key_id"]) for row in assets], [(account["id"], 7)])

    def test_missing_or_deleted_key_is_removed_from_relay_eligibility(self):
        store = self._fresh()
        profile = store.create_profile({"name": "Relay", "site_key": "bmapi"})
        account = store.create_account(
            profile["id"], "relay@example.com", "password-1"
        )
        key_id = store.upsert_account_key(
            account["id"], 7, "codex", "ciphertext", 2, "active"
        )
        store.set_relay_key(account["id"], key_id)
        self.assertEqual(len(store.relay_assets()), 1)

        self.assertEqual(store.reconcile_account_keys(account["id"], []), 1)
        self.assertEqual(store.relay_assets(), [])
        with self.assertRaisesRegex(ValueError, "有效"):
            store.set_account_relay_enabled(account["id"], True)
        with self.assertRaisesRegex(ValueError, "不属于"):
            store.set_relay_key(account["id"], key_id)

        store.update_account_key_metadata(
            account["id"], 7, name="codex", group_id=2, status="active"
        )
        store.set_relay_key(account["id"], key_id)
        store.mark_account_key_deleted(account["id"], key_id)
        refreshed = store.get_account(account["id"])
        self.assertIsNone(refreshed["relay_key_id"])
        self.assertFalse(refreshed["relay_enabled"])
        self.assertEqual(store.get_account_key(key_id)["key_ciphertext"], "")


if __name__ == "__main__":
    unittest.main()
