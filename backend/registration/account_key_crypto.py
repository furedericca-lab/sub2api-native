from __future__ import annotations

import base64
import os
import secrets
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AccountKeyCrypto:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.key_path = self.data_root / "accounts" / "api_keys.key"
        self.registration_db_path = (
            self.data_root / "accounts" / "registration_results.sqlite3"
        )
        self.legacy_relay_db_path = self.data_root / "relay" / "relay_state.sqlite3"

    @staticmethod
    def _read_key(path: Path) -> bytes:
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError(f"account API Key encryption key has invalid length: {path}")
        return key

    @staticmethod
    def _table_has_ciphertext(path: Path, table: str) -> bool:
        if not path.exists():
            return False
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    return False
                return db.execute(
                    f"SELECT 1 FROM {table} WHERE key_ciphertext <> '' LIMIT 1"
                ).fetchone() is not None
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"cannot inspect persisted Account API Key ciphertext in {path}"
            ) from exc

    def has_persisted_ciphertext(self) -> bool:
        return self._table_has_ciphertext(
            self.registration_db_path, "account_api_keys"
        ) or self._table_has_ciphertext(self.legacy_relay_db_path, "relay_pool")

    def initialize(self) -> None:
        if self.key_path.exists():
            self._read_key(self.key_path)
            return
        if self.has_persisted_ciphertext():
            raise RuntimeError(
                "Account API Key ciphertext exists but data/accounts/api_keys.key is missing"
            )

    def _key(self, *, create: bool) -> bytes:
        self.initialize()
        if not self.key_path.exists():
            if not create:
                raise RuntimeError("Account API Key encryption key is missing")
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self._read_key(self.key_path)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            return key
        return self._read_key(self.key_path)

    def encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key(create=True)).encrypt(
            nonce, str(value).encode(), None
        )
        return (
            "gcm:"
            + base64.urlsafe_b64encode(nonce).decode()
            + ":"
            + base64.urlsafe_b64encode(ciphertext).decode()
        )

    def decrypt(self, value: str) -> str:
        parts = str(value).split(":", 2)
        if len(parts) != 3 or parts[0] != "gcm":
            raise ValueError("invalid Account API Key ciphertext")
        nonce = base64.urlsafe_b64decode(parts[1].encode())
        ciphertext = base64.urlsafe_b64decode(parts[2].encode())
        return AESGCM(self._key(create=False)).decrypt(nonce, ciphertext, None).decode()
