# -*- coding: utf-8 -*-
"""管理控制台应用。

本模块负责 HTTP 路由、管理员会话、配置读写和静态资源分发；注册执行由
``backend.registration`` 与 ``backend.web.jobs`` 提供。
"""
from __future__ import annotations

import asyncio
import json
import base64
import hashlib
import hmac
import os
import secrets
import time
import traceback
import datetime
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .account_exports import build_credentials_text
from .jobs import job_coordinator
from backend.integrations.proxy import resolve_proxy_url, validate_http_proxy_url
from backend.mailbox import service as mailbox_service
from backend.shared.paths import DATA_ROOT, PROJECT_ROOT, STATIC_ROOT

APP_DIR = PROJECT_ROOT
DATA_DIR = DATA_ROOT
STATIC_DIR = STATIC_ROOT
WEB_SESSION_COOKIE = "sub2api_native_session"
WEB_SESSION_TTL = 60 * 60 * 24 * 7
WEB_AUTH_FILE = DATA_DIR / "web_auth.json"
LEGACY_WEB_AUTH_FILE = APP_DIR / "web_auth.json"
MAX_BATCH_ACCOUNT_IDS = 10000
_account_remote_guard = threading.Lock()

CONFIG_PUBLIC_KEYS = (
    "outlookemail_api_base",
    "outlookemail_api_key",
    "outlookemail_source",
    "outlookemail_group_id",
    "outlookemail_web_password",
    "outlookemail_session_cookie",
    "outlookemail_temp_tag_ids",
    "outlookemail_folder",
    "outlookemail_top",
    "outlookemail_pick_mode",
    "proxy",
    "debug_mode",
    "browser_locale",
    "close_browser_on_stop",
    "log_level",
    "register_count",
    "user_agent",
    "account_interval",
    "relay_enabled",
    "relay_strategy",
    "relay_proxy",
    "relay_request_timeout_seconds",
    "relay_first_byte_timeout_seconds",
    "relay_cooldown_seconds",
    "relay_rate_cooldown_seconds",
    "relay_model_cache_ttl_seconds",
    "relay_max_attempts",
    "relay_session_affinity_ttl_seconds",
)

SENSITIVE_HINT_KEYS = {
    "outlookemail_api_key",
    "outlookemail_web_password",
    "outlookemail_session_cookie",
    "proxy",
    "relay_proxy",
}


class AccountIdsBody(BaseModel):
    ids: List[int] = Field(default_factory=list)


class DeleteAccountsBody(AccountIdsBody):
    delete_files: bool = True
    release_email: bool = False


class ConfigUpdateBody(BaseModel):
    config: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"


class StartJobBody(BaseModel):
    count: Optional[int] = None
    config: Optional[Dict[str, Any]] = None
    # Profile 是唯一注册作用域（任务级，绝不写入 config.json）。
    profile_id: Optional[int] = None


class CredentialVerificationBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class ApiKeyCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_id: int = Field(gt=0)


class AccountCreateBody(BaseModel):
    profile_id: int = Field(gt=0)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)

class RelayKeyBody(BaseModel):
    key_id: int = Field(gt=0)


class AccountRelayBody(BaseModel):
    enabled: bool


class ApiKeyGroupBody(BaseModel):
    group_id: int = Field(gt=0)


class ProfileBody(BaseModel):
    """Sub2API Profile 创建/更新载荷（无密码字段；id 是身份，name 仅展示）。"""

    name: str = ""
    site_key: Optional[str] = None
    promo_code: Optional[str] = None
    invitation_code: Optional[str] = None
    aff_code: Optional[str] = None
    enabled: Optional[bool] = None

    class Config:
        extra = "forbid"


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""
    confirm_password: str = ""


def _batch_account_ids(ids: List[int]) -> List[int]:
    normalized: List[int] = []
    seen = set()
    for account_id in ids or []:
        if account_id <= 0:
            raise HTTPException(status_code=400, detail="账号 ID 必须是正整数")
        if account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)
        if len(normalized) > MAX_BATCH_ACCOUNT_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"单次最多操作 {MAX_BATCH_ACCOUNT_IDS} 个账号",
            )
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择要操作的账号")
    return normalized


def _gr():
    from backend.registration import engine as gr

    return gr


def _sub2api_account_context(account_id: int):
    from backend.registration.verified_sites import get_verified_site

    gr = _gr()
    store = gr.get_registration_repository()
    rows = store.get_results_by_ids([account_id])
    if not rows:
        raise HTTPException(status_code=404, detail="记录不存在")
    record = rows[0]
    if not bool(record.get("success")) or str(record.get("registration_status")) != "success":
        raise HTTPException(status_code=409, detail="只有注册成功的账号可以管理 API Key")
    email = str(record.get("email") or "").strip()
    password = str(record.get("password") or "")
    if not email or not password:
        raise HTTPException(status_code=409, detail="账号记录缺少登录凭据")
    profile = store.get_profile(record.get("profile_id"))
    if not profile:
        raise HTTPException(status_code=404, detail="账号对应的 Profile 不存在")
    site = get_verified_site(str(profile.get("site_key") or ""))
    if site is None:
        raise HTTPException(status_code=404, detail="账号对应的站点尚未通过验证")
    parsed = urlsplit(site.register_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return gr, record, profile, site, origin, email, password


def gate_l_max_count() -> int:
    """Gate L 代码硬门禁：未通过 count=2 Live 验收前，批量注册保持 count=1。

    默认上限 1；R2（count=2 身份隔离验收）通过后，部署时通过
    SUB2API_GATE_L_MAX_COUNT=1000 恢复批量，或移除本门禁。
    """
    raw = os.environ.get("SUB2API_GATE_L_MAX_COUNT", "1").strip()
    try:
        limit = int(raw)
    except ValueError:
        return 1
    return max(1, min(limit, 1000))


def _load_auth_record() -> Dict[str, str] | None:
    for path in (WEB_AUTH_FILE, LEGACY_WEB_AUTH_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("username") and data.get("password_hash"):
            return {str(key): str(value) for key, value in data.items()}
    return None


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000).hex()


def _create_auth_record(username: str, password: str) -> Dict[str, str]:
    salt = secrets.token_bytes(16)
    return {
        "username": username,
        "password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "password_hash": _hash_password(password, salt),
        "session_secret": secrets.token_urlsafe(32),
    }


def _save_auth_record(record: Dict[str, str]) -> None:
    WEB_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = WEB_AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, WEB_AUTH_FILE)


def _auth_record() -> Dict[str, str] | None:
    return _load_auth_record()


def _web_auth_enabled() -> bool:
    return _auth_record() is not None


def _sign_session(username: str, expires_at: int, secret: str) -> str:
    payload = f"{username}\n{expires_at}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_session(value: str) -> bool:
    record = _auth_record()
    username = str((record or {}).get("username") or "")
    secret = str((record or {}).get("session_secret") or "")
    if not username or not secret or not value or "." not in value:
        return False
    encoded, signature = value.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        raw_username, raw_expires = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8").split("\n", 1)
        return hmac.compare_digest(raw_username, username) and int(raw_expires) > int(time.time())
    except (ValueError, UnicodeError, base64.binascii.Error):
        return False


def _auth_required_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return path not in {
        "/api/health",
        "/api/auth/login",
        "/api/auth/setup",
        "/api/auth/me",
        "/api/auth/logout",
    }


def _public_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    gr = _gr()
    for key in CONFIG_PUBLIC_KEYS:
        if key in raw:
            value = raw.get(key)
            # OutlookEmail credentials are write-only. Returning an empty value
            # lets the existing config editor preserve them without exposing the
            # secret; the UI uses the separate configured flag below.
            if key in {"outlookemail_api_key", "outlookemail_web_password", "outlookemail_session_cookie"}:
                out[key] = ""
            elif key == "outlookemail_api_base":
                try:
                    out[key] = mailbox_service.resolve_api_base(raw)
                except Exception:
                    out[key] = mailbox_service.DEFAULT_API_BASE
            else:
                out[key] = value
        elif key in gr.DEFAULT_CONFIG:
            out[key] = gr.DEFAULT_CONFIG.get(key)
    out["outlookemail_api_key_configured"] = bool(
        str(raw.get("outlookemail_api_key") or "").strip()
    )
    out["outlookemail_runtime_managed"] = True
    out["_sensitive_keys"] = sorted(SENSITIVE_HINT_KEYS)
    return out


def _config_file_snapshot() -> Dict[str, Any]:
    """读取磁盘上的实际 config.json，并返回适合管理端展示的元数据。"""
    gr = _gr()
    path = Path(gr.CONFIG_FILE).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    result: Dict[str, Any] = {
        "path": str(resolved),
        "exists": resolved.is_file(),
        "size": 0,
        "modified_at": "",
        "content": "{}",
        "parse_error": "",
        "sensitive_keys": sorted(SENSITIVE_HINT_KEYS),
    }
    if not resolved.is_file():
        gr.load_config()
        result["content"] = json.dumps(gr.config, ensure_ascii=False, indent=2)
        return result
    try:
        stat = resolved.stat()
        result["size"] = int(stat.st_size)
        result["modified_at"] = datetime.datetime.fromtimestamp(
            stat.st_mtime, tz=datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        if stat.st_size > 2 * 1024 * 1024:
            raise ValueError("config.json 超过 2 MiB")
        raw_text = resolved.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
        result["content"] = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result["parse_error"] = str(exc)
        try:
            result["content"] = resolved.read_text(encoding="utf-8")[: 2 * 1024 * 1024]
        except (OSError, UnicodeError):
            result["content"] = ""
    return result


def _apply_config_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    gr = _gr()
    gr.load_config()
    proxy_update: Optional[str] = None
    if "proxy" in updates:
        proxy_update = str(updates.get("proxy") or "").strip()
        if proxy_update.lower().startswith(("http:", "https:")):
            try:
                proxy_update = validate_http_proxy_url(proxy_update)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"网络代理格式错误: {exc}") from exc
    relay_proxy_update: Optional[str] = None
    if "relay_proxy" in updates:
        relay_proxy_update = str(updates.get("relay_proxy") or "").strip()
        if relay_proxy_update:
            try:
                relay_proxy_update = validate_http_proxy_url(relay_proxy_update)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"聚合出口代理格式错误: {exc}"
                ) from exc
    changed: List[str] = []
    for key in CONFIG_PUBLIC_KEYS:
        if key not in updates:
            continue
        value = updates[key]
        # Secrets are write-only. A blank value or the configured marker from a
        # prior response must leave the existing value untouched.
        if key in {"outlookemail_api_key", "outlookemail_web_password", "outlookemail_session_cookie"}:
            if str(value or "").strip() in {"", "********", "••••••••"}:
                continue
        if key in (
                    "debug_mode",
            "close_browser_on_stop",
                ):
            value = bool(value)
        elif key in (
            "register_count",
            "outlookemail_top",
        ):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if key == "register_count":
                value = max(1, min(value, 1000))
            elif key == "outlookemail_top":
                value = max(1, min(value, 50))
        elif key == "log_level":
            value = str(value or "info").strip().lower() or "info"
        elif key == "browser_locale":
            value = str(value or "en-US").strip()
            if value not in {"en-US", "zh-CN"}:
                value = "en-US"
        elif key == "outlookemail_source":
            value = str(value or "accounts").strip().lower()
            if value not in {"accounts", "temp"}:
                value = "accounts"
        elif key == "outlookemail_pick_mode":
            value = str(value or "random").strip().lower()
            if value not in {"random", "sequential"}:
                value = "random"
        elif key == "relay_strategy":
            value = str(value or "fill_first").strip().lower()
            if value not in {"fill_first", "round_robin"}: value = "fill_first"
        elif key in {"relay_enabled"}:
            value = bool(value)
        elif key == "relay_proxy":
            value = relay_proxy_update or ""
        elif key in {"relay_request_timeout_seconds", "relay_first_byte_timeout_seconds", "relay_cooldown_seconds", "relay_rate_cooldown_seconds", "relay_model_cache_ttl_seconds", "relay_max_attempts", "relay_session_affinity_ttl_seconds"}:
            try: value = max(1, int(value))
            except (TypeError, ValueError): continue
        elif key in (
            "proxy",
            "outlookemail_api_base",
                        ):
            value = proxy_update if key == "proxy" else str(value or "").strip()
        else:
            if isinstance(value, (dict, list)):
                continue
            value = value if isinstance(value, (int, float, bool)) else str(
                value if value is not None else ""
            )
        gr.config[key] = value
        changed.append(key)
    gr.save_config()
    return {"changed": changed, "config": _public_config(gr.config)}


def _serialize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(record or {})
    # Registration-result endpoints are audit-only; credentials belong to Account APIs.
    item.pop("password", None)
    item["status"] = item.get("registration_status") or item.get("status") or "failure"
    item["failure_reason"] = item.get("registration_error", item.get("failure_reason", ""))
    # 邮箱消费语义：consumed 只表示“禁止再次注册”，OutlookEmail 侧保持 active
    item["mail_consumed"] = str(item.get("mail_status") or "").strip().lower() == "consumed"
    item["mailbox_consumed_at"] = item.get("consumed_at", "")
    item["success"] = bool(item.get("success"))
    item["profile_id"] = int(item.get("profile_id") or 0)
    item["screenshot_url"] = (
        f"/api/accounts/{item.get('id')}/failure-screenshot"
        if str(item.get("screenshot_path") or "").strip()
        else ""
    )
    extra = item.get("extra_json") or "{}"
    if isinstance(extra, str):
        try:
            item["extra"] = json.loads(extra) if extra.strip() else {}
        except Exception:
            item["extra"] = {"raw": extra}
    else:
        item["extra"] = extra
    extra_data = item["extra"] if isinstance(item["extra"], dict) else {}
    # acquire 时冻结的邮箱来源（accounts/temp），审计用
    item["mailbox_source"] = str(extra_data.get("mailbox_source") or "")
    item["exception_traceback"] = str(extra_data.get("exception_traceback") or "")
    item["exception_type"] = str(extra_data.get("exception_type") or "")
    item["has_exception_traceback"] = bool(item["exception_traceback"])
    return item


def _path_within(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _stream_file(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """按固定块读取文件，让响应在首块就绪后立即进入浏览器下载队列。"""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def _failure_screenshot_file(record: Dict[str, Any]) -> tuple[Path, str]:
    raw_path = str(record.get("screenshot_path") or "").strip()
    if not raw_path:
        raise FileNotFoundError("该记录没有失败截图")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    screenshot_roots = [
        DATA_DIR / "screenshots" / "registration-failures",
    ]
    if not _path_within(path, screenshot_roots) or not path.is_file():
        raise FileNotFoundError("失败截图文件不存在")
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media_type = media_types.get(path.suffix.lower())
    if not media_type:
        raise ValueError("失败截图格式不受支持")
    return path.resolve(), media_type


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sub2API Native Web",
        description="Lightweight console for register / list / manage accounts",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from backend.relay.proxy import forward_responses, stream_responses
    from backend.relay.router import RelayRouter
    from backend.relay.state import RelayState
    from backend.registration.account_key_crypto import AccountKeyCrypto
    relay_state = RelayState(DATA_DIR)
    account_key_crypto = AccountKeyCrypto(DATA_DIR)
    def _relay_assets() -> List[Dict[str, Any]]:
        assets = _gr().get_registration_repository().relay_assets()
        if not isinstance(assets, list): return []
        for asset in assets:
            asset["secret"] = account_key_crypto.decrypt(str(asset.pop("key_ciphertext")))
            asset["site_key"] = str(asset.get("site_key") or "")
        return assets
    relay_router = RelayRouter(relay_state, _relay_assets)
    relay_model_refresh_lock = asyncio.Lock()
    relay_model_backoff: Dict[int, float] = {}

    @contextmanager
    def _account_remote_slot():
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(
                status_code=409, detail="已有账号远程操作正在执行，请等待完成"
            )
        try:
            try:
                with job_coordinator.idle_guard():
                    yield
            except RuntimeError as exc:
                if job_coordinator.status().get("running"):
                    raise HTTPException(status_code=409, detail="注册任务运行中") from exc
                raise
        finally:
            _account_remote_guard.release()

    @contextmanager
    def _account_operations():
        with _account_remote_slot():
            from backend.integrations.sub2api_account_operations import (
                AccountOperationsService,
            )

            gr = _gr()
            gr.load_config()
            gr._wire_runtime_modules()
            yield AccountOperationsService(
                gr.get_registration_repository(),
                account_key_crypto,
                proxies=gr.get_proxies(),
                log_callback=job_coordinator._append_log,
            )

    def _account_operation_status(exc: BaseException) -> int:
        from backend.integrations.sub2api_account_operations import AccountOperationError
        from backend.integrations.sub2api_keys import (
            ApiKeyCreateUncertainError,
            ApiKeyMutationUncertainError,
            ApiKeyValidationError,
        )

        if isinstance(exc, (ApiKeyCreateUncertainError, ApiKeyMutationUncertainError)):
            return 504
        if isinstance(exc, ApiKeyValidationError):
            return 422
        if isinstance(exc, AccountOperationError):
            return 404 if "不存在" in str(exc) else 422
        return 502

    @app.middleware("http")
    async def require_web_login(request: Request, call_next):
        if _auth_required_path(request.url.path):
            if not _web_auth_enabled():
                return JSONResponse(
                    status_code=401,
                    content={
                        "ok": False,
                        "error": "请先创建管理员账号",
                        "auth_required": True,
                        "setup_required": True,
                    },
                )
            if not _valid_session(request.cookies.get(WEB_SESSION_COOKIE, "")):
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "请先登录", "auth_required": True},
                )
        return await call_next(request)

    @app.on_event("startup")
    def _startup() -> None:
        gr = _gr()
        gr.load_config()
        gr._wire_runtime_modules()
        try:
            store = gr.get_registration_repository()
            account_key_crypto.initialize()
            legacy_rows = relay_state.legacy_pool_rows()
            migrations = []
            for legacy in legacy_rows:
                account_key_crypto.decrypt(str(legacy["key_ciphertext"]))
                account = store.account_for_result(int(legacy["account_id"]))
                if not account:
                    raise RuntimeError(
                        f"legacy Relay member {legacy['account_id']} has no canonical Account"
                    )
                migrations.append((legacy, account))
            for legacy, account in migrations:
                key_row = store.upsert_account_key(int(account["id"]), int(legacy.get("key_id") or 0), "legacy-relay", str(legacy["key_ciphertext"]), int(legacy.get("group_id") or 0), "active")
                store.set_relay_key(int(account["id"]), key_row)
                relay_state.remap_runtime(int(legacy["account_id"]), int(account["id"]))
            relay_state.finalize_legacy_pool_migration()
        except Exception as exc:
            print(f"[web] 初始化 SQLite 失败: {exc}", flush=True)
            raise

    @app.on_event("shutdown")
    def _shutdown() -> None:
        pass

    @app.get("/api/health")
    def api_health() -> Dict[str, Any]:
        return {"ok": True, "service": "sub2api-native-web"}

    def _mailbox_error(status_code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"ok": False, "error": message})

    @app.get("/api/mailbox/status", response_model=None)
    def api_mailbox_status() -> Dict[str, Any] | JSONResponse:
        """Return non-sensitive status for the embedded OutlookEmail process."""
        gr = _gr()
        gr.load_config()
        try:
            return mailbox_service.get_status(gr.config).as_dict()
        except mailbox_service.MailboxUnavailableError:
            return _mailbox_error(503, "邮箱服务暂不可用")
        except mailbox_service.MailboxServiceError:
            # Keep the parent API useful even when a malformed local setting is
            # present, without reflecting upstream response bodies.
            return _mailbox_error(503, "邮箱服务暂不可用")
        except Exception:
            return _mailbox_error(503, "邮箱服务状态检查失败")

    @app.post("/api/mailbox/launch", response_model=None)
    async def api_mailbox_launch(request: Request) -> Dict[str, Any] | JSONResponse:
        """Issue a one-time native OutlookEmail management URL."""
        gr = _gr()
        gr.load_config()
        next_path = "/"
        try:
            payload = await request.json()
            if isinstance(payload, dict):
                next_path = mailbox_service.normalize_next_path(payload.get("next", "/"))
        except (ValueError, TypeError):
            # Empty bodies are valid; malformed optional JSON falls back to the
            # safe root rather than becoming an open redirect primitive.
            next_path = "/"
        try:
            launch_path = mailbox_service.launch_url(gr.config, next_path=next_path)
            host = request.url.hostname or (request.client.host if request.client else "")
            url = mailbox_service.management_url(
                host,
                launch_path,
                scheme=request.url.scheme,
            )
            return {"ok": True, "url": url}
        except mailbox_service.MailboxUnavailableError:
            return _mailbox_error(503, "邮箱服务暂不可用")
        except mailbox_service.MailboxPayloadError:
            return _mailbox_error(502, "邮箱服务登录跳转失败")
        except mailbox_service.MailboxServiceError:
            return _mailbox_error(503, "邮箱管理入口暂不可用")
        except Exception:
            return _mailbox_error(503, "邮箱管理入口暂不可用")

    def _relay_settings() -> Dict[str, Any]:
        gr = _gr(); gr.load_config(); return gr.config

    def _relay_machine_auth(request: Request) -> Optional[JSONResponse]:
        auth = str(request.headers.get("authorization") or "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else str(request.headers.get("x-api-key") or "").strip()
        if not relay_state.authorize(token):
            return JSONResponse(status_code=401, content={"error": {"message": "Invalid relay credential", "type": "authentication_error"}}, headers={"WWW-Authenticate": "Bearer"})
        return None

    async def _refresh_relay_models(*, stale_only: bool) -> Dict[str, int]:
        import httpx
        async with relay_model_refresh_lock:
            settings = _relay_settings()
            proxy = resolve_proxy_url(str(settings.get("relay_proxy") or settings.get("proxy") or "")) or None
            ttl = float(settings.get("relay_model_cache_ttl_seconds") or 900)
            refreshed = failed = skipped = 0
            async def refresh(row):
                nonlocal refreshed, failed, skipped
                if not row.get("enabled") or (stale_only and not relay_state.models_stale(row, ttl)):
                    skipped += 1; return
                if relay_model_backoff.get(int(row["account_id"]), 0) > time.time():
                    skipped += 1; return
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
                        response = await client.get(row["origin"] + "/v1/models", headers={"Authorization": f"Bearer {row['secret']}"})
                    response.raise_for_status()
                    data = response.json().get("data", [])
                    models = [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
                    relay_state.update_models(row["account_id"], models); relay_model_backoff.pop(int(row["account_id"]), None); refreshed += 1
                except (httpx.HTTPError, ValueError, TypeError):
                    relay_model_backoff[int(row["account_id"])] = time.time() + 30; failed += 1
            await asyncio.gather(*(refresh(row) for row in relay_state.runtime_rows(_relay_assets())))
            return {"refreshed": refreshed, "failed": failed, "skipped": skipped}

    @app.get("/v1/models")
    async def relay_models(request: Request) -> Dict[str, Any]:
        if not bool(_relay_settings().get("relay_enabled", False)): raise HTTPException(status_code=404, detail="relay disabled")
        if (auth_error := _relay_machine_auth(request)) is not None: return auth_error
        await _refresh_relay_models(stale_only=True)
        models: Dict[str, Dict[str, Any]] = {}
        for row in relay_state.runtime_rows(_relay_assets()):
            for model in row.get("models", []): models.setdefault(model, {"id": model, "object": "model", "owned_by": "sub2api-native"})
        return {"object": "list", "data": [models[key] for key in sorted(models)]}

    @app.post("/v1/responses")
    async def relay_responses(request: Request) -> JSONResponse:
        settings = _relay_settings()
        if not bool(settings.get("relay_enabled", False)): raise HTTPException(status_code=404, detail="relay disabled")
        if (auth_error := _relay_machine_auth(request)) is not None: return auth_error
        payload = await request.body()
        try: body = json.loads(payload)
        except (ValueError, UnicodeDecodeError): raise HTTPException(status_code=400, detail="invalid JSON body")
        model = str(body.get("model") or "").strip()
        if not model: raise HTTPException(status_code=400, detail="model is required")
        affinity_source = next((request.headers.get(name) for name in ("x-session-id", "x-session-affinity", "x-conversation-id", "x-opencode-session") if request.headers.get(name)), str(body.get("prompt_cache_key") or body.get("conversation_id") or ""))
        session_key = hashlib.sha256(str(affinity_source).encode()).hexdigest() if affinity_source else ""
        affinity_ttl = float(settings.get("relay_session_affinity_ttl_seconds") or 3600)
        proxy = resolve_proxy_url(str(settings.get("relay_proxy") or settings.get("proxy") or "")) or None
        if bool(body.get("stream", False)):
            response, chunks, error = await stream_responses(relay_router, model, payload, str(settings.get("relay_strategy") or "fill_first"), proxy, float(settings.get("relay_first_byte_timeout_seconds") or 180), float(settings.get("relay_cooldown_seconds") or 120), float(settings.get("relay_rate_cooldown_seconds") or 30), int(settings.get("relay_max_attempts") or 2), dict(request.headers), session_key, affinity_ttl)
            if error: return JSONResponse(status_code=503, content=error)
            return StreamingResponse(chunks, status_code=response.status_code, media_type=response.headers.get("content-type", "text/event-stream").split(";", 1)[0], headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        response, error = await forward_responses(relay_router, model, payload, str(settings.get("relay_strategy") or "fill_first"), proxy, float(settings.get("relay_request_timeout_seconds") or 600), float(settings.get("relay_cooldown_seconds") or 120), float(settings.get("relay_rate_cooldown_seconds") or 30), int(settings.get("relay_max_attempts") or 2), dict(request.headers), session_key, affinity_ttl)
        if error: return JSONResponse(status_code=503, content=error)
        headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
        content_type = response.headers.get("content-type", "application/json")
        return Response(content=response.content, status_code=response.status_code, media_type=content_type.split(";", 1)[0], headers=headers)

    @app.api_route("/v1/{unsupported:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def relay_unsupported(unsupported: str) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"message": "this relay serves /v1/models and /v1/responses only", "type": "not_found"}})

    @app.get("/api/relay/overview")
    def relay_overview() -> Dict[str, Any]:
        settings = _relay_settings(); rows = relay_state.runtime_rows(_relay_assets())
        now = time.time()
        return {"ok": True, "enabled": bool(settings.get("relay_enabled", False)), "strategy": str(settings.get("relay_strategy") or "fill_first"), "pool_count": len(rows), "models": len({m for row in rows for m in row.get("models", [])}), "requests": len(relay_state.requests(500)), "in_flight": sum(int(row.get("in_flight") or 0) for row in rows), "cooling_down": sum(1 for row in rows if float(row.get("cooldown_until") or 0) > now)}

    @app.get("/api/relay/pool")
    def relay_pool() -> Dict[str, Any]:
        rows = relay_state.runtime_rows(_relay_assets())
        for row in rows: row["masked_key"] = "********"
        return {"ok": True, "items": rows}

    @app.get("/api/relay/requests")
    def relay_requests(limit: int = Query(100, ge=1, le=500)) -> Dict[str, Any]:
        return {"ok": True, "items": relay_state.requests(limit)}

    @app.post("/api/relay/refresh-models")
    async def relay_refresh_models() -> Dict[str, Any]:
        return {"ok": True, **(await _refresh_relay_models(stale_only=False))}

    @app.post("/api/relay/pool/{account_id}/probe")
    async def relay_probe(account_id: int) -> Dict[str, Any]:
        import httpx
        with _account_remote_slot():
            rows = [row for row in relay_state.runtime_rows(_relay_assets()) if int(row.get("account_id") or 0) == account_id]
            if not rows: raise HTTPException(status_code=404, detail="聚合池成员不存在")
            row = rows[0]; settings = _relay_settings(); proxy = resolve_proxy_url(str(settings.get("relay_proxy") or settings.get("proxy") or "")) or None
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
                    response = await client.get(row["origin"] + "/v1/models", headers={"Authorization": f"Bearer {row['secret']}"})
                if not response.is_success: raise RuntimeError(f"上游返回 HTTP {response.status_code}")
                data = response.json().get("data", []); models = [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
                relay_state.update_models(account_id, models); relay_state.mark(account_id, "healthy", response.status_code)
                return {"ok": True, "models": len(models), "status": "healthy"}
            except Exception as exc:
                relay_state.mark(account_id, "probe_failed", 0, float(settings.get("relay_cooldown_seconds") or 120))
                raise HTTPException(status_code=502, detail="聚合池成员探测失败") from exc

    @app.post("/api/relay/keys/rotate")
    def relay_rotate() -> JSONResponse:
        return JSONResponse({"ok": True, "relay_api_key": relay_state.rotate_credential()}, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/me")
    def api_auth_me(request: Request) -> Dict[str, Any]:
        record = _auth_record() or {}
        username = str(record.get("username") or "")
        enabled = _web_auth_enabled()
        authenticated = bool(enabled and _valid_session(request.cookies.get(WEB_SESSION_COOKIE, "")))
        return {
            "ok": True,
            "enabled": enabled,
            "setup_required": not enabled,
            "authenticated": authenticated,
            "username": username if authenticated and enabled else "",
        }

    @app.post("/api/auth/setup")
    def api_auth_setup(body: LoginBody) -> JSONResponse:
        if _auth_record() is not None:
            raise HTTPException(status_code=409, detail="管理员账号已创建")
        username = str(body.username or "").strip()
        password = str(body.password or "")
        confirm = str(body.confirm_password or "")
        if len(username) < 3:
            raise HTTPException(status_code=400, detail="账号至少需要 3 个字符")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="密码至少需要 8 个字符")
        if password != confirm:
            raise HTTPException(status_code=400, detail="两次输入的密码不一致")
        record = _create_auth_record(username, password)
        try:
            _save_auth_record(record)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"保存管理员账号失败: {exc}") from exc
        response = JSONResponse(
            {"ok": True, "enabled": True, "authenticated": True, "username": username}
        )
        expires_at = int(time.time()) + WEB_SESSION_TTL
        response.set_cookie(
            WEB_SESSION_COOKIE,
            _sign_session(username, expires_at, record["session_secret"]),
            max_age=WEB_SESSION_TTL,
            expires=WEB_SESSION_TTL,
            httponly=True,
            secure=str(os.environ.get("SUB2API_WEB_COOKIE_SECURE", "1")).strip().lower()
            not in {"0", "false", "no", "off"},
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/auth/login")
    def api_auth_login(body: LoginBody) -> JSONResponse:
        record = _auth_record()
        if record is None:
            raise HTTPException(status_code=409, detail="请先创建管理员账号")
        username = record["username"]
        supplied_password = str(body.password or "")
        supplied_user = str(body.username or "")
        try:
            salt = base64.urlsafe_b64decode(record["password_salt"])
        except (ValueError, base64.binascii.Error) as exc:
            raise HTTPException(status_code=500, detail="管理员账号数据损坏") from exc
        valid_password = hmac.compare_digest(
            _hash_password(supplied_password, salt), record["password_hash"]
        )
        if not (hmac.compare_digest(supplied_user, username) and valid_password):
            raise HTTPException(status_code=401, detail="账号或密码错误")
        expires_at = int(time.time()) + WEB_SESSION_TTL
        response = JSONResponse(
            {"ok": True, "enabled": True, "authenticated": True, "username": username}
        )
        response.set_cookie(
            WEB_SESSION_COOKIE,
            _sign_session(username, expires_at, record["session_secret"]),
            max_age=WEB_SESSION_TTL,
            expires=WEB_SESSION_TTL,
            httponly=True,
            secure=str(os.environ.get("SUB2API_WEB_COOKIE_SECURE", "1")).strip().lower()
            not in {"0", "false", "no", "off"},
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def api_auth_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(WEB_SESSION_COOKIE, path="/")
        return response

    @app.get("/api/stats")
    def api_stats() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        store = gr.get_registration_repository()
        return {"ok": True, "stats": store.stats(), "job": job_coordinator.status()}

    @app.get("/api/accounts")
    def api_accounts(
        status: str = Query(""),
        email_disable_status: str = Query(""),
        q: str = Query(""),
        keyword: str = Query(""),
        batch_id: str = Query(""),
        profile_id: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=10000),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        status_norm = str(status or "").strip().lower()
        keyword_norm = str(q or keyword or "").strip()
        batch_norm = str(batch_id or "").strip()
        # Profile 过滤：0 = 全部
        profile_filter = profile_id if profile_id > 0 else ""
        rows = store.list_results(
            status=status_norm,
            mail_status=str(email_disable_status or "").strip().lower(),
            keyword=keyword_norm,
            batch_id=batch_norm,
            profile_id=profile_filter,
            limit=limit,
            offset=offset,
        )
        total = store.count_results(
            status=status_norm,
            mail_status=str(email_disable_status or "").strip().lower(),
            keyword=keyword_norm,
            batch_id=batch_norm,
            profile_id=profile_filter,
        )
        return {
            "ok": True,
            "total": total,
            "count": len(rows),
            "has_more": offset + len(rows) < total,
            "offset": offset,
            "limit": limit,
            "items": [_serialize_record(row) for row in rows],
        }

    @app.get("/api/accounts/actionable-ids")
    @app.post("/api/accounts/credentials-txt/download")
    def api_accounts_credentials_txt_download(body: AccountIdsBody) -> StreamingResponse:
        """批量导出凭据 TXT：每行一条 email----password（仅 success 记录）。"""
        ids = _batch_account_ids(body.ids)
        records = _gr().get_registration_repository().get_results_by_ids(ids)
        if not records:
            raise HTTPException(status_code=404, detail="没有匹配的记录")
        payload, exported = build_credentials_text(records)
        skipped = 0
        if not exported:
            raise HTTPException(status_code=404, detail="所选账号均没有可导出的凭据")
        filename = f"credentials-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        return StreamingResponse(
            iter([payload]),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Exported-Count": str(exported),
                "X-Skipped-Count": str(skipped),
            },
        )

    @app.get("/api/accounts/{account_id}")
    def api_account_detail(account_id: int) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        rows = store.get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"ok": True, "item": _serialize_record(rows[0])}

    @app.post("/api/accounts/{account_id}/checkin")
    def api_account_checkin(account_id: int) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        rows = store.get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        record = rows[0]
        if not bool(record.get("success")) or str(record.get("registration_status")) != "success":
            raise HTTPException(status_code=409, detail="只有注册成功的账号可以签到")
        email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "")
        if not email or not password:
            raise HTTPException(status_code=409, detail="账号记录缺少登录凭据")
        profile = store.get_profile(record.get("profile_id"))
        from backend.registration.verified_sites import get_verified_site
        site = get_verified_site(str((profile or {}).get("site_key") or ""))
        if site is None or not site.checkin_supported:
            raise HTTPException(status_code=409, detail="该站点尚未验证签到功能")
        origin = str((profile or {}).get("register_origin") or "").strip()
        if not origin:
            raise HTTPException(status_code=409, detail="账号对应的 Profile origin 无效")
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有账号远程操作正在执行，请等待完成")

        from backend.integrations.sub2api_checkin import (
            CamoufoxCaptchaSolver,
            Sub2ApiCheckinService,
            Sub2ApiClient,
        )

        solver = None
        client = None
        try:
            with job_coordinator.idle_guard():
                gr.load_config()
                gr._wire_runtime_modules()
                solver = CamoufoxCaptchaSolver(log_callback=job_coordinator._append_log)
                client = Sub2ApiClient(origin, timeout=30, proxies=gr.get_proxies())
                result = Sub2ApiCheckinService(client, solver).run(email, password)
            return {"ok": True, "result": result.as_dict()}
        except RuntimeError as exc:
            if job_coordinator.status().get("running"):
                raise HTTPException(status_code=409, detail="注册任务运行中，暂不能执行签到") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            try:
                if solver is not None:
                    solver.close()
            except BaseException:
                pass
            try:
                if client is not None:
                    client.close()
            except BaseException:
                pass
            _account_remote_guard.release()

    @app.post("/api/accounts/checkin")
    def api_accounts_checkin(body: AccountIdsBody) -> Dict[str, Any]:
        ids = _batch_account_ids(body.ids)
        items: List[Dict[str, Any]] = []
        for account_id in ids:
            try:
                response = api_account_checkin(account_id)
                result = dict(response.get("result") or {})
                items.append({"account_id": account_id, "ok": True, "result": result})
            except HTTPException as exc:
                items.append({"account_id": account_id, "ok": False, "error": str(exc.detail)})
        return {
            "ok": True,
            "items": items,
            "success": sum(1 for item in items if item["ok"]),
            "failure": sum(1 for item in items if not item["ok"]),
        }

    @app.post("/api/accounts/{account_id}/verify-credentials")
    def api_verify_account_credentials(
        account_id: int, body: CredentialVerificationBody
    ) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        rows = store.get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        record = rows[0]
        if str(record.get("failure_type") or "") != "already_registered":
            raise HTTPException(status_code=409, detail="只有已注册记录可以验证新密码")
        email = str(record.get("email") or "").strip()
        profile = store.get_profile(record.get("profile_id"))
        origin = str((profile or {}).get("register_origin") or "").strip()
        if not email or not origin:
            raise HTTPException(status_code=409, detail="账号记录缺少有效邮箱或站点 Profile")
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有账号远程操作正在执行，请等待完成")

        from backend.integrations.sub2api_checkin import (
            CamoufoxCaptchaSolver,
            Sub2ApiCheckinService,
            Sub2ApiClient,
        )

        solver = None
        client = None
        try:
            with job_coordinator.idle_guard():
                gr.load_config()
                gr._wire_runtime_modules()
                solver = CamoufoxCaptchaSolver(log_callback=job_coordinator._append_log)
                client = Sub2ApiClient(origin, timeout=30, proxies=gr.get_proxies())
                verification = Sub2ApiCheckinService(client, solver).verify_credentials(
                    email, body.password
                )
            if verification.status != "success":
                raise HTTPException(status_code=409, detail=verification.message)
            updated = store.promote_verified_credentials(
                account_id,
                body.password,
                verified_at=store.now_text(),
            )
            return {
                "ok": True,
                "result": verification.as_dict(),
                "item": _serialize_record(updated),
            }
        except HTTPException:
            raise
        except RuntimeError as exc:
            if job_coordinator.status().get("running"):
                raise HTTPException(status_code=409, detail="注册任务运行中，暂不能验证凭据") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            try:
                if solver is not None:
                    solver.close()
            except BaseException:
                pass
            try:
                if client is not None:
                    client.close()
            except BaseException:
                pass
            _account_remote_guard.release()

    @app.get("/api/accounts/{account_id}/api-key-context")
    def api_account_api_key_context(account_id: int) -> Dict[str, Any]:
        gr, record, profile, site, origin, email, password = _sub2api_account_context(account_id)
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="已有账号远程操作正在执行，请等待完成")

        from backend.integrations.sub2api_auth import Sub2ApiAuthService
        from backend.integrations.sub2api_captcha import CamoufoxCaptchaSolver, CaptchaError
        from backend.integrations.sub2api_keys import Sub2ApiKeyService, site_capabilities
        from backend.integrations.sub2api_transport import (
            Sub2ApiApiError,
            Sub2ApiClient,
            Sub2ApiNetworkError,
        )

        solver = None
        client = None
        try:
            with job_coordinator.idle_guard():
                gr.load_config()
                gr._wire_runtime_modules()
                solver = CamoufoxCaptchaSolver(log_callback=job_coordinator._append_log)
                client = Sub2ApiClient(origin, timeout=30, proxies=gr.get_proxies())
                auth = Sub2ApiAuthService(client, solver)
                token = auth.login(email, password, auth.public_settings())
                keys = Sub2ApiKeyService(client)
                groups = keys.list_groups(token)
                existing = keys.list_keys(token)
            return {
                "ok": True,
                "account": {
                    "id": int(record["id"]),
                    "profile_id": int(profile["id"]),
                    "site_key": site.key,
                },
                "groups": groups,
                "existing_key_count": len(existing),
                "existing_keys": existing,
                "capabilities": site_capabilities(site.key),
            }
        except (Sub2ApiApiError, Sub2ApiNetworkError, CaptchaError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except RuntimeError as exc:
            if job_coordinator.status().get("running"):
                raise HTTPException(status_code=409, detail="注册任务运行中，暂不能读取 API Key") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            try:
                if solver is not None:
                    solver.close()
            except BaseException:
                pass
            try:
                if client is not None:
                    client.close()
            except BaseException:
                pass
            _account_remote_guard.release()

    @app.post("/api/accounts/{account_id}/api-keys")
    def api_account_api_key_create(account_id: int, body: ApiKeyCreateBody) -> JSONResponse:
        gr, record, profile, site, origin, email, password = _sub2api_account_context(account_id)
        no_store = {"Cache-Control": "no-store"}
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="已有账号远程操作正在执行，请等待完成",
                headers=no_store,
            )

        from backend.integrations.sub2api_auth import Sub2ApiAuthService
        from backend.integrations.sub2api_captcha import CamoufoxCaptchaSolver, CaptchaError
        from backend.integrations.sub2api_keys import (
            ApiKeyCreateUncertainError,
            ApiKeyValidationError,
            Sub2ApiKeyService,
        )
        from backend.integrations.sub2api_transport import (
            Sub2ApiApiError,
            Sub2ApiClient,
            Sub2ApiNetworkError,
        )

        solver = None
        client = None
        try:
            with job_coordinator.idle_guard():
                gr.load_config()
                gr._wire_runtime_modules()
                solver = CamoufoxCaptchaSolver(log_callback=job_coordinator._append_log)
                client = Sub2ApiClient(origin, timeout=30, proxies=gr.get_proxies())
                auth = Sub2ApiAuthService(client, solver)
                token = auth.login(email, password, auth.public_settings())
                result = Sub2ApiKeyService(client).create_key(token, body.name, body.group_id)
                account = gr.get_registration_repository().account_for_result(int(record["id"]))
                if isinstance(account, dict) and account.get("id"):
                    key_data = result.as_dict(); key_row_id = gr.get_registration_repository().upsert_account_key(int(account["id"]), int(key_data["id"]), str(key_data.get("name") or body.name), account_key_crypto.encrypt(str(key_data["secret"])), int(key_data.get("group_id") or body.group_id), str(key_data.get("status") or "active"))
                    if not account.get("relay_key_id"):
                        gr.get_registration_repository().set_relay_key(int(account["id"]), key_row_id)
            return JSONResponse(
                status_code=201,
                content={"ok": True, "key": result.as_dict()},
                headers=no_store,
            )
        except ApiKeyValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc), headers=no_store) from exc
        except ApiKeyCreateUncertainError as exc:
            raise HTTPException(status_code=504, detail=str(exc), headers=no_store) from exc
        except (Sub2ApiApiError, Sub2ApiNetworkError, CaptchaError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc), headers=no_store) from exc
        except RuntimeError as exc:
            if job_coordinator.status().get("running"):
                raise HTTPException(
                    status_code=409,
                    detail="注册任务运行中，暂不能创建 API Key",
                    headers=no_store,
                ) from exc
            raise HTTPException(status_code=409, detail=str(exc), headers=no_store) from exc
        finally:
            try:
                if solver is not None:
                    solver.close()
            except BaseException:
                pass
            try:
                if client is not None:
                    client.close()
            except BaseException:
                pass
            _account_remote_guard.release()

    @app.get("/api/accounts/{account_id}/api-keys/{key_id}/reveal")
    def api_account_api_key_reveal(account_id: int, key_id: int) -> JSONResponse:
        gr, record, profile, site, origin, email, password = _sub2api_account_context(account_id)
        no_store = {"Cache-Control": "no-store"}
        if not _account_remote_guard.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="已有账号远程操作正在执行，请等待完成",
                headers=no_store,
            )

        from backend.integrations.sub2api_auth import Sub2ApiAuthService
        from backend.integrations.sub2api_captcha import CamoufoxCaptchaSolver, CaptchaError
        from backend.integrations.sub2api_keys import (
            ApiKeyProtocolError,
            ApiKeyValidationError,
            Sub2ApiKeyService,
        )
        from backend.integrations.sub2api_transport import (
            Sub2ApiApiError,
            Sub2ApiClient,
            Sub2ApiNetworkError,
        )

        solver = None
        client = None
        try:
            with job_coordinator.idle_guard():
                gr.load_config()
                gr._wire_runtime_modules()
                solver = CamoufoxCaptchaSolver(log_callback=job_coordinator._append_log)
                client = Sub2ApiClient(origin, timeout=30, proxies=gr.get_proxies())
                auth = Sub2ApiAuthService(client, solver)
                token = auth.login(email, password, auth.public_settings())
                result = Sub2ApiKeyService(client).reveal_key(token, key_id)
                account = gr.get_registration_repository().account_for_result(int(record["id"]))
                if isinstance(account, dict) and account.get("id"):
                    key_data = result.as_dict(); key_row_id = gr.get_registration_repository().upsert_account_key(int(account["id"]), int(key_data["id"]), str(key_data.get("name") or ""), account_key_crypto.encrypt(str(key_data["secret"])), int(key_data.get("group_id") or 0), str(key_data.get("status") or "active"))
                    if not account.get("relay_key_id"):
                        gr.get_registration_repository().set_relay_key(int(account["id"]), key_row_id)
            return JSONResponse(status_code=200, content={"ok": True, "key": result.as_dict()}, headers=no_store)
        except ApiKeyValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc), headers=no_store) from exc
        except ApiKeyProtocolError as exc:
            raise HTTPException(status_code=502, detail=str(exc), headers=no_store) from exc
        except (Sub2ApiApiError, Sub2ApiNetworkError, CaptchaError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc), headers=no_store) from exc
        except RuntimeError as exc:
            if job_coordinator.status().get("running"):
                raise HTTPException(status_code=409, detail="注册任务运行中，暂不能读取 API Key", headers=no_store) from exc
            raise HTTPException(status_code=409, detail=str(exc), headers=no_store) from exc
        finally:
            try:
                if solver is not None:
                    solver.close()
            except BaseException:
                pass
            try:
                if client is not None:
                    client.close()
            except BaseException:
                pass
            _account_remote_guard.release()

    @app.get("/api/accounts/{account_id}/failure-screenshot")
    def api_account_failure_screenshot(account_id: int) -> FileResponse:
        gr = _gr()
        rows = gr.get_registration_repository().get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            path, media_type = _failure_screenshot_file(rows[0])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")

    @app.post("/api/accounts/delete")
    def api_accounts_delete(body: DeleteAccountsBody) -> Dict[str, Any]:
        gr = _gr()
        ids = _batch_account_ids(body.ids)

        from backend.registration.artifacts import (
            collect_related_file_paths,
            delete_related_files,
        )

        store = gr.get_registration_repository()
        records = store.get_results_by_ids(ids)
        if not records:
            raise HTTPException(status_code=404, detail="没有匹配的记录")

        released_emails: List[str] = []
        # 释放按 Profile 作用域分组（跨 Profile 互不阻塞）。
        release_groups: Dict[int, List[str]] = {}
        # 并发门禁（TOCTOU 闭合）：release_email 的整个临界区
        # （check → can_release → delete → release）持有与 job start 相同的
        # idle guard，临界区内不可能有新任务开始；运行中任务 → 409。
        from contextlib import nullcontext

        release_guard = (
            job_coordinator.idle_guard() if body.release_email else nullcontext()
        )
        try:
            with release_guard:
                if body.release_email:
                    if job_coordinator.status().get("running"):
                        raise HTTPException(
                            status_code=409,
                            detail="注册任务运行中，禁止释放消费标记；请等任务结束后再删除并释放",
                        )
                    for record in records:
                        profile_id = int(record.get("profile_id") or 0)
                        email = str(record.get("email") or "").strip()
                        if profile_id > 0 and email:
                            release_groups.setdefault(profile_id, []).append(email)
                    # fail-closed：每个 Profile 作用域内检查该邮箱的全部历史记录（不只选中行）
                    blocked_details: List[str] = []
                    for profile_id, emails in release_groups.items():
                        blocked = store.can_release_consumption(emails, profile_id=profile_id)
                        for email, reason in list(blocked.items())[:5]:
                            blocked_details.append(f"[Profile {profile_id}] {email}（{reason}）")
                    if blocked_details:
                        raise HTTPException(
                            status_code=409,
                            detail="以下邮箱存在成功记录，不能释放：" + "; ".join(blocked_details[:10]),
                        )

                file_paths: List[str] = []
                seen = set()
                if body.delete_files:
                    for record in records:
                        for path in collect_related_file_paths(
                            record,
                            accounts_dir=gr.ACCOUNTS_DIR,
                            app_dir=gr.DATA_DIR,
                        ):
                            if path in seen:
                                continue
                            seen.add(path)
                            file_paths.append(path)

                deleted_records = store.delete_results([row.get("id") for row in records])
                if body.release_email:
                    # 只释放各 Profile 作用域自己的账本行（仍在 guard 临界区内）
                    for profile_id, emails in release_groups.items():
                        released_emails.extend(
                            store.release_consumptions(emails, profile_id=profile_id)
                        )

                deleted_files: List[str] = []
                file_errors: List[str] = []
                if body.delete_files:
                    deleted_files, file_errors = delete_related_files(file_paths)

                return {
                    "ok": True,
                    "deleted": len(deleted_records),
                    "deleted_files": len(deleted_files),
                    "file_errors": file_errors[:20],
                    "released_emails": released_emails,
                }
        except HTTPException:
            raise
        except RuntimeError as exc:
            # idle_guard 在 TOCTOU 窗口拒绝（任务恰好在检查后启动）→ 409 非 500
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Sub2API Profile 管理（identity = id；name 仅展示；origin 用后冻结）
    # ------------------------------------------------------------------
    def _profile_api_view(profile: Dict[str, Any], store) -> Dict[str, Any]:
        from backend.registration.verified_sites import get_verified_site

        item = dict(profile)
        item.pop("purpose", None)
        item["in_use"] = store.profile_has_usage(item["id"])
        site = get_verified_site(str(item.get("site_key") or ""))
        item["checkin_supported"] = bool(site and site.checkin_supported)
        item.update(store.profile_asset_counts(item["id"]))
        return item

    @app.get("/api/sub2api/profiles")
    def api_sub2api_profiles() -> Dict[str, Any]:
        store = _gr().get_registration_repository()
        profiles = [_profile_api_view(profile, store) for profile in store.list_profiles()]
        return {"ok": True, "profiles": profiles}

    @app.get("/api/account-pool")
    def api_account_pool(profile_id: Optional[int] = None, status: str = "") -> Dict[str, Any]:
        store = _gr().get_registration_repository()
        return {"ok": True, "accounts": store.list_accounts(profile_id=profile_id or "", status=status)}

    @app.post("/api/account-pool")
    def api_account_pool_create(body: AccountCreateBody) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                account, summary = service.add_account(
                    body.profile_id, body.email, body.password
                )
            return {"ok": True, "account": account, **summary.as_dict()}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.post("/api/account-pool/credentials-txt/download")
    def api_account_pool_credentials_txt_download(body: AccountIdsBody) -> StreamingResponse:
        ids = _batch_account_ids(body.ids)
        accounts = _gr().get_registration_repository().get_accounts_by_ids(ids)
        if not accounts:
            raise HTTPException(status_code=404, detail="没有匹配的 Account")
        payload, exported = build_credentials_text(accounts)
        if not exported:
            raise HTTPException(status_code=404, detail="所选 Account 均没有可导出的凭据")
        filename = f"account-credentials-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        return StreamingResponse(
            iter([payload]),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Exported-Count": str(exported),
                "X-Skipped-Count": str(len(accounts) - exported),
            },
        )

    @app.post("/api/account-pool/checkin")
    def api_account_pool_batch_checkin(body: AccountIdsBody) -> Dict[str, Any]:
        ids = _batch_account_ids(body.ids)
        items: List[Dict[str, Any]] = []
        try:
            with _account_operations() as service:
                for account_id in ids:
                    try:
                        result = service.checkin(account_id)
                        items.append(
                            {"account_id": account_id, "ok": True, "result": result}
                        )
                    except Exception as exc:
                        items.append(
                            {
                                "account_id": account_id,
                                "ok": False,
                                "error": str(exc)[:300],
                            }
                        )
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=_account_operation_status(exc), detail=str(exc)[:300]
            ) from exc
        return {
            "ok": True,
            "items": items,
            "success": sum(1 for item in items if item["ok"]),
            "failure": sum(1 for item in items if not item["ok"]),
        }

    @app.get("/api/account-pool/{account_id}")
    def api_account_pool_detail(account_id: int) -> Dict[str, Any]:
        store = _gr().get_registration_repository()
        account = store.get_account_context(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account 不存在")
        account.pop("password", None)
        return {
            "ok": True,
            "account": account,
            "keys": store.list_account_keys(account_id),
        }

    @app.get("/api/account-pool/{account_id}/credentials")
    def api_account_pool_credentials(account_id: int) -> JSONResponse:
        account = _gr().get_registration_repository().get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account 不存在")
        return JSONResponse(
            {
                "ok": True,
                "email": str(account.get("email") or ""),
                "password": str(account.get("password") or ""),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/account-pool/{account_id}/verify")
    def api_account_pool_verify(account_id: int) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                account = service.verify(account_id)
            return {"ok": True, "account": account}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.post("/api/account-pool/{account_id}/checkin")
    def api_account_pool_checkin(account_id: int) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                result = service.checkin(account_id)
            return {"ok": True, "result": result}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.post("/api/account-pool/{account_id}/sync-keys")
    def api_account_sync_keys(account_id: int) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                summary = service.sync_keys(account_id)
            return {"ok": True, **summary.as_dict()}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.get("/api/account-pool/{account_id}/groups")
    def api_account_groups(account_id: int) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                groups = service.groups(account_id)
            return {"ok": True, "groups": groups}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.post("/api/account-pool/{account_id}/api-keys")
    def api_account_pool_key_create(
        account_id: int, body: ApiKeyCreateBody
    ) -> JSONResponse:
        try:
            with _account_operations() as service:
                row_id, key = service.create_key(account_id, body.name, body.group_id)
            return JSONResponse(
                status_code=201,
                content={"ok": True, "key_row_id": row_id, "key": key},
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.put("/api/account-pool/{account_id}/api-keys/{key_row_id}")
    def api_account_pool_key_group(
        account_id: int, key_row_id: int, body: ApiKeyGroupBody
    ) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                key = service.update_key_group(account_id, key_row_id, body.group_id)
            return {"ok": True, "key": key}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.delete("/api/account-pool/{account_id}/api-keys/{key_row_id}")
    def api_account_pool_key_delete(account_id: int, key_row_id: int) -> Dict[str, Any]:
        try:
            with _account_operations() as service:
                service.delete_key(account_id, key_row_id)
            return {"ok": True, "deleted": key_row_id}
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=_account_operation_status(exc), detail=str(exc)[:300]) from exc

    @app.get("/api/api-keys")
    def api_global_keys(account_id: int = 0) -> Dict[str, Any]:
        return {"ok": True, "keys": _gr().get_registration_repository().list_account_keys(account_id)}

    @app.get("/api/api-keys/{key_row_id}/reveal")
    def api_global_key_reveal(key_row_id: int) -> JSONResponse:
        key = _gr().get_registration_repository().get_account_key(key_row_id)
        if not key or not str(key.get("key_ciphertext") or ""):
            raise HTTPException(status_code=404, detail="API Key 不存在或本地未保存完整值")
        try:
            secret = account_key_crypto.decrypt(str(key["key_ciphertext"]))
        except Exception as exc:
            raise HTTPException(status_code=500, detail="API Key 解密失败") from exc
        return JSONResponse(
            {"ok": True, "secret": secret}, headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/account-pool/{account_id}/relay-key")
    def api_account_relay_key(account_id: int, body: RelayKeyBody) -> Dict[str, Any]:
        try: _gr().get_registration_repository().set_relay_key(account_id, body.key_id)
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    @app.put("/api/account-pool/{account_id}/relay")
    def api_account_relay(account_id: int, body: AccountRelayBody) -> Dict[str, Any]:
        try:
            _gr().get_registration_repository().set_account_relay_enabled(
                account_id, body.enabled
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/sub2api/sites")
    def api_sub2api_sites() -> Dict[str, Any]:
        from backend.registration.verified_sites import list_verified_sites

        return {"ok": True, "sites": list_verified_sites()}

    @app.post("/api/sub2api/profiles")
    def api_sub2api_profile_create(body: ProfileBody) -> Dict[str, Any]:
        store = _gr().get_registration_repository()
        try:
            # exclude_unset：未提交的字段不参与创建（不传 enabled → 默认启用）。
            profile = store.create_profile(body.model_dump(exclude_unset=True))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "profile": _profile_api_view(profile, store)}

    @app.put("/api/sub2api/profiles/{profile_id}")
    def api_sub2api_profile_update(profile_id: int, body: ProfileBody) -> Dict[str, Any]:
        from backend.registration.store import ProfileError

        store = _gr().get_registration_repository()
        # exclude_unset：partial update 只覆盖提交了的字段，未提交字段保持原值
        # （否则 None 会把 promo/邀请码清空、把 Profile 误禁用）。
        payload = body.model_dump(exclude_unset=True)
        try:
            profile = store.update_profile(profile_id, payload)
        except ProfileError as exc:
            # 名称冲突/origin 冻结是业务规则冲突 → 409；其余为 400/404。
            from backend.registration.store import (
                ProfileNameConflictError,
                ProfileNotFoundError,
                ProfileOriginLockedError,
            )

            if isinstance(exc, ProfileNotFoundError):
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if isinstance(exc, (ProfileNameConflictError, ProfileOriginLockedError)):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "profile": _profile_api_view(profile, store)}

    @app.delete("/api/sub2api/profiles/{profile_id}")
    def api_sub2api_profile_delete(profile_id: int) -> Dict[str, Any]:
        from backend.registration.store import ProfileInUseError

        store = _gr().get_registration_repository()
        try:
            deleted = store.delete_profile(profile_id)
        except ProfileInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Profile 不存在")
        return {"ok": True, "deleted": profile_id}

    @app.get("/api/config")
    def api_config_get() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        # gate_l_max_count：Gate L 单一真相源，前端据此动态限制 count 上限
        return {
            "ok": True,
            "config": _public_config(gr.config),
            "gate_l_max_count": gate_l_max_count(),
        }

    @app.get("/api/config/file")
    def api_config_file_get() -> Dict[str, Any]:
        return {"ok": True, "file": _config_file_snapshot()}

    @app.put("/api/config")
    @app.post("/api/config")
    async def api_config_put(request: Request) -> Dict[str, Any]:
        if job_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="注册任务运行中，暂不可修改配置")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="无效的配置 JSON")
        updates = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        result = _apply_config_updates(updates)
        return {"ok": True, **result}

    @app.get("/api/job")
    def api_job_status() -> Dict[str, Any]:
        return {"ok": True, "job": job_coordinator.status()}

    @app.get("/api/job/logs")
    def api_job_logs(
        after_id: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=2000),
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "logs": job_coordinator.get_logs(after_id=after_id, limit=limit),
            "job": job_coordinator.status(),
        }

    @app.post("/api/job/start")
    def api_job_start(body: StartJobBody) -> Dict[str, Any]:
        if body.profile_id in (None, 0):
            raise HTTPException(status_code=400, detail="任务必须指定 profile_id")
        gr = _gr()
        gr.load_config()
        if body.config:
            _apply_config_updates(body.config)
            gr.load_config()

        count = body.count if body.count is not None else gr.config.get("register_count", 1)
        try:
            count_i = int(count)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="count 必须是整数")

        # Gate L（代码硬门禁）：count>1 浏览器身份隔离尚未通过 Live 验收，
        # 未过门禁前批量注册保持 count=1，避免未验收功能被意外使用。
        limit = gate_l_max_count()
        if count_i > limit:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Gate L 未通过：批量 count>1 尚未完成 Live 验收，"
                    f"当前仅支持 count≤{limit}"
                ),
            )

        try:
            # 单 worker 固定：v1 不提供并发浏览器配置（runner 内部强制 1）。
            status = job_coordinator.start(
                count=count_i,
                profile_id=body.profile_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            # 任务参数错误（Profile 不存在/禁用/非法 ID 等）
            detail = str(exc)
            status_code = 404 if "不存在" in detail else 400
            raise HTTPException(status_code=status_code, detail=detail)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"启动失败: {exc}",
            ) from exc
        return {"ok": True, "job": status}

    @app.post("/api/job/stop")
    def api_job_stop() -> Dict[str, Any]:
        try:
            status = job_coordinator.stop()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"停止失败: {exc}") from exc
        return {"ok": True, "job": status}

    @app.post("/api/browser/kill-all")
    def api_browser_kill_all() -> Dict[str, Any]:
        gr = _gr()
        gr._bs.block_browser_launches()
        if job_coordinator.status().get("running"):
            try:
                job_coordinator.request_stop()
            except Exception:
                pass
        try:
            result = gr._bs.kill_all_browser_processes(log_callback=job_coordinator._append_log)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"终止浏览器失败: {exc}") from exc
        return {"ok": True, **result, "job": job_coordinator.status()}

    @app.api_route("/api/connectivity", methods=["GET", "POST"])
    def api_connectivity() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        gr._wire_runtime_modules()
        try:
            checks = gr._conn.run_connectivity_checks(gr.config, gr.http_get, gr.http_post)
            items = [
                {"name": name, "ok": bool(ok), "detail": str(detail)}
                for name, ok, detail in checks
            ]
            return {"ok": True, "items": items, "blocked": False}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"连通性检查失败: {exc}") from exc

    # ---- static SPA ----
    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503,
                detail="Web UI 未构建。请在 front/ 执行 npm install && npm run build。",
            )
        return FileResponse(index)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=503, detail="Web UI 未构建")

    return app


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    print(f"[web] Sub2API Native Web UI -> http://{host}:{port}", flush=True)
    print("[web] Sub2API 注册与 OutlookEmail 执行逻辑；交付 email----password", flush=True)
    print(f"[web] API docs -> http://{host}:{port}/api/docs", flush=True)
    uvicorn.run(
        "backend.web.application:create_app",
        factory=True,
        host=host,
        port=int(port),
        log_level="warning",
        access_log=False,
        workers=1,
    )


def main(argv: Optional[List[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Sub2API Native Web Console (FastAPI)")
    parser.add_argument("--host", default=os.environ.get("SUB2API_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUB2API_WEB_PORT", "8787")))
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
