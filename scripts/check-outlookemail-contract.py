#!/usr/bin/env python3
"""Small, credential-safe smoke for the embedded OutlookEmail HTTP contract.

The script deliberately talks to the public upstream endpoints only.  It does
not import the upstream application or inspect its SQLite database, so it can
be run after an OutlookEmail submodule bump without coupling to its internals.

Failure policy
--------------
The smoke separates "this deployment is broken" from "this probe is slow":

* ``/`` , ``/api/extension/login`` and ``/api/external/accounts`` are strict.
  A timeout, an unreachable endpoint, or a shape mismatch fails the run.
* ``/api/external/emails`` fans out to Microsoft login and Graph, so its
  latency tail is owned by an external provider.  It gets its own larger time
  budget (``--emails-timeout``) plus at most one bounded retry.  A pure
  provider-side timeout is reported as ``external-emails: unverified`` and exits
  0; a definite HTTP or shape contract violation still exits 1.

That distinction exists because a fixed 8-second budget on the fan-out endpoint
produced repeated false failures on healthy deployments: the client gave up at
8s while the upstream access log recorded every request as ``status=200``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

# The emails endpoint is a provider fan-out; its budget must stay inside the
# 20-30s window agreed for the operator's own waiting tolerance.
EMAILS_TAIL_BUDGET_SECONDS = 25.0
MAX_EMAILS_RETRIES = 1


class ContractError(Exception):
    """Base class for probe-level transport failures."""


class ContractTimeout(ContractError):
    """The request exceeded its time budget."""


class EndpointUnreachable(ContractError):
    """The endpoint refused, dropped, or could not be resolved."""


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
    except (socket.timeout, TimeoutError) as exc:
        raise ContractTimeout("request timed out") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ContractTimeout("request timed out") from exc
        raise EndpointUnreachable("endpoint unreachable") from exc
    except OSError as exc:
        raise EndpointUnreachable("endpoint unreachable") from exc


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


def probe_external_emails(
    opener: urllib.request.OpenerDirector,
    base: str,
    api_key: str,
    email: str,
    *,
    tail_timeout: float,
    retries: int,
) -> tuple[str, str]:
    """Probe the provider fan-out endpoint.

    Returns ``(verdict, detail)`` where verdict is ``ok``, ``unverified`` (the
    provider stayed inside its budget tail, contract never observed broken) or
    ``fail`` (a definite contract violation or an unreachable endpoint).
    A contract violation is never retried.
    """
    query = urllib.parse.urlencode({"email": email, "folder": "inbox", "top": 1})
    url = f"{base}/api/external/emails?{query}"
    attempts = 1 + max(0, min(retries, MAX_EMAILS_RETRIES))
    for attempt in range(1, attempts + 1):
        try:
            status, data = _request(
                opener, "GET", url, api_key=api_key, timeout=tail_timeout
            )
        except ContractTimeout:
            if attempt < attempts:
                print(
                    f"external-emails: retrying once after "
                    f"{tail_timeout:g}s provider tail latency"
                )
                time.sleep(min(1.0, tail_timeout / 10))
                continue
            return "unverified", f"no answer within {tail_timeout:g}s per attempt"
        except EndpointUnreachable:
            return "fail", "endpoint unreachable"
        if (
            not 200 <= status < 300
            or not isinstance(data, Mapping)
            or not isinstance(data.get("success"), bool)
        ):
            return "fail", "contract failed"
        if data.get("success"):
            return "ok", "ok"
        # success=false is a valid upstream business response (for example a
        # temporarily unauthorized mailbox); shape compatibility still passed.
        return "ok", "reachable (upstream reported unavailable)"
    return "unverified", "exhausted attempts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=os.environ.get("OUTLOOKEMAIL_CONTRACT_BASE", "http://127.0.0.1:5000"),
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--emails-timeout",
        type=float,
        default=float(os.environ.get("OUTLOOKEMAIL_EMAILS_TIMEOUT", EMAILS_TAIL_BUDGET_SECONDS)),
        help="per-attempt budget for the Microsoft/Graph fan-out endpoint",
    )
    parser.add_argument(
        "--emails-retries",
        type=int,
        default=int(os.environ.get("OUTLOOKEMAIL_EMAILS_RETRIES", MAX_EMAILS_RETRIES)),
        help=f"extra attempts for the fan-out endpoint, capped at {MAX_EMAILS_RETRIES}",
    )
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
    except ContractError:
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
    except ContractError:
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
    except ContractError:
        return _fail("external accounts endpoint unavailable")
    accounts = data.get("accounts") if isinstance(data, Mapping) else None
    if not 200 <= status < 300 or not isinstance(data, Mapping) or data.get("success") is not True or not isinstance(accounts, list):
        return _fail("external accounts contract failed")
    print(f"external-accounts: ok ({len(accounts)})")

    email = next((_email_from_item(item) for item in accounts), "")
    if not email:
        print("external-emails: skipped (no account in pool)")
        return 0
    verdict, detail = probe_external_emails(
        opener,
        base,
        api_key,
        email,
        tail_timeout=args.emails_timeout,
        retries=args.emails_retries,
    )
    if verdict == "fail":
        return _fail(f"external emails {detail}")
    if verdict == "unverified":
        print(f"external-emails: unverified ({detail}); provider tail, contract not violated")
        return 0
    print("external-emails: ok" if detail == "ok" else f"external-emails: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
