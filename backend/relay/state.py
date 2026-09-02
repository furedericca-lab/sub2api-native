from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict

class RelayState:
    VERSION = 3

    def __init__(self, data_root: Path):
        root = Path(data_root) / "relay"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "relay_state.sqlite3"
        self.meta_path = root / "relay_state.json"
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0] or 0)
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if version not in {0, 1, 2, self.VERSION} or (version == 0 and tables):
                raise RuntimeError(
                    f"unsupported relay database schema (user_version={version})"
                )
            if version == self.VERSION and "relay_pool" in tables:
                raise RuntimeError("relay schema v3 cannot contain legacy relay_pool")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS relay_cursor (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL DEFAULT 0);
            INSERT OR IGNORE INTO relay_cursor(id,value) VALUES(1,0);
            CREATE TABLE IF NOT EXISTS relay_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, model TEXT NOT NULL,
              account_id INTEGER NOT NULL, site_key TEXT NOT NULL, stream INTEGER NOT NULL DEFAULT 0,
              outcome TEXT NOT NULL, http_status INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
              retries INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS relay_sessions (
              session_key TEXT PRIMARY KEY, account_id INTEGER NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relay_runtime (
              account_id INTEGER PRIMARY KEY, in_flight INTEGER NOT NULL DEFAULT 0,
              cooldown_until REAL NOT NULL DEFAULT 0, last_used_at REAL NOT NULL DEFAULT 0,
              last_status TEXT NOT NULL DEFAULT '', last_http_status INTEGER NOT NULL DEFAULT 0,
              models_json TEXT NOT NULL DEFAULT '[]', models_updated_at REAL NOT NULL DEFAULT 0
            );
            """)
            if version == 1:
                db.execute("""INSERT OR IGNORE INTO relay_runtime(account_id,in_flight,cooldown_until,last_used_at,last_status,last_http_status,models_json,models_updated_at)
                    SELECT account_id,0,cooldown_until,last_used_at,last_status,last_http_status,models_json,models_updated_at FROM relay_pool""")
            if version == 0:
                db.execute(f"PRAGMA user_version={self.VERSION}")
            db.execute("UPDATE relay_runtime SET in_flight=0")

    def legacy_pool_rows(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0] or 0)
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_pool'"
            ).fetchone()
            if version not in {1, 2} or not exists:
                return []
            result = [dict(row) for row in db.execute("SELECT * FROM relay_pool ORDER BY account_id")]
        return result

    def finalize_legacy_pool_migration(self) -> None:
        with self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0] or 0)
            if version == self.VERSION:
                return
            if version not in {1, 2}:
                raise RuntimeError(
                    f"cannot finalize relay migration from user_version={version}"
                )
            db.execute("DROP TABLE IF EXISTS relay_pool")
            db.execute(f"PRAGMA user_version={self.VERSION}")

    def remap_runtime(self, old_account_id: int, new_account_id: int) -> None:
        if int(old_account_id) == int(new_account_id): return
        with self._connect() as db:
            row = db.execute("SELECT * FROM relay_runtime WHERE account_id=?", (int(old_account_id),)).fetchone()
            if not row: return
            db.execute("""INSERT INTO relay_runtime(account_id,in_flight,cooldown_until,last_used_at,last_status,last_http_status,models_json,models_updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET cooldown_until=excluded.cooldown_until,last_used_at=excluded.last_used_at,last_status=excluded.last_status,last_http_status=excluded.last_http_status,models_json=excluded.models_json,models_updated_at=excluded.models_updated_at""",
                (int(new_account_id),0,row["cooldown_until"],row["last_used_at"],row["last_status"],row["last_http_status"],row["models_json"],row["models_updated_at"]))
            db.execute("DELETE FROM relay_runtime WHERE account_id=?", (int(old_account_id),))
            db.execute("UPDATE relay_requests SET account_id=? WHERE account_id=?", (int(new_account_id), int(old_account_id)))

    def runtime_rows(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._connect() as db:
            runtime = {int(row["account_id"]): dict(row) for row in db.execute("SELECT * FROM relay_runtime")}
        result = []
        for asset in assets:
            row = dict(asset); state = runtime.get(int(row["account_id"]), {})
            row.update({"in_flight": 0, "cooldown_until": 0, "last_used_at": 0, "last_status": "", "last_http_status": 0, "models_json": "[]", "models_updated_at": 0, **state})
            row["models"] = json.loads(row.pop("models_json") or "[]"); result.append(row)
        return result

    def candidates(self, model: str, strategy: str, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = time.time(); rows = [r for r in self.runtime_rows(assets) if r.get("enabled", True) and model in r["models"] and r["cooldown_until"] <= now]
        rows.sort(key=lambda row: row["account_id"])
        if strategy == "round_robin" and rows:
            with self._connect() as db:
                cursor = int(db.execute("SELECT value FROM relay_cursor WHERE id=1").fetchone()[0]); db.execute("UPDATE relay_cursor SET value=? WHERE id=1", (cursor + 1,))
            offset = cursor % len(rows); rows = rows[offset:] + rows[:offset]
        return rows

    def session_account(self, session_key: str, ttl_seconds: float = 3600) -> int:
        if not session_key: return 0
        with self._connect() as db:
            row = db.execute("SELECT account_id,updated_at FROM relay_sessions WHERE session_key=?", (session_key,)).fetchone()
        if not row or time.time() - float(row["updated_at"]) > max(60, ttl_seconds): return 0
        return int(row["account_id"])

    def bind_session(self, session_key: str, account_id: int) -> None:
        if session_key:
            with self._connect() as db:
                db.execute("INSERT INTO relay_sessions(session_key,account_id,updated_at) VALUES(?,?,?) ON CONFLICT(session_key) DO UPDATE SET account_id=excluded.account_id,updated_at=excluded.updated_at", (session_key, account_id, time.time()))

    def no_candidate_reason(self, model: str, assets: list[dict[str, Any]]) -> str:
        enabled = [row for row in self.runtime_rows(assets) if row.get("enabled", True)]
        if not enabled:
            return "no_pool_member"
        serving = [row for row in enabled if model in row["models"]]
        if not serving:
            return "model_not_served"
        return "all_members_cooling_down"

    def update_models(self, account_id: int, models: list[str]) -> None:
        with self._connect() as db: db.execute("INSERT INTO relay_runtime(account_id,models_json,models_updated_at) VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET models_json=excluded.models_json,models_updated_at=excluded.models_updated_at", (account_id, json.dumps(models), time.time()))

    def models_stale(self, row: dict[str, Any], ttl_seconds: float) -> bool:
        return time.time() - float(row.get("models_updated_at") or 0) >= max(1, ttl_seconds)

    def mark(self, account_id: int, outcome: str, status: int = 0, cooldown: float = 0) -> None:
        with self._connect() as db: db.execute("INSERT INTO relay_runtime(account_id,last_used_at,last_status,last_http_status,cooldown_until) VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET last_used_at=excluded.last_used_at,last_status=excluded.last_status,last_http_status=excluded.last_http_status,cooldown_until=excluded.cooldown_until", (account_id, time.time(), outcome, status, time.time() + cooldown))

    def adjust_in_flight(self, account_id: int, delta: int) -> None:
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO relay_runtime(account_id) VALUES(?)", (account_id,))
            db.execute("UPDATE relay_runtime SET in_flight=MAX(0,in_flight+?) WHERE account_id=?", (int(delta), account_id))

    def log_request(self, model: str, account_id: int, site_key: str, stream: bool, outcome: str, status: int, duration_ms: int, retries: int = 0) -> None:
        with self._connect() as db: db.execute("INSERT INTO relay_requests(created_at,model,account_id,site_key,stream,outcome,http_status,duration_ms,retries) VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), model, account_id, site_key, int(stream), outcome, status, duration_ms, retries))

    def requests(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db: return [dict(r) for r in db.execute("SELECT * FROM relay_requests ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def rotate_credential(self) -> str:
        import hashlib, secrets
        raw = "sk-relay-" + secrets.token_urlsafe(32)
        self.meta_path.write_text(json.dumps({"key_hash": hashlib.sha256(raw.encode()).hexdigest()}, ensure_ascii=True), encoding="utf-8")
        try: self.meta_path.chmod(0o600)
        except OSError: pass
        return raw

    def authorize(self, value: str) -> bool:
        import hashlib, hmac
        try: stored = json.loads(self.meta_path.read_text(encoding="utf-8")).get("key_hash", "")
        except (OSError, ValueError): return False
        return bool(value and stored and hmac.compare_digest(stored, hashlib.sha256(value.encode()).hexdigest()))
