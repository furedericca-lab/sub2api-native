#!/usr/bin/env python3
"""Small, credential-safe smoke for the embedded OutlookEmail HTTP contract.

The script deliberately talks to the public upstream endpoints only.  It does
not import the upstream application or inspect its SQLite database, so it can
be run after an OutlookEmail submodule bump without coupling to its internals.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping


def _read_env(path: Path) -> dict[str, str]:
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
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(char.isalnum() or char == "_" for char in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _load_config(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    api_key: str = "",
    timeout: float,
) -> tuple[int, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if payload is not None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            try:
                data: Any = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeError, json.JSONDecodeError):
                data = None
            return int(response.status), data
    except urllib.error.HTTPError as exc:
        # A status is enough for the smoke result; never print upstream bodies.
        return int(exc.code), None
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("request failed") from exc


def _email_from_item(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    for key in ("email", "address", "name"):
        value = str(item.get(key) or "").strip()
        if "@" in value:
            return value
    return ""


def _fail(message: str) -> int:
    print(f"[outlookemail-contract] FAIL: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=os.environ.get("OUTLOOKEMAIL_CONTRACT_BASE", "http://127.0.0.1:5000"),
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--runtime-env",
        default=os.environ.get(
            "OUTLOOKEMAIL_RUNTIME_ENV", "/app/data/outlookemail/runtime.env"
        ),
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("SUB2API_CONFIG_FILE", "/app/data/config.json"),
    )
    args = parser.parse_args(argv)

    base = str(args.base or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _fail("invalid base URL")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        status, _ = _request(opener, "GET", f"{base}/", timeout=args.timeout)
    except RuntimeError:
        return _fail("root endpoint unavailable")
    if not 200 <= status < 400:
        return _fail("root endpoint returned an unexpected status")
    print("root: ok")

    runtime = _read_env(Path(args.runtime_env).expanduser())
    password = str(os.environ.get("OUTLOOKEMAIL_LOGIN_PASSWORD") or runtime.get("LOGIN_PASSWORD") or os.environ.get("LOGIN_PASSWORD") or "")
    if not password:
        return _fail("LOGIN_PASSWORD is not configured for the smoke")
    try:
        status, data = _request(
            opener,
            "POST",
            f"{base}/api/extension/login",
            payload={"password": password, "next": "/"},
            timeout=args.timeout,
        )
    except RuntimeError:
        return _fail("extension login endpoint unavailable")
    launch_path = str(data.get("launch_url") or "") if isinstance(data, Mapping) else ""
    launch_parts = urllib.parse.urlsplit(launch_path)
    if not 200 <= status < 300 or not isinstance(data, Mapping) or not data.get("success") or launch_parts.scheme or launch_parts.netloc or not launch_parts.path.startswith("/extension-login/"):
        return _fail("extension login contract failed")
    print("extension-login: ok")

    config = _load_config(Path(args.config).expanduser())
    api_key = str(os.environ.get("OUTLOOKEMAIL_API_KEY") or config.get("outlookemail_api_key") or "").strip()
    if not api_key:
        return _fail("outlookemail_api_key is not configured for the smoke")
    try:
        status, data = _request(
            opener,
            "GET",
            f"{base}/api/external/accounts?limit=10000&offset=0",
            api_key=api_key,
            timeout=args.timeout,
        )
    except RuntimeError:
        return _fail("external accounts endpoint unavailable")
    accounts = data.get("accounts") if isinstance(data, Mapping) else None
    if not 200 <= status < 300 or not isinstance(data, Mapping) or data.get("success") is not True or not isinstance(accounts, list):
        return _fail("external accounts contract failed")
    print(f"external-accounts: ok ({len(accounts)})")

    email = next((_email_from_item(item) for item in accounts), "")
    if not email:
        print("external-emails: skipped (no account in pool)")
        return 0
    query = urllib.parse.urlencode({"email": email, "folder": "inbox", "top": 1})
    try:
        status, data = _request(
            opener,
            "GET",
            f"{base}/api/external/emails?{query}",
            api_key=api_key,
            timeout=args.timeout,
        )
    except RuntimeError:
        return _fail("external emails endpoint unavailable")
    if not 200 <= status < 300 or not isinstance(data, Mapping) or not isinstance(data.get("success"), bool):
        return _fail("external emails contract failed")
    # success=false is a valid upstream business response (for example a
    # temporarily unauthorized mailbox); shape compatibility still passed.
    print("external-emails: ok" if data.get("success") else "external-emails: reachable (upstream reported unavailable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
