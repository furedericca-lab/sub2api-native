"""Managed OutlookEmail service boundary.

The embedded deployment keeps OutlookEmail as an independent Flask process.
This module owns only the small HTTP contract needed by Sub2API Native; it
never opens or inspects the upstream SQLite database.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from backend.shared.paths import DATA_ROOT, PROJECT_ROOT


DEFAULT_API_BASE = "http://127.0.0.1:5000"
LEGACY_SERVICE_BASES = {
    "http://outlook-email:5000",
    "http://outlookemail:5000",
}
DEFAULT_PUBLIC_PORT = 15000
RUNTIME_ENV_FILENAME = "runtime.env"
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class MailboxServiceError(RuntimeError):
    """A safe, user-facing error from the managed mailbox boundary."""


class MailboxUnavailableError(MailboxServiceError):
    """The embedded OutlookEmail process cannot be reached."""


class MailboxPayloadError(MailboxServiceError):
    """OutlookEmail returned a response outside the integration contract."""


@dataclass(frozen=True)
class MailboxStatus:
    healthy: bool
    version: str
    account_count: int | None
    integration_key_configured: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "outlookemail",
            "healthy": self.healthy,
            "version": self.version,
            "account_count": self.account_count,
            "management_port": public_port(),
            "integration_key_configured": self.integration_key_configured,
        }


def runtime_env_path() -> Path:
    """Return the private runtime env path used by the embedded process."""
    override = str(os.environ.get("OUTLOOKEMAIL_RUNTIME_ENV", "") or "").strip()
    if override:
        return Path(override).expanduser()
    return DATA_ROOT / "outlookemail" / RUNTIME_ENV_FILENAME


def read_runtime_env(path: str | Path | None = None) -> dict[str, str]:
    """Read simple ``KEY=value`` pairs without executing a shell file.

    The migration file is operator-owned and intentionally limited to scalar
    environment values. Invalid lines are ignored rather than interpreted.
    """
    source = Path(path) if path is not None else runtime_env_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _config_value(config: Mapping[str, Any] | None, key: str) -> str:
    if not isinstance(config, Mapping):
        return ""
    return str(config.get(key, "") or "").strip()


def normalize_api_base(value: str) -> str:
    base = str(value or "").strip().rstrip("/")
    if not base:
        return DEFAULT_API_BASE
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MailboxServiceError("OutlookEmail API 地址格式无效")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MailboxServiceError("OutlookEmail API 地址不应包含凭据或查询参数")
    return base


def resolve_api_base(config: Mapping[str, Any] | None = None) -> str:
    """Resolve the embedded loopback base while reading old configs safely."""
    configured = _config_value(config, "outlookemail_api_base")
    environment = str(os.environ.get("SUB2API_OUTLOOKEMAIL_API_BASE", "") or "").strip()
    # The embedded container publishes this loopback address as its process
    # boundary.  An explicitly supplied environment value therefore wins over
    # an old config.json value left behind by the former Compose topology.
    # Outside the managed container no value is injected, so an intentional
    # legacy external deployment remains readable until its operator migrates.
    candidate = environment or configured or DEFAULT_API_BASE
    normalized = candidate.rstrip("/")
    # The old Compose service name is not resolvable after consolidation.
    # Treat it as the embedded default; preserve any other explicit endpoint
    # for a deliberate legacy/external deployment.
    if normalized.lower() in LEGACY_SERVICE_BASES:
        normalized = environment.rstrip("/") if environment else DEFAULT_API_BASE
    return normalize_api_base(normalized)


def resolve_runtime_value(
    name: str,
    config: Mapping[str, Any] | None = None,
    *,
    legacy_config_key: str = "",
) -> str:
    """Resolve a private runtime value without exposing it to callers."""
    # A distinct alias makes local process launches explicit and avoids
    # confusing OutlookEmail's own LOGIN_PASSWORD with Sub2API config.
    alias = str(os.environ.get(f"OUTLOOKEMAIL_{name}", "") or "")
    if alias:
        return alias
    # In the embedded container, entrypoint.sh writes this restricted file on
    # first startup. It must win over the inherited bootstrap environment so a
    # deliberate upstream password change can be synchronized without a
    # container restart.
    runtime_path = str(os.environ.get("OUTLOOKEMAIL_RUNTIME_ENV", "") or "").strip()
    if runtime_path:
        file_value = read_runtime_env(runtime_path).get(name, "")
        if file_value:
            return file_value
    direct = str(os.environ.get(name, "") or "")
    if direct:
        return direct
    file_value = read_runtime_env().get(name, "")
    if file_value:
        return file_value
    return _config_value(config, legacy_config_key) if legacy_config_key else ""


def resolve_login_password(config: Mapping[str, Any] | None = None) -> str:
    return resolve_runtime_value(
        "LOGIN_PASSWORD",
        config,
        legacy_config_key="outlookemail_web_password",
    )


def resolve_legacy_session_cookie(config: Mapping[str, Any] | None = None) -> str:
    """Read the old cookie only as a compatibility fallback for temp mail."""
    return _config_value(config, "outlookemail_session_cookie")


def public_port() -> int:
    raw = (
        os.environ.get("OUTLOOKEMAIL_PUBLIC_PORT")
        or os.environ.get("OUTLOOKEMAIL_PORT")
        or str(DEFAULT_PUBLIC_PORT)
    )
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PUBLIC_PORT
    return value if 1 <= value <= 65535 else DEFAULT_PUBLIC_PORT


def public_host(request_host: str = "") -> str:
    """Return the trusted host for the separately published mailbox port.

    Compose passes the exact host-side bind address into the container.  It is
    deliberately preferred over the request Host header so an authenticated
    request cannot turn a one-time OutlookEmail token into an external
    redirect.  The loopback-only fallback keeps direct local development
    usable when Compose is not involved.
    """
    configured = str(os.environ.get("OUTLOOKEMAIL_PUBLIC_HOST", "") or "").strip()
    candidate = configured or str(request_host or "").strip()
    value = candidate.rstrip(".")
    if not value or any(ord(char) < 32 for char in value) or "/" in value or "@" in value:
        raise MailboxServiceError("当前访问主机不适合生成邮箱管理地址")
    try:
        parsed_ip = ipaddress.ip_address(value)
    except ValueError:
        if configured:
            if not _HOST_RE.fullmatch(value) or ".." in value:
                raise MailboxServiceError("邮箱管理地址配置无效")
            return value
        if value.lower() != "localhost":
            raise MailboxServiceError("邮箱管理地址未配置")
        return value
    if parsed_ip.is_unspecified:
        raise MailboxServiceError("邮箱管理地址不能使用通配符")
    if configured or parsed_ip.is_loopback:
        return value
    raise MailboxServiceError("邮箱管理地址未配置")


def _request_json(
    method: str,
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
) -> tuple[int, Mapping[str, str], Any]:
    body = None
    request_headers = {"Accept": "application/json", **dict(headers or {})}
    if payload is not None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(text) if text else None
            except json.JSONDecodeError:
                parsed = None
            return int(response.status), dict(response.headers.items()), parsed
    except urllib.error.HTTPError as exc:
        # HTTP failures prove that the process answered.  Keep only the parsed
        # shape/status for local contract decisions; callers never reflect the
        # response body to a browser or log it.
        raw = exc.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = None
        return int(exc.code), dict(exc.headers.items()), parsed
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise MailboxUnavailableError("OutlookEmail 服务暂不可用") from exc


def _version_from_source() -> str:
    candidates = [
        PROJECT_ROOT / "vendor" / "outlookEmail" / "VERSION",
        Path("/app/vendor/outlookEmail/VERSION"),
    ]
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value if value.lower().startswith("v") else f"v{value}"
    return "unknown"


def _account_count(base: str, api_key: str) -> int | None:
    key = str(api_key or "").strip()
    if not key:
        return None
    try:
        status, _headers, data = _request_json(
            "GET",
            f"{base}/api/external/accounts?limit=1&offset=0",
            headers={"X-API-Key": key},
        )
    except MailboxServiceError:
        return None
    if status < 200 or status >= 300 or not isinstance(data, Mapping):
        return None
    total = data.get("total")
    try:
        return max(0, int(total))
    except (TypeError, ValueError):
        accounts = data.get("accounts")
        return len(accounts) if isinstance(accounts, list) else None


def get_status(config: Mapping[str, Any] | None = None) -> MailboxStatus:
    """Probe the native root and return non-sensitive operator metadata."""
    base = resolve_api_base(config)
    try:
        status, headers, _data = _request_json("GET", f"{base}/")
        healthy = 200 <= status < 400
    except MailboxServiceError:
        raise MailboxUnavailableError("OutlookEmail 服务暂不可用")
    if not healthy:
        raise MailboxUnavailableError("OutlookEmail 服务暂不可用")
    version = ""
    for key, value in headers.items():
        if key.lower() in {"x-outlookemail-version", "x-app-version"}:
            version = str(value or "").strip()
            break
    return MailboxStatus(
        healthy=healthy,
        version=version or _version_from_source(),
        account_count=(
            _account_count(base, _config_value(config, "outlookemail_api_key"))
            if healthy
            else None
        ),
        integration_key_configured=bool(_config_value(config, "outlookemail_api_key")),
    )


def normalize_next_path(value: str) -> str:
    text = str(value or "").strip()
    if not text or not text.startswith("/") or text.startswith("//"):
        return "/"
    if "\\" in text or "\r" in text or "\n" in text:
        return "/"
    return text


def validate_launch_path(value: str) -> str:
    """Accept only the upstream one-time extension-login path."""
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise MailboxPayloadError("OutlookEmail 登录跳转响应无效")
    path = parsed.path or ""
    if not path.startswith("/extension-login/") or path.endswith("/"):
        raise MailboxPayloadError("OutlookEmail 登录跳转响应无效")
    if "\\" in text or "\r" in text or "\n" in text or ".." in path:
        raise MailboxPayloadError("OutlookEmail 登录跳转响应无效")
    return urllib.parse.urlunsplit(("", "", path, parsed.query, ""))


def launch_url(
    config: Mapping[str, Any] | None = None,
    *,
    next_path: str = "/",
) -> str:
    """Request a one-time native UI login URL from OutlookEmail."""
    password = resolve_login_password(config)
    if not password:
        raise MailboxServiceError("OutlookEmail 登录凭据未配置")
    base = resolve_api_base(config)
    try:
        status, _headers, data = _request_json(
            "POST",
            f"{base}/api/extension/login",
            payload={"password": password, "next": normalize_next_path(next_path)},
        )
    except MailboxServiceError:
        raise
    if status < 200 or status >= 300 or not isinstance(data, Mapping) or not data.get("success"):
        raise MailboxPayloadError("OutlookEmail 登录跳转失败")
    path = validate_launch_path(str(data.get("launch_url") or ""))
    return path


def management_url(host: str, path: str, *, scheme: str = "http") -> str:
    """Build a same-host URL for the separately published native port."""
    value = public_host(host)
    try:
        ip = ipaddress.ip_address(value)
        valid_host = value
        if ip.version == 6:
            valid_host = f"[{value}]"
    except ValueError:
        valid_host = value
    normalized_scheme = str(scheme or "http").strip().lower()
    if normalized_scheme not in {"http", "https"}:
        normalized_scheme = "http"
    return f"{normalized_scheme}://{valid_host}:{public_port()}{validate_launch_path(path)}"


__all__ = [
    "DEFAULT_API_BASE",
    "MailboxPayloadError",
    "MailboxServiceError",
    "MailboxStatus",
    "MailboxUnavailableError",
    "get_status",
    "launch_url",
    "management_url",
    "normalize_api_base",
    "normalize_next_path",
    "public_port",
    "public_host",
    "read_runtime_env",
    "resolve_api_base",
    "resolve_legacy_session_cookie",
    "resolve_login_password",
    "runtime_env_path",
    "validate_launch_path",
]
