#!/usr/bin/env python3
"""Synchronize the embedded OutlookEmail launch credential without printing it.

This is an operator recovery tool for the exceptional case where the native
OutlookEmail UI changed its own login password. It validates the supplied
password through OutlookEmail's extension-login HTTP API before atomically
updating the private runtime file. It never opens the OutlookEmail database.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE = "http://127.0.0.1:5000"
DEFAULT_RUNTIME_ENV = "/app/data/outlookemail/runtime.env"


def read_runtime_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if key not in {"LOGIN_PASSWORD", "SECRET_KEY"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def verify_password(base: str, password: str, *, timeout: float = 8.0) -> bool:
    url = f"{base.rstrip('/')}/api/extension/login"
    body = json.dumps({"password": password, "next": "/"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False
    try:
        payload: Any = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        200 <= int(response.status) < 300
        and isinstance(payload, dict)
        and payload.get("success") is True
        and str(payload.get("launch_url") or "").startswith("/extension-login/")
    )


def write_runtime_env(path: Path, secret_key: str, login_password: str) -> None:
    original = path.stat()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"LOGIN_PASSWORD={login_password}\n")
            stream.write(f"SECRET_KEY={secret_key}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if os.geteuid() == 0:
            os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def sync_runtime_password(path: Path, base: str, password: str) -> bool:
    current = read_runtime_env(path)
    secret_key = str(current.get("SECRET_KEY") or "")
    if not secret_key or not password:
        return False
    if not verify_password(base, password):
        return False
    write_runtime_env(path, secret_key, password)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-env",
        default=os.environ.get("OUTLOOKEMAIL_RUNTIME_ENV", DEFAULT_RUNTIME_ENV),
    )
    parser.add_argument("--base", default=DEFAULT_BASE)
    args = parser.parse_args(argv)

    path = Path(args.runtime_env).expanduser()
    base = str(args.base or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if not path.is_file() or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print("runtime credential synchronization is not available", file=os.sys.stderr)
        return 1
    password = getpass.getpass("OutlookEmail login password: ")
    if not sync_runtime_password(path, base, password):
        print("credential validation failed; runtime file was not changed", file=os.sys.stderr)
        return 1
    print("runtime launch credential synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
