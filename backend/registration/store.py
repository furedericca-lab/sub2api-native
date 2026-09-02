# -*- coding: utf-8 -*-
"""注册结果仓储。

使用 SQLite WAL 保存任务结果与邮箱消费账本；每次操作建立独立连接以适配后台
线程并发。

数据模型是单业务（Sub2API）的：Profile ID 是唯一注册作用域身份，
不再有 business 字符串作用域。消费账本主键 (profile_id, email)：
同一 Profile 内一个邮箱只允许消费一次；同一邮箱可以在不同 Profile 各
消费一次。
"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit as _urlsplit

from .verified_sites import find_verified_site_by_url, get_verified_site


RESULT_COLUMNS = (
    "profile_id",
    "batch_id",
    "source",
    "started_at",
    "finished_at",
    "duration_seconds",
    "email",
    "password",
    "registration_status",
    "success",
    "provider",
    "worker_id",
    "failure_type",
    "registration_error",
    "mail_status",
    "consumed_at",
    "screenshot_path",
    "extra_json",
)

# 邮箱消费账本：邮箱一旦提交给目标站点即为不可逆外部副作用，永久记录。
# remote active ≠ registration available：OutlookEmail 侧保持 active，
# 本地 consumed 后绝不允许在 Profile 作用域内再次参与注册。
MAILBOX_CONSUMPTION_COLUMNS = (
    "profile_id",
    "email",
    "source",
    "consumed_at",
    "batch_id",
    "reason",
)

SQLITE_IN_BATCH_SIZE = 900

SCHEMA_VERSION = 2


def normalize_profile_id(value: Any) -> int:
    """规范化 Profile ID；非法值抛 ValueError（fail-closed）。"""
    try:
        profile_id = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"非法的 Profile ID: {value!r}") from None
    if profile_id <= 0:
        raise ValueError(f"Profile ID 必须是正整数: {value!r}")
    return profile_id


def normalize_register_url(value: Any) -> str:
    """规范化 Sub2API 注册页 URL。

    只允许 http/https（拒绝 javascript:/file:/data: 等 scheme）；
    保留 query（站点可能用 ?redirect= 等），去掉 fragment 与首尾空白。
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("Register URL 不能为空")
    parsed = _urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Register URL 必须是 http:// 或 https:// 地址")
    if not parsed.netloc or " " in parsed.netloc:
        raise ValueError("Register URL 地址不合法")
    return parsed._replace(fragment="").geturl()


def register_url_origin(value: Any) -> str:
    """URL 的规范 origin：scheme://host[:port]（全小写）。

    用于「已使用 Profile 的 origin 冻结」判定：同 origin 的路径变化允许，
    跨 origin 变化视为换站点，必须新建 Profile。
    """
    parsed = _urlsplit(str(value or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def normalize_domain_whitelist(value: Any) -> List[str]:
    """Normalize exact domains and verified leading ``*.suffix`` patterns.

    接受 JSON 数组或混合格式字符串（; , ， 空白/换行分隔）；每个条目
    去掉前导 @、转小写、去空、保序去重。空输入 = 不限制。
    仅允许 ``*.example.com`` 形式的受限通配；其它通配/正则直接拒绝。
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,\s\uFF0C]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        raise ValueError(f"邮箱域名白名单类型不合法: {type(value).__name__}")
    domains: List[str] = []
    seen: set = set()
    for raw in parts:
        domain = str(raw or "").strip().lstrip("@").lower()
        if not domain:
            continue
        if "@" in domain or " " in domain or (
            "*" in domain and domain != "*" and not domain.startswith("*.")
        ):
            raise ValueError(f"邮箱域名白名单条目不合法: {raw!r}")
        if domain.startswith("*.") and (domain.count("*") != 1 or domain.count(".") < 2):
            raise ValueError(f"邮箱域名白名单通配条目不合法: {raw!r}")
        if domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


class ProfileError(Exception):
    """Profile 管理通用错误。"""


class ProfileNotFoundError(ProfileError):
    pass


class ProfileNameConflictError(ProfileError):
    pass


class ProfileOriginLockedError(ProfileError):
    """已使用的 Profile 试图更换 origin。"""


class ProfileInUseError(ProfileError):
    """已使用的 Profile 被请求删除（应改用 enabled=false）。"""


class RegistrationRepository:
    def __init__(self, database_path: os.PathLike[str] | str):
        self.database_path = os.path.abspath(os.fspath(database_path))
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._fail_on_foreign_database()
        self._initialize()

    def _database_state(self) -> Tuple[int, set, set]:
        """读取现有 DB 的 (user_version, 核心列, 表名)；无 DB/不可读时返回空。"""
        if not os.path.exists(self.database_path):
            return 0, set(), set()
        try:
            conn = sqlite3.connect(self.database_path, timeout=15.0)
            try:
                version = int(
                    conn.execute("PRAGMA user_version").fetchone()[0] or 0
                )
                result_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(registration_results)"
                    ).fetchall()
                }
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return 0, set(), set()
        return version, result_columns, tables

    def _fail_on_foreign_database(self) -> None:
        """只接受 sub2api-native v1/v2；检测到其它 schema 直接报错。

        数据库逻辑：没有 DB -> 创建当前 schema；v1 -> 自动迁移到 v2；
        v2 -> 直接打开；其它旧版数据库 ->
        fail-closed，请人工确认后手动删除或改名该文件。
        """
        version, columns, tables = self._database_state()
        if version == 0 and not columns and not tables:
            return
        if version in (1, SCHEMA_VERSION):
            if not (
                "registration_status" in columns
                and "profile_id" in columns
                and "registration_results" in tables
            ):
                raise RuntimeError(
                    f"数据库 schema 版本为 {version} 但缺少核心列: "
                    f"{self.database_path}；请人工检查"
                )
            return
        raise RuntimeError(
            f"检测到非 sub2api-native 的数据库（user_version={version}）: "
            f"{self.database_path}；本项目数据库从 v1 开始、不兼容旧库，"
            "请确认无需历史数据后手动删除或改名该文件，再重启服务"
        )

    def _create_fresh_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS registration_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                account_id INTEGER,
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
                mail_status TEXT NOT NULL DEFAULT 'not_attempted',
                consumed_at TEXT NOT NULL DEFAULT '',
                screenshot_path TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_registration_results_finished
                ON registration_results(finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_registration_results_email
                ON registration_results(email COLLATE NOCASE, success);
            CREATE INDEX IF NOT EXISTS idx_registration_results_profile
                ON registration_results(profile_id, finished_at);
            CREATE INDEX IF NOT EXISTS idx_registration_results_reg_status
                ON registration_results(registration_status);
            CREATE INDEX IF NOT EXISTS idx_registration_results_batch
                ON registration_results(batch_id);

            CREATE TABLE IF NOT EXISTS mailbox_consumptions (
                profile_id INTEGER NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE,
                source TEXT NOT NULL DEFAULT 'accounts',
                consumed_at TEXT NOT NULL,
                batch_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (profile_id, email)
            );
            CREATE INDEX IF NOT EXISTS idx_mailbox_consumptions_email
                ON mailbox_consumptions(email COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS registration_job_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                batch_id TEXT NOT NULL DEFAULT '',
                running INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                finished_at REAL,
                target_count INTEGER NOT NULL DEFAULT 0,
                workers INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'web',
                profile_id INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                completed_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT NOT NULL DEFAULT '',
                current_email TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sub2api_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                site_key TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT 'register',
                register_url TEXT NOT NULL,
                register_origin TEXT NOT NULL DEFAULT '',
                promo_code TEXT NOT NULL DEFAULT '',
                invitation_code TEXT NOT NULL DEFAULT '',
                aff_code TEXT NOT NULL DEFAULT '',
                email_domain_whitelist TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE,
                password TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'registered',
                status TEXT NOT NULL DEFAULT 'active',
                relay_enabled INTEGER NOT NULL DEFAULT 1,
                relay_key_id INTEGER,
                last_login_at TEXT NOT NULL DEFAULT '',
                last_checkin_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(profile_id, email),
                FOREIGN KEY(profile_id) REFERENCES sub2api_profiles(id) ON DELETE RESTRICT,
                FOREIGN KEY(relay_key_id) REFERENCES account_api_keys(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS account_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                remote_key_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                key_ciphertext TEXT NOT NULL,
                group_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '',
                UNIQUE(account_id, remote_key_id),
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def now_text() -> str:
        return _datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=15.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            if version == 0:
                self._create_fresh_schema(conn)
                version = SCHEMA_VERSION
            elif version == 1:
                self._prepare_v1_asset_tables(conn)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sub2api_profiles)").fetchall()}
            result_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(registration_results)").fetchall()}
            if "account_id" not in result_columns:
                conn.execute("ALTER TABLE registration_results ADD COLUMN account_id INTEGER")
            if "site_key" not in columns:
                conn.execute("ALTER TABLE sub2api_profiles ADD COLUMN site_key TEXT NOT NULL DEFAULT ''")
            if "purpose" not in columns:
                conn.execute("ALTER TABLE sub2api_profiles ADD COLUMN purpose TEXT NOT NULL DEFAULT 'register'")
            conn.execute("UPDATE sub2api_profiles SET purpose = 'register' WHERE purpose <> 'register'")
            # Backfill profiles created before the verified-site contract. Only
            # exact verified origins are migrated; unknown legacy URLs remain
            # untouched and cannot be used for new registration jobs.
            legacy_rows = conn.execute(
                "SELECT id, site_key, register_url, aff_code FROM sub2api_profiles"
            ).fetchall()
            for row in legacy_rows:
                site = get_verified_site(row[1]) or find_verified_site_by_url(row[2])
                if site is None:
                    continue
                aff_code = str(row[3] or "").strip() or site.default_aff_code
                conn.execute(
                    "UPDATE sub2api_profiles SET site_key = ?, register_url = ?, register_origin = ?, aff_code = ?, email_domain_whitelist = ? WHERE id = ?",
                    (
                        site.key, site.register_url, register_url_origin(site.register_url), aff_code,
                        json.dumps(normalize_domain_whitelist(site.email_suffix_whitelist)), int(row[0]),
                    ),
                )
            # 单行快照：没有则插入空行
            conn.execute(
                """
                INSERT OR IGNORE INTO registration_job_snapshot (id, updated_at)
                VALUES (1, ?)
                """,
                (self.now_text(),),
            )
            # Existing successful registration rows become stable Account
            # records without changing the historical attempt table.
            conn.execute("""INSERT INTO accounts(profile_id,email,password,source,status,created_at,updated_at)
                SELECT r.profile_id,r.email,r.password,'registered','active',COALESCE(r.finished_at,?),COALESCE(r.finished_at,?)
                FROM registration_results r
                JOIN sub2api_profiles p ON p.id=r.profile_id
                WHERE r.email<>'' AND r.success=1
                ON CONFLICT(profile_id,email) DO UPDATE SET password=excluded.password,status='active',updated_at=excluded.updated_at""", (self.now_text(), self.now_text()))
            conn.execute("""DELETE FROM accounts WHERE source='registered' AND NOT EXISTS(
                SELECT 1 FROM registration_results r WHERE r.profile_id=accounts.profile_id
                AND r.email=accounts.email COLLATE NOCASE AND r.success=1)""")
            conn.execute("""UPDATE registration_results SET account_id=(SELECT a.id FROM accounts a
                WHERE a.profile_id=registration_results.profile_id AND a.email=registration_results.email COLLATE NOCASE)
                WHERE account_id IS NULL AND success=1 AND email<>''""")
            if version == 1:
                self._migrate_v1_asset_tables(conn)

    def _prepare_v1_asset_tables(self, conn: sqlite3.Connection) -> None:
        """Materialize the v1-compatible asset shape before rebuilding with FKs."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE, password TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'registered', status TEXT NOT NULL DEFAULT 'active',
                relay_enabled INTEGER NOT NULL DEFAULT 1, relay_key_id INTEGER,
                last_login_at TEXT NOT NULL DEFAULT '', last_checkin_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(profile_id, email)
            );
            CREATE TABLE IF NOT EXISTS account_api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
                remote_key_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
                key_ciphertext TEXT NOT NULL, group_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '', UNIQUE(account_id, remote_key_id)
            );
            """
        )
        result_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(registration_results)").fetchall()
        }
        if "account_id" not in result_columns:
            conn.execute("ALTER TABLE registration_results ADD COLUMN account_id INTEGER")

    def _migrate_v1_asset_tables(self, conn: sqlite3.Connection) -> None:
        """Upgrade native v1 assets to v2 without accepting foreign databases."""
        conn.executescript(
            """
            CREATE TABLE accounts_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE,
                password TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'registered',
                status TEXT NOT NULL DEFAULT 'active',
                relay_enabled INTEGER NOT NULL DEFAULT 1,
                relay_key_id INTEGER,
                last_login_at TEXT NOT NULL DEFAULT '',
                last_checkin_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(profile_id, email),
                FOREIGN KEY(profile_id) REFERENCES sub2api_profiles(id) ON DELETE RESTRICT,
                FOREIGN KEY(relay_key_id) REFERENCES account_api_keys_v2(id) ON DELETE SET NULL
            );
            CREATE TABLE account_api_keys_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                remote_key_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                key_ciphertext TEXT NOT NULL,
                group_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL DEFAULT '',
                UNIQUE(account_id, remote_key_id),
                FOREIGN KEY(account_id) REFERENCES accounts_v2(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """INSERT INTO accounts_v2(
                   id,profile_id,email,password,source,status,relay_enabled,relay_key_id,
                   last_login_at,last_checkin_at,last_error,created_at,updated_at)
               SELECT a.id,a.profile_id,a.email,a.password,a.source,a.status,a.relay_enabled,NULL,
                      a.last_login_at,a.last_checkin_at,a.last_error,a.created_at,a.updated_at
               FROM accounts a JOIN sub2api_profiles p ON p.id=a.profile_id"""
        )
        conn.execute(
            """INSERT INTO account_api_keys_v2(
                   id,account_id,remote_key_id,name,key_ciphertext,group_id,status,created_at,last_seen_at)
               SELECT k.id,k.account_id,k.remote_key_id,k.name,k.key_ciphertext,k.group_id,
                      k.status,k.created_at,k.last_seen_at
               FROM account_api_keys k JOIN accounts_v2 a ON a.id=k.account_id"""
        )
        conn.execute(
            """UPDATE accounts_v2 SET relay_key_id=(
                   SELECT k.id FROM accounts old
                   JOIN account_api_keys_v2 k
                     ON k.id=old.relay_key_id AND k.account_id=accounts_v2.id
                   WHERE old.id=accounts_v2.id)"""
        )
        conn.executescript(
            """
            DROP TABLE account_api_keys;
            DROP TABLE accounts;
            ALTER TABLE accounts_v2 RENAME TO accounts;
            ALTER TABLE account_api_keys_v2 RENAME TO account_api_keys;
            PRAGMA user_version = 2;
            """
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"v1 -> v2 外键迁移失败: {violations!r}")

    def add_result(self, record: Dict[str, Any]) -> int:
        now = self.now_text()
        registration_status = str(
            record.get("registration_status", record.get("status")) or "failure"
        ).strip().lower()
        success = (
            1
            if registration_status == "success" or bool(record.get("success"))
            else 0
        )
        extra = record.get("extra_json", record.get("extra", {}))
        if isinstance(extra, str):
            extra_json = extra
        else:
            extra_json = json.dumps(extra or {}, ensure_ascii=False, sort_keys=True)

        normalized = {
            "profile_id": normalize_profile_id(record.get("profile_id")),
            "batch_id": str(record.get("batch_id") or ""),
            "source": str(record.get("source") or "web"),
            "started_at": str(record.get("started_at") or now),
            "finished_at": str(record.get("finished_at") or now),
            "duration_seconds": max(float(record.get("duration_seconds") or 0), 0.0),
            "email": str(record.get("email") or "").strip(),
            "password": str(record.get("password") or ""),
            "registration_status": registration_status,
            "success": success,
            "provider": str(record.get("provider") or "outlookemail"),
            "worker_id": int(record.get("worker_id") or 0),
            "failure_type": str(record.get("failure_type") or ""),
            "registration_error": str(
                record.get("registration_error", record.get("failure_reason")) or ""
            ),
            "mail_status": str(
                record.get("mail_status") or "not_attempted"
            ).strip().lower(),
            "consumed_at": str(record.get("consumed_at") or ""),
            "screenshot_path": str(record.get("screenshot_path") or ""),
            "extra_json": extra_json,
        }
        columns = ", ".join(RESULT_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in RESULT_COLUMNS)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO registration_results ({columns}) VALUES ({placeholders})",
                normalized,
            )
            result_id = int(cursor.lastrowid)
            if success and normalized["email"]:
                conn.execute("""INSERT INTO accounts(profile_id,email,password,source,status,created_at,updated_at)
                    SELECT ?,?,?,'registered','active',?,?
                    WHERE EXISTS(SELECT 1 FROM sub2api_profiles WHERE id=?)
                    ON CONFLICT(profile_id,email) DO UPDATE SET password=excluded.password,status='active',updated_at=excluded.updated_at""",
                    (normalized["profile_id"], normalized["email"], normalized["password"], now, now, normalized["profile_id"]))
                account = conn.execute("SELECT id FROM accounts WHERE profile_id=? AND email=? COLLATE NOCASE", (normalized["profile_id"], normalized["email"])).fetchone()
                if account:
                    conn.execute("UPDATE registration_results SET account_id=? WHERE id=?", (int(account[0]), result_id))
            return result_id

    def list_accounts(self, *, profile_id: Any = "", status: str = "", limit: int = 10000) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if profile_id not in (None, ""):
            clauses.append("a.profile_id = ?"); params.append(normalize_profile_id(profile_id))
        if status:
            clauses.append("a.status = ?"); params.append(str(status).strip().lower())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit or 10000), 10000)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(f"""SELECT
                a.id,a.profile_id,a.email,a.source,a.status,a.relay_enabled,
                a.relay_key_id,a.last_login_at,a.last_checkin_at,a.last_error,
                a.created_at,a.updated_at,p.name AS profile_name,p.site_key,
                COUNT(k.id) AS key_count,
                SUM(CASE WHEN k.status='active' THEN 1 ELSE 0 END) AS active_key_count,
                rk.name AS relay_key_name,rk.status AS relay_key_status,
                (SELECT MAX(r.id) FROM registration_results r WHERE r.profile_id=a.profile_id AND r.email=a.email COLLATE NOCASE AND r.success=1) AS result_id
                FROM accounts a JOIN sub2api_profiles p ON p.id=a.profile_id
                LEFT JOIN account_api_keys k ON k.account_id=a.id
                LEFT JOIN account_api_keys rk ON rk.id=a.relay_key_id
                {where} GROUP BY a.id ORDER BY a.id DESC LIMIT ?""", params).fetchall()]

    def profile_asset_counts(self, profile_id: int) -> Dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT a.id) AS account_count,
                          COUNT(k.id) AS key_count,
                          SUM(CASE WHEN k.status='active' THEN 1 ELSE 0 END) AS active_key_count
                   FROM accounts a
                   LEFT JOIN account_api_keys k ON k.account_id=a.id
                   WHERE a.profile_id=?""",
                (int(profile_id),),
            ).fetchone()
        return {
            "account_count": int(row["account_count"] or 0),
            "key_count": int(row["key_count"] or 0),
            "active_key_count": int(row["active_key_count"] or 0),
        }

    def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account_id),)).fetchone()
        return dict(row) if row else None

    def get_accounts_by_ids(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        normalized: List[int] = []
        seen = set()
        for raw in ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            return []
        by_id: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM accounts WHERE id IN ({placeholders})", batch
                ).fetchall()
                by_id.update({int(row["id"]): dict(row) for row in rows})
        return [by_id[item_id] for item_id in normalized if item_id in by_id]

    def get_account_context(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT a.*,p.name AS profile_name,p.site_key,p.register_origin,
                          p.enabled AS profile_enabled
                   FROM accounts a
                   JOIN sub2api_profiles p ON p.id=a.profile_id
                   WHERE a.id=?""",
                (int(account_id),),
            ).fetchone()
        return dict(row) if row else None

    def create_account(self, profile_id: int, email: str, password: str, source: str = "manual") -> Dict[str, Any]:
        now = self.now_text(); normalized_email = str(email or "").strip()
        if not normalized_email or not password: raise ValueError("邮箱和密码不能为空")
        with self._connect() as conn:
            conn.execute("""INSERT INTO accounts(profile_id,email,password,source,status,last_login_at,created_at,updated_at)
                VALUES(?,?,?,?,'active',?,?,?) ON CONFLICT(profile_id,email) DO UPDATE SET password=excluded.password,status='active',last_login_at=excluded.last_login_at,last_error='',updated_at=excluded.updated_at""",
                (normalize_profile_id(profile_id), normalized_email, password, source, now, now, now))
            row = conn.execute("SELECT * FROM accounts WHERE profile_id=? AND email=? COLLATE NOCASE", (int(profile_id), normalized_email)).fetchone()
        return dict(row)

    def account_for_result(self, result_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT a.* FROM accounts a JOIN registration_results r ON r.account_id=a.id WHERE r.id=?", (int(result_id),)).fetchone()
        return dict(row) if row else None

    def upsert_account_key(self, account_id: int, remote_key_id: int, name: str, ciphertext: str, group_id: int, status: str) -> int:
        now = self.now_text()
        with self._connect() as conn:
            conn.execute("""INSERT INTO account_api_keys(account_id,remote_key_id,name,key_ciphertext,group_id,status,created_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id,remote_key_id) DO UPDATE SET name=excluded.name,key_ciphertext=excluded.key_ciphertext,group_id=excluded.group_id,status=excluded.status,last_seen_at=excluded.last_seen_at""",
                (int(account_id), int(remote_key_id), str(name or ""), ciphertext, int(group_id or 0), str(status or "active"), now, now))
            return int(conn.execute("SELECT id FROM account_api_keys WHERE account_id=? AND remote_key_id=?", (int(account_id), int(remote_key_id))).fetchone()[0])

    def get_account_key(self, key_row_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_api_keys WHERE id=?", (int(key_row_id),)
            ).fetchone()
        return dict(row) if row else None

    def account_key_by_remote_id(
        self, account_id: int, remote_key_id: int
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM account_api_keys
                   WHERE account_id=? AND remote_key_id=?""",
                (int(account_id), int(remote_key_id)),
            ).fetchone()
        return dict(row) if row else None

    def update_account_key_metadata(
        self,
        account_id: int,
        remote_key_id: int,
        *,
        name: str,
        group_id: int,
        status: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE account_api_keys
                   SET name=?,group_id=?,status=?,last_seen_at=?
                   WHERE account_id=? AND remote_key_id=?""",
                (
                    str(name or ""),
                    int(group_id or 0),
                    str(status or "active"),
                    self.now_text(),
                    int(account_id),
                    int(remote_key_id),
                ),
            )
        return cursor.rowcount > 0

    def reconcile_account_keys(
        self, account_id: int, remote_key_ids: Iterable[int]
    ) -> int:
        observed = sorted({int(value) for value in remote_key_ids if int(value) > 0})
        with self._connect() as conn:
            if observed:
                placeholders = ",".join("?" for _ in observed)
                cursor = conn.execute(
                    f"""UPDATE account_api_keys SET status='missing'
                        WHERE account_id=? AND status<>'deleted'
                          AND remote_key_id NOT IN ({placeholders})""",
                    (int(account_id), *observed),
                )
            else:
                cursor = conn.execute(
                    """UPDATE account_api_keys SET status='missing'
                       WHERE account_id=? AND status<>'deleted'""",
                    (int(account_id),),
                )
        return max(0, int(cursor.rowcount or 0))

    def mark_account_key_deleted(self, account_id: int, key_row_id: int) -> None:
        now = self.now_text()
        with self._connect() as conn:
            key = conn.execute(
                "SELECT id FROM account_api_keys WHERE id=? AND account_id=?",
                (int(key_row_id), int(account_id)),
            ).fetchone()
            if not key:
                raise ValueError("API Key 不属于该账号")
            conn.execute(
                """UPDATE account_api_keys
                   SET status='deleted',key_ciphertext='',last_seen_at=? WHERE id=?""",
                (now, int(key_row_id)),
            )
            conn.execute(
                """UPDATE accounts
                   SET relay_key_id=NULL,relay_enabled=0,updated_at=?
                   WHERE id=? AND relay_key_id=?""",
                (now, int(account_id), int(key_row_id)),
            )

    def record_account_login(
        self,
        account_id: int,
        *,
        success: bool,
        error: str = "",
        authentication_failure: bool = False,
    ) -> None:
        now = self.now_text()
        status = (
            "active"
            if success
            else ("authentication_failure" if authentication_failure else None)
        )
        with self._connect() as conn:
            if status is None:
                conn.execute(
                    "UPDATE accounts SET last_error=?,updated_at=? WHERE id=?",
                    (str(error or "")[:500], now, int(account_id)),
                )
            else:
                conn.execute(
                    """UPDATE accounts
                       SET status=?,last_login_at=?,last_error=?,updated_at=?
                       WHERE id=?""",
                    (
                        status,
                        now,
                        "" if success else str(error or "")[:500],
                        now,
                        int(account_id),
                    ),
                )

    def record_account_checkin(
        self, account_id: int, *, success: bool, error: str = ""
    ) -> None:
        now = self.now_text()
        with self._connect() as conn:
            if success:
                conn.execute(
                    """UPDATE accounts
                       SET last_checkin_at=?,last_error='',updated_at=? WHERE id=?""",
                    (now, now, int(account_id)),
                )
            else:
                conn.execute(
                    "UPDATE accounts SET last_error=?,updated_at=? WHERE id=?",
                    (str(error or "")[:500], now, int(account_id)),
                )

    def set_relay_key(self, account_id: int, key_row_id: int) -> None:
        with self._connect() as conn:
            valid = conn.execute("SELECT 1 FROM account_api_keys WHERE id=? AND account_id=? AND status='active'", (int(key_row_id), int(account_id))).fetchone()
            if not valid: raise ValueError("API Key 不属于该账号")
            conn.execute("UPDATE accounts SET relay_key_id=?,updated_at=? WHERE id=?", (int(key_row_id), self.now_text(), int(account_id)))

    def set_account_relay_enabled(self, account_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            account = conn.execute(
                """SELECT a.id,k.status AS key_status FROM accounts a
                   LEFT JOIN account_api_keys k ON k.id=a.relay_key_id
                   WHERE a.id=?""",
                (int(account_id),),
            ).fetchone()
            if not account:
                raise ValueError("Account 不存在")
            if enabled and account["key_status"] != "active":
                raise ValueError("必须先选择有效的 Relay Key")
            conn.execute("UPDATE accounts SET relay_enabled=?,updated_at=? WHERE id=?", (int(enabled), self.now_text(), int(account_id)))

    def list_account_keys(self, account_id: int = 0) -> List[Dict[str, Any]]:
        query = """SELECT k.id,k.account_id,k.remote_key_id,k.name,k.group_id,k.status,k.created_at,k.last_seen_at,
            a.email,p.name AS profile_name,CASE WHEN a.relay_key_id=k.id THEN 1 ELSE 0 END AS is_relay
            FROM account_api_keys k JOIN accounts a ON a.id=k.account_id JOIN sub2api_profiles p ON p.id=a.profile_id"""
        params: tuple = ()
        if account_id > 0: query += " WHERE k.account_id=?"; params = (int(account_id),)
        query += " ORDER BY k.account_id,k.id"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def relay_assets(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT a.id AS account_id,a.email,a.relay_enabled,a.status AS account_status,
                    p.name AS profile_name,p.site_key,p.register_origin AS origin,p.enabled AS profile_enabled,
                    k.id AS key_row_id,k.remote_key_id AS key_id,k.name AS key_name,k.key_ciphertext,k.group_id,k.status AS key_status
                FROM accounts a JOIN sub2api_profiles p ON p.id=a.profile_id
                JOIN account_api_keys k ON k.id=a.relay_key_id
                WHERE a.status='active' AND a.relay_enabled=1 AND p.enabled=1 AND k.status='active'
                ORDER BY a.id""").fetchall()
        return [dict(row) for row in rows]

    def has_success(self, email: str, *, profile_id: int) -> bool:
        normalized = str(email or "").strip()
        if not normalized:
            return False
        profile_id = normalize_profile_id(profile_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM registration_results
                WHERE profile_id = ?
                  AND success = 1
                  AND email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (profile_id, normalized),
            ).fetchone()
        return row is not None

    def has_registered_or_consumed(
        self, email: str, *, profile_id: int
    ) -> bool:
        """成功、已判定账号已注册/注册风控，或已消耗邮箱的记录，都应避免再次取用（Profile 作用域内）。"""
        normalized = str(email or "").strip()
        if not normalized:
            return False
        profile_id = normalize_profile_id(profile_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM registration_results
                WHERE profile_id = ?
                  AND email = ? COLLATE NOCASE
                  AND (
                    success = 1
                    OR lower(coalesce(failure_type, '')) IN (
                        'already_registered', 'registration_risk'
                    )
                    OR lower(coalesce(mail_status, '')) IN ('success', 'failed', 'consumed')
                  )
                LIMIT 1
                """,
                (profile_id, normalized),
            ).fetchone()
        return row is not None

    def is_mailbox_consumed(
        self, profile_id: int, source: str, email: str
    ) -> bool:
        """消费账本命中（Profile 作用域 + 来源命名空间）：审计 / 指定来源查询用。"""
        normalized = str(email or "").strip()
        if not normalized:
            return False
        profile_id = normalize_profile_id(profile_id)
        normalized_source = str(source or "accounts").strip().lower() or "accounts"
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM mailbox_consumptions
                WHERE profile_id = ? AND source = ? AND email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (profile_id, normalized_source, normalized),
            ).fetchone()
        return row is not None

    def is_mailbox_consumed_any_source(
        self, email: str, *, profile_id: int
    ) -> bool:
        """消费硬边界（Profile 作用域内跨来源）：一个 email 在一个 Profile 里就是一个身份。

        无论该邮箱来自 accounts 还是 temp，只要在同一 Profile 下提交过，
        就永久不允许再次参与注册。source 仅保留为审计元数据；
        同一邮箱仍可分别在不同 Profile 各消费一次。
        """
        normalized = str(email or "").strip()
        if not normalized:
            return False
        profile_id = normalize_profile_id(profile_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM mailbox_consumptions
                WHERE profile_id = ? AND email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (profile_id, normalized),
            ).fetchone()
        return row is not None

    def mark_mailbox_consumed(
        self,
        profile_id: int,
        source: str,
        email: str,
        *,
        batch_id: str = "",
        reason: str = "",
    ) -> bool:
        """记录不可逆消费边界；返回 True 表示该 Profile 作用域内首次写入。

        一个 email 在一个 Profile 作用域内就是一个身份：同一 profile_id 下
        任意 source 已有消费记录即视为重复，返回 False 且不再新增行；
        source 仅记录第一次真实消费的来源（审计元数据）。
        原子性：BEGIN IMMEDIATE 先取写锁，check + insert 在同一临界区内
        完成，并发 worker 也无法为同一 (profile_id, email) 写入第二条。
        """
        normalized = str(email or "").strip()
        if not normalized:
            return False
        profile_id = normalize_profile_id(profile_id)
        normalized_source = str(source or "accounts").strip().lower() or "accounts"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT 1
                FROM mailbox_consumptions
                WHERE profile_id = ? AND email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (profile_id, normalized),
            ).fetchone()
            if row:
                return False
            conn.execute(
                """
                INSERT INTO mailbox_consumptions
                       (profile_id, source, email, consumed_at, batch_id, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, email) DO NOTHING
                """,
                (
                    profile_id,
                    normalized_source,
                    normalized,
                    self.now_text(),
                    str(batch_id or ""),
                    str(reason or ""),
                ),
            )
            return True

    def release_consumptions(
        self, emails: Iterable[str], *, profile_id: int
    ) -> List[str]:
        """删除指定邮箱在**指定 Profile 作用域**内的消费标记（人工释放，仅限确认未建号的失败场景）。

        返回实际释放的邮箱列表（按库内原样大小写去重）。这是对 fail-closed
        账本的人工例外出口：调用方必须先确认该邮箱在该 Profile 侧没有账号。
        只删本 Profile 作用域的行——绝不能误删其它 Profile 的。
        """
        normalized: List[str] = []
        seen: set = set()
        for item in emails:
            value = str(item or "").strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            return []
        profile_id = normalize_profile_id(profile_id)
        with self._connect() as conn:
            released: List[str] = []
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT email FROM mailbox_consumptions
                    WHERE profile_id = ?
                      AND email COLLATE NOCASE IN ({placeholders})
                    """,
                    [profile_id, *batch],
                ).fetchall()
                released.extend(str(row["email"]) for row in rows)
                conn.execute(
                    f"""
                    DELETE FROM mailbox_consumptions
                    WHERE profile_id = ?
                      AND email COLLATE NOCASE IN ({placeholders})
                    """,
                    [profile_id, *batch],
                )
            return released

    def can_release_consumption(
        self,
        emails: Iterable[str],
        *,
        profile_id: int,
    ) -> Dict[str, str]:
        """逐邮箱判定是否允许人工释放消费标记（fail-closed）。

        返回 {lower(email): 拒绝原因}；空 dict 表示全部可释放。

        必须检查同一 Profile 作用域下该邮箱的全部历史记录——不能只看选中的行：
        同邮箱可能存在另一条 success 记录（站点账号真实存在），
        只看选中行会误删账本、允许重注必然失败的邮箱。
        """
        normalized: List[str] = []
        seen: set = set()
        for item in emails:
            value = str(item or "").strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            return {}
        profile_id = normalize_profile_id(profile_id)
        blocked: Dict[str, str] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT lower(email) AS email_key,
                           MAX(CASE WHEN success = 1
                                     OR registration_status = 'success'
                                    THEN 1 ELSE 0 END) AS has_success
                    FROM registration_results
                    WHERE profile_id = ?
                      AND email COLLATE NOCASE IN ({placeholders})
                    GROUP BY lower(email)
                    """,
                    [profile_id, *batch],
                ).fetchall()
                for row in rows:
                    reasons = []
                    if int(row["has_success"] or 0):
                        reasons.append("存在成功记录（站点账号真实存在，重注必然失败）")
                    if reasons:
                        blocked[str(row["email_key"])] = "；".join(reasons)
        return blocked

    def consumption_sources(
        self, email: str, *, profile_id: int
    ) -> List[str]:
        """列出该邮箱在指定 Profile 作用域内消费账本中的全部去重审计来源（accounts/temp）。

        主键 (profile_id, email) 保证同一 Profile 内至多一行；返回的审计
        来源必须唯一且有效（accounts/temp），调用方对异常值 fail-closed。
        """
        normalized = str(email or "").strip()
        if not normalized:
            return []
        profile_id = normalize_profile_id(profile_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT lower(source) AS source
                FROM mailbox_consumptions
                WHERE profile_id = ? AND email = ? COLLATE NOCASE
                ORDER BY source
                """,
                (profile_id, normalized),
            ).fetchall()
        return [str(row["source"] or "") for row in rows if str(row["source"] or "").strip()]

    @staticmethod
    def _result_filters(
        *,
        status: str = "",
        mail_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        profile_id: Any = "",
    ) -> Tuple[str, List[Any]]:
        """构造结果表过滤条件。

        profile_id 语义：空串 = 不限（全部 Profile）；非空 = 精确 Profile 过滤。
        """
        clauses = []
        params: List[Any] = []
        normalized_profile = str(profile_id or "").strip()
        if normalized_profile:
            clauses.append("profile_id = ?")
            params.append(normalize_profile_id(normalized_profile))
        normalized_reg_status = str(status or "").strip().lower()
        if normalized_reg_status:
            clauses.append("registration_status = ?")
            params.append(normalized_reg_status)
        normalized_mail_status = str(mail_status or "").strip().lower()
        if normalized_mail_status:
            clauses.append("mail_status = ?")
            params.append(normalized_mail_status)
        normalized_batch_id = str(batch_id or "").strip()
        if normalized_batch_id:
            clauses.append("batch_id = ?")
            params.append(normalized_batch_id)
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            like = f"%{normalized_keyword}%"
            clauses.append(
                "(email LIKE ? OR provider LIKE ? OR registration_error LIKE ? "
                "OR batch_id LIKE ?)"
            )
            params.extend([like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_results(
        self,
        *,
        status: str = "",
        mail_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        profile_id: Any = "",
        limit: int = 2000,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where, params = self._result_filters(
            status=status,
            mail_status=mail_status,
            keyword=keyword,
            batch_id=batch_id,
            profile_id=profile_id,
        )
        safe_limit = max(1, min(int(limit or 2000), 10000))
        safe_offset = max(0, int(offset or 0))
        params.extend([safe_limit, safe_offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM registration_results
                {where}
                ORDER BY finished_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_results(
        self,
        *,
        status: str = "",
        mail_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        profile_id: Any = "",
    ) -> int:
        """返回与账号列表相同筛选条件下的记录总数。"""
        where, params = self._result_filters(
            status=status,
            mail_status=mail_status,
            keyword=keyword,
            batch_id=batch_id,
            profile_id=profile_id,
        )
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS total FROM registration_results {where}", params
            ).fetchone()
        return int(row["total"] or 0)

    def list_result_ids(
        self,
        *,
        status: str = "",
        mail_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        profile_id: Any = "",
    ) -> List[int]:
        """返回与账号列表相同筛选条件下的全部主键，顺序与列表一致。"""
        where, params = self._result_filters(
            status=status,
            mail_status=mail_status,
            keyword=keyword,
            batch_id=batch_id,
            profile_id=profile_id,
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id
                FROM registration_results
                {where}
                ORDER BY finished_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def get_results_by_ids(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        """按主键批量读取记录，保持传入顺序。"""
        normalized: List[int] = []
        seen = set()
        for raw in ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            return []
        by_id: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM registration_results
                    WHERE id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                by_id.update({int(row["id"]): dict(row) for row in rows})
        return [by_id[item_id] for item_id in normalized if item_id in by_id]

    def promote_verified_credentials(
        self,
        result_id: int | str,
        password: str,
        *,
        verified_at: str = "",
    ) -> Dict[str, Any]:
        """Persist externally-registered credentials after live login verification."""
        try:
            normalized_id = int(result_id)
        except (TypeError, ValueError):
            raise ValueError("账号记录 ID 无效") from None
        if normalized_id <= 0:
            raise ValueError("账号记录 ID 无效")
        normalized_password = str(password or "")
        if not normalized_password:
            raise ValueError("账号密码不能为空")
        timestamp = str(verified_at or self.now_text())

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM registration_results WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ValueError("账号记录不存在")
            current = dict(row)
            if current.get("success") or str(current.get("registration_status") or "") == "success":
                raise ValueError("账号已经是成功状态，无需凭据验证")
            if str(current.get("failure_type") or "") != "already_registered":
                raise ValueError("只有 already_registered 记录可以验证凭据")
            try:
                extra = json.loads(str(current.get("extra_json") or "{}"))
            except (TypeError, ValueError):
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            extra.update(
                {
                    "credential_verified_at": timestamp,
                    "credential_verification": "live_login",
                    "credential_origin": "externally_registered",
                    "previous_registration_status": str(
                        current.get("registration_status") or "failure"
                    ),
                    "previous_failure_type": str(current.get("failure_type") or ""),
                }
            )
            conn.execute(
                """
                UPDATE registration_results
                SET password = ?, registration_status = 'success', success = 1,
                    failure_type = '', registration_error = '', extra_json = ?,
                    finished_at = ?
                WHERE id = ? AND success = 0
                  AND registration_status = 'failure'
                  AND failure_type = 'already_registered'
                """,
                (
                    normalized_password,
                    json.dumps(extra, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    normalized_id,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("账号状态已被其他操作更新，请刷新后重试")
            updated = conn.execute(
                "SELECT * FROM registration_results WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            return dict(updated)

    def delete_results(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        """删除指定记录，返回实际删除前的记录快照。"""
        records = self.get_results_by_ids(ids)
        if not records:
            return []
        delete_ids = [int(row["id"]) for row in records]
        with self._connect() as conn:
            for start in range(0, len(delete_ids), SQLITE_IN_BATCH_SIZE):
                batch = delete_ids[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM registration_results WHERE id IN ({placeholders})",
                    batch,
                )
        return records

    def stats(self) -> Dict[str, Any]:
        today = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN registration_status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN registration_status = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN registration_status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN registration_status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    SUM(CASE WHEN mail_status = 'consumed' THEN 1 ELSE 0 END) AS results_mailbox_consumed,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today_total,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? AND registration_status = 'success' THEN 1 ELSE 0 END) AS today_success,
                    -- 唯一成功账号数：身份 = (profile_id, email)，同一邮箱跨 Profile 计多个账号
                    COUNT(DISTINCT CASE WHEN success = 1 THEN profile_id || '|' || lower(email) END) AS unique_success_accounts,
                    AVG(CASE WHEN registration_status = 'success' AND duration_seconds > 0 THEN duration_seconds END) AS avg_success_seconds
                FROM registration_results
                """,
                (today, today),
            ).fetchone()
            # 消费账本是唯一真相源：即使 submit 后 crash（无 registration_result 行），
            # 消费计数也不丢失；删除账号记录同样不影响消费统计。
            ledger_row = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT profile_id || '|' || lower(email)) AS mailbox_consumed,
                    COUNT(DISTINCT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM registration_results r
                        WHERE r.profile_id = m.profile_id
                          AND r.email = m.email COLLATE NOCASE
                    ) THEN m.profile_id || '|' || lower(m.email) END) AS orphan_consumptions
                FROM mailbox_consumptions m
                """
            ).fetchone()
            # 按 Profile 汇总（注册记录）
            profile_rows = conn.execute(
                """
                SELECT
                    profile_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN registration_status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN registration_status = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN mail_status = 'consumed' THEN 1 ELSE 0 END) AS consumed
                FROM registration_results
                GROUP BY profile_id
                ORDER BY profile_id ASC
                """
            ).fetchall()
            # 每个 Profile 的账本消费数
            profile_consumed = conn.execute(
                """
                SELECT profile_id, COUNT(DISTINCT lower(email)) AS consumed
                FROM mailbox_consumptions
                GROUP BY profile_id
                ORDER BY profile_id ASC
                """
            ).fetchall()
            # 全部 Profile 的名称
            name_rows = conn.execute(
                "SELECT id, name FROM sub2api_profiles"
            ).fetchall()
        result = {key: (row[key] or 0) for key in row.keys()}
        result["mailbox_consumed"] = int(ledger_row["mailbox_consumed"] or 0)
        result["orphan_consumptions"] = int(ledger_row["orphan_consumptions"] or 0)
        name_by_id = {int(item["id"]): str(item["name"]) for item in name_rows}
        consumed_by_profile = {
            int(item["profile_id"]): int(item["consumed"] or 0)
            for item in profile_consumed
        }
        profile_entries = []
        for item in profile_rows:
            pid = int(item["profile_id"])
            profile_entries.append(
                {
                    "profile_id": pid,
                    "profile_name": name_by_id.get(pid, ""),
                    "total": int(item["total"] or 0),
                    "success": int(item["success"] or 0),
                    "failure": int(item["failure"] or 0),
                    "consumed": max(
                        int(item["consumed"] or 0),
                        consumed_by_profile.get(pid, 0),
                    ),
                }
            )
        # 账本里可能有该 Profile 尚无结果行的消费（crash 边界）：补上对应条目
        known = {entry["profile_id"] for entry in profile_entries}
        for pid, ledger_consumed in consumed_by_profile.items():
            if pid not in known and ledger_consumed > 0:
                profile_entries.append(
                    {
                        "profile_id": pid,
                        "profile_name": name_by_id.get(pid, ""),
                        "total": 0,
                        "success": 0,
                        "failure": 0,
                        "consumed": ledger_consumed,
                    }
                )
        profile_entries.sort(key=lambda entry: entry["profile_id"])
        result["profiles"] = profile_entries
        return result

    def get_job_snapshot(self) -> Dict[str, Any]:
        """读取最近一次 Web 注册任务快照（单行）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM registration_job_snapshot WHERE id = 1"
            ).fetchone()
        if not row:
            return {}
        data = dict(row)
        data["running"] = bool(data.get("running"))
        return data

    def save_job_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """持久化最近一次 Web 注册任务快照，供服务重启后恢复批次与进度摘要。"""
        now = self.now_text()
        try:
            profile_id = int(snapshot.get("profile_id") or 0)
        except (TypeError, ValueError):
            profile_id = 0
        payload = {
            "batch_id": str(snapshot.get("batch_id") or ""),
            "running": 1 if snapshot.get("running") else 0,
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "target_count": int(snapshot.get("target_count") or 0),
            "workers": int(snapshot.get("workers") or 1),
            "source": str(snapshot.get("source") or "web"),
            # 任务级 Profile 是运行时输入，不落 config.json；0 = 未知。
            "profile_id": profile_id,
            "last_error": str(snapshot.get("last_error") or ""),
            "completed_count": int(snapshot.get("completed_count") or 0),
            "success_count": int(snapshot.get("success_count") or 0),
            "failure_count": int(snapshot.get("failure_count") or 0),
            "current_stage": str(snapshot.get("current_stage") or ""),
            "current_email": str(snapshot.get("current_email") or ""),
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registration_job_snapshot (
                    id, batch_id, running, started_at, finished_at, target_count, workers,
                    source, profile_id, last_error, completed_count, success_count,
                    failure_count, current_stage, current_email, updated_at
                ) VALUES (
                    1, :batch_id, :running, :started_at, :finished_at, :target_count, :workers,
                    :source, :profile_id, :last_error, :completed_count, :success_count,
                    :failure_count, :current_stage, :current_email, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    batch_id = excluded.batch_id,
                    running = excluded.running,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    target_count = excluded.target_count,
                    workers = excluded.workers,
                    source = excluded.source,
                    profile_id = excluded.profile_id,
                    last_error = excluded.last_error,
                    completed_count = excluded.completed_count,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    current_stage = excluded.current_stage,
                    current_email = excluded.current_email,
                    updated_at = excluded.updated_at
                """,
                payload,
            )

    def latest_web_batch_id(self) -> str:
        """回退：从结果表推断最近一个非空 web 批次号。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT batch_id
                FROM registration_results
                WHERE batch_id IS NOT NULL AND trim(batch_id) != ''
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["batch_id"] if row else "") or ""

    # ------------------------------------------------------------------
    # Sub2API Profile 管理（sub2api_profiles 表）
    # ------------------------------------------------------------------

    _PROFILE_SELECT = """
        SELECT id, name, site_key, register_url, register_origin, promo_code,
               invitation_code, aff_code, email_domain_whitelist,
               enabled, created_at, updated_at
        FROM sub2api_profiles
    """

    def _profile_from_row(self, row) -> Dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["whitelist"] = self._profile_whitelist(data.get("email_domain_whitelist"))
        return data

    @staticmethod
    def _profile_whitelist(raw: Any) -> List[str]:
        """解析存储的白名单 JSON；损坏数据 fail-closed，绝不静默当作无限制。

        [] 语义是“无域名限制”——把损坏 JSON 解析成 [] 等于放开全部域名，
        与白名单业务语义相反。因此无效/非 list 一律响亮报错。
        """
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                raise ProfileError(
                    f"Profile 邮箱域名白名单数据损坏（非法 JSON），拒绝启动以防误放行: {exc}"
                ) from exc
        else:
            parsed = raw
        if not isinstance(parsed, list):
            raise ProfileError(
                "Profile 邮箱域名白名单数据损坏（非列表），拒绝启动以防误放行"
            )
        return [str(item).lower() for item in parsed if str(item).strip()]

    def list_profiles(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(self._PROFILE_SELECT + " ORDER BY id ASC").fetchall()
        return [self._profile_from_row(row) for row in rows]

    def get_profile(self, profile_id: int | str) -> Optional[Dict[str, Any]]:
        try:
            normalized_id = int(profile_id)
        except (TypeError, ValueError):
            return None
        if normalized_id <= 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._PROFILE_SELECT + " WHERE id = ?", (normalized_id,)
            ).fetchone()
        return self._profile_from_row(row) if row else None

    def get_profile_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        normalized = str(name or "").strip()
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                self._PROFILE_SELECT + " WHERE name = ? COLLATE NOCASE LIMIT 1",
                (normalized,),
            ).fetchone()
        return self._profile_from_row(row) if row else None

    def profile_has_usage(self, profile_id: int | str) -> bool:
        """Profile 是否已被使用（注册记录或消费账本存在该 profile_id 行）。

        使用判定只看 profile_id 作用域：一旦任何邮箱在该 Profile 下被
        消费/记录，origin 即冻结、不可删除（只能禁用）。
        """
        try:
            normalized_id = int(profile_id)
        except (TypeError, ValueError):
            return False
        if normalized_id <= 0:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM accounts WHERE profile_id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
            if row:
                return True
            row = conn.execute(
                """
                SELECT 1 FROM registration_results
                WHERE profile_id = ? LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            if row:
                return True
            row = conn.execute(
                """
                SELECT 1 FROM mailbox_consumptions
                WHERE profile_id = ? LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
        return bool(row)

    def create_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ProfileError("Profile 名称不能为空")
        if len(name) > 120:
            raise ProfileError("Profile 名称过长（<=120 字符）")
        site = get_verified_site(str(payload.get("site_key") or ""))
        if site is None:
            raise ProfileError("必须选择已验证的注册站点")
        purpose = "register"
        if payload.get("register_url") not in (None, "", site.register_url):
            raise ProfileError("Register URL 不允许自定义")
        register_url = normalize_register_url(site.register_url)
        register_origin = register_url_origin(register_url)
        if self.get_profile_by_name(name) is not None:
            raise ProfileNameConflictError(f"Profile 名称已存在: {name}")
        requested_whitelist = payload.get("email_domain_whitelist")
        whitelist = normalize_domain_whitelist(site.email_suffix_whitelist)
        if requested_whitelist not in (None, "", []) and normalize_domain_whitelist(requested_whitelist) != whitelist:
            raise ProfileError("邮箱域名白名单由已验证站点目录管理，不允许自定义")
        aff_code = str(payload.get("aff_code") or "").strip() or site.default_aff_code
        now = self.now_text()
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO sub2api_profiles
                        (name, site_key, purpose, register_url, register_origin, promo_code,
                         invitation_code, aff_code, email_domain_whitelist,
                         enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        site.key,
                        purpose,
                        register_url,
                        register_origin,
                        str(payload.get("promo_code") or "").strip(),
                        str(payload.get("invitation_code") or "").strip(),
                        aff_code,
                        json.dumps(whitelist, ensure_ascii=False),
                        1 if payload.get("enabled", True) else 0,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProfileNameConflictError(f"Profile 名称已存在: {name}") from exc
            profile_id = int(cursor.lastrowid)
        return self.get_profile(profile_id)  # type: ignore[return-value]

    def update_profile(self, profile_id: int | str, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"Profile 不存在: {profile_id}")
        existing_id = int(profile["id"])
        existing_origin = str(profile.get("register_origin") or "")

        # 名称：重命名不影响作用域（identity = id）。
        new_name = str(payload.get("name") or profile["name"]).strip()
        if not new_name:
            raise ProfileError("Profile 名称不能为空")
        if len(new_name) > 120:
            raise ProfileError("Profile 名称过长（<=120 字符）")

        site_key = str(payload.get("site_key") or profile.get("site_key") or "").strip().lower()
        site = get_verified_site(site_key)
        if site is None:
            raise ProfileError("必须选择已验证的注册站点")
        purpose = "register"
        if payload.get("register_url") not in (None, "", site.register_url):
            raise ProfileError("Register URL 不允许自定义")
        new_url = normalize_register_url(site.register_url)
        new_origin = register_url_origin(new_url)

        if new_origin != existing_origin:
            if self.profile_has_usage(existing_id):
                raise ProfileOriginLockedError(
                    "该 Profile 已被使用，origin 不可更改；请创建新的 Profile"
                )
        if new_name != profile["name"]:
            conflict = self.get_profile_by_name(new_name)
            if conflict is not None and int(conflict["id"]) != existing_id:
                raise ProfileNameConflictError(f"Profile 名称已存在: {new_name}")

        requested_whitelist = payload.get("email_domain_whitelist")
        whitelist = normalize_domain_whitelist(site.email_suffix_whitelist)
        if requested_whitelist not in (None, "", []) and normalize_domain_whitelist(requested_whitelist) != whitelist:
            raise ProfileError("邮箱域名白名单由已验证站点目录管理，不允许自定义")
        new_enabled = 1 if payload.get("enabled", profile.get("enabled", True)) else 0
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    UPDATE sub2api_profiles
                    SET name = ?,
                        site_key = ?,
                        purpose = ?,
                        register_url = ?,
                        register_origin = ?,
                        promo_code = ?,
                        invitation_code = ?,
                        aff_code = ?,
                        email_domain_whitelist = ?,
                        enabled = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_name,
                        site.key,
                        purpose,
                        new_url,
                        new_origin,
                        str(payload.get("promo_code", profile.get("promo_code")) or "").strip(),
                        str(payload.get("invitation_code", profile.get("invitation_code")) or "").strip(),
                        str(payload.get("aff_code", profile.get("aff_code")) or "").strip(),
                        json.dumps(whitelist, ensure_ascii=False),
                        new_enabled,
                        self.now_text(),
                        existing_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProfileNameConflictError(f"Profile 名称已存在: {new_name}") from exc
            if not cursor.rowcount:
                raise ProfileNotFoundError(f"Profile 不存在: {profile_id}")
        return self.get_profile(existing_id)  # type: ignore[return-value]

    def delete_profile(self, profile_id: int | str) -> bool:
        profile = self.get_profile(profile_id)
        if profile is None:
            return False
        if self.profile_has_usage(int(profile["id"])):
            raise ProfileInUseError(
                "该 Profile 已被使用，不能删除；请改为禁用（enabled=false）"
            )
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sub2api_profiles WHERE id = ?", (int(profile["id"]),)
            )
        return bool(cursor.rowcount)
