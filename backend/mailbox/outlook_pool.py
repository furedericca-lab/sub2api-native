"""Outlook 账号池与临时邮箱渠道适配器。

同时支持 API Key 查询（accounts）、Web Session 登录（temp）和验证码轮询。
"""

from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable, List, Optional

from backend.mailbox.utilities import extract_verification_code, strip_html
from backend.mailbox.service import (
    normalize_api_base,
    resolve_api_base,
    resolve_legacy_session_cookie,
    resolve_login_password,
    validate_launch_path,
)

HttpGet = Callable[..., Any]
SessionFactory = Callable[[], Any]
UnavailableCheck = Callable[[str], bool]
LogCallback = Optional[Callable[[str], None]]

_state_lock = threading.RLock()
_account_index = 0
_RECEIVED_AT_PRECISION_TOLERANCE = 1.0
_session_cookie = ""
_session_cookie_key: tuple[str, str] | None = None
_reserved_emails: set[str] = set()


def normalize_base(api_base: str) -> str:
    base = str(api_base or "").strip()
    return normalize_api_base(base) if base else resolve_api_base({})


def normalize_source(source: str) -> str:
    value = str(source or "accounts").strip().lower()
    return value if value in {"accounts", "temp"} else "accounts"


def api_headers(api_key: str) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise Exception("OutlookEmail accounts 来源需要配置 API Key")
    return {"X-API-Key": key}


def reset_runtime_state() -> None:
    global _account_index, _session_cookie, _session_cookie_key
    with _state_lock:
        _account_index = 0
        _session_cookie = ""
        _session_cookie_key = None
        _reserved_emails.clear()


def release_email(email: str) -> None:
    """邮箱未提交到目标站点，释放占用允许再次获取。"""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    with _state_lock:
        _reserved_emails.discard(normalized)


def cookie_from_response(resp: Any) -> str:
    try:
        raw_cookie = str(resp.headers.get("set-cookie", "") or "")
    except Exception:
        raw_cookie = ""
    if not raw_cookie:
        return ""
    try:
        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        return "; ".join(f"{key}={value.value}" for key, value in cookie.items())
    except Exception:
        return ""


def cookie_from_session(session: Any) -> str:
    try:
        items = list(session.cookies.items())
        if items:
            return "; ".join(f"{key}={value}" for key, value in items)
    except Exception:
        pass
    try:
        jar = getattr(session.cookies, "jar", None)
        if jar:
            items = [f"{item.name}={item.value}" for item in jar]
            if items:
                return "; ".join(items)
    except Exception:
        pass
    return ""


def merge_cookie_headers(*values: str) -> str:
    """合并多次响应里的 Cookie，同名项以后者为准。"""
    merged: dict[str, str] = {}
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            cookie = SimpleCookie()
            cookie.load(text)
            for key, value in cookie.items():
                merged[key] = value.value
        except Exception:
            for part in text.split(";"):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                key = key.strip()
                if key:
                    merged[key] = value.strip()
    return "; ".join(f"{key}={value}" for key, value in merged.items())


def login_cookie(
    session_factory: SessionFactory,
    api_base: str,
    web_password: str,
    *,
    proxies: Optional[dict] = None,
    force_refresh: bool = False,
) -> str:
    global _session_cookie, _session_cookie_key
    base = normalize_base(api_base)
    password = str(web_password or "")
    if not password:
        return ""
    cache_key = (base, password)
    with _state_lock:
        if not force_refresh and _session_cookie and _session_cookie_key == cache_key:
            return _session_cookie

    session = session_factory()
    if proxies:
        try:
            session.proxies = proxies
        except Exception:
            pass
    login_resp = session.post(
        f"{base}/api/extension/login",
        json={"password": password, "next": "/"},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    login_resp.raise_for_status()
    data = login_resp.json()
    if not isinstance(data, dict) or not data.get("success") or not data.get("launch_url"):
        # Upstream error payloads may contain account addresses or provider
        # details. Keep those values out of registration logs and UI errors.
        raise Exception("OutlookEmail 网页登录响应无效")
    launch_path = validate_launch_path(str(data.get("launch_url") or ""))
    url = f"{base}{launch_path}"
    session_resp = session.get(url, allow_redirects=True, timeout=15)
    session_resp.raise_for_status()
    cookie = merge_cookie_headers(
        cookie_from_response(login_resp),
        cookie_from_response(session_resp),
        cookie_from_session(session),
    )
    if not cookie:
        raise Exception("OutlookEmail 登录成功但未获取到 Session Cookie")
    with _state_lock:
        _session_cookie = cookie
        _session_cookie_key = cache_key
    return cookie


def session_headers(
    session_factory: SessionFactory,
    api_base: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> dict:
    """temp 来源专用：Web 登录换 Cookie，或使用兼容回退的手工 Cookie。"""
    if not str(web_password or ""):
        web_password = resolve_login_password()
    if not str(session_cookie or "").strip():
        session_cookie = resolve_legacy_session_cookie()
    cookie = login_cookie(
        session_factory,
        api_base,
        web_password,
        proxies=proxies,
    ) or str(session_cookie or "").strip()
    if not cookie:
        raise Exception("OutlookEmail temp 来源未取得运行时登录凭据")
    return {"Cookie": cookie}


def pick_list(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("emails", "items", "results", "accounts", "temp_emails", "tempEmails", "messages"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get("data")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return pick_list(nested)
    return []


def item_email(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("email", "address", "name"):
        value = str(item.get(key, "") or "").strip()
        if "@" in value:
            return value
    return ""


def item_is_active(item: Any) -> bool:
    """账号池中 status=inactive 表示已停用，不参与注册。"""
    if not isinstance(item, dict):
        return False
    return str(item.get("status", "") or "").strip().lower() != "inactive"


def parse_tag_ids(raw: str | Iterable[Any]) -> set[str]:
    if isinstance(raw, str):
        return {item.strip() for item in re.split(r"[,，\s]+", raw) if item.strip()}
    return {str(item).strip() for item in (raw or []) if str(item).strip()}


def temp_matches_tags(item: Any, tag_ids: set[str]) -> bool:
    if not tag_ids:
        return True
    tags = item.get("tags") if isinstance(item, dict) else None
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if isinstance(tag, dict) and str(tag.get("id", "")).strip() in tag_ids:
            return True
        if str(tag).strip() in tag_ids:
            return True
    return False


def get_accounts(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    *,
    group_id: str = "",
) -> List[dict]:
    params: dict[str, Any] = {
        "limit": 10000,
        "offset": 0,
        "sort_by": "created_at",
        "sort_order": "asc",
    }
    group = str(group_id or "").strip()
    if group:
        params["group_id"] = group
    resp = http_get(
        f"{normalize_base(api_base)}/api/external/accounts",
        headers=api_headers(api_key),
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise Exception("OutlookEmail 获取账号列表失败（响应无效）")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise Exception("OutlookEmail accounts 响应格式无效")
    return [item for item in accounts if isinstance(item, dict)]


def get_temp_emails(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    temp_tag_ids: str = "",
    proxies: Optional[dict] = None,
) -> List[dict]:
    resp = http_get(
        f"{normalize_base(api_base)}/api/temp-emails",
        headers=session_headers(
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            proxies=proxies,
        ),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise Exception("OutlookEmail 获取临时邮箱失败（响应无效）")
    tag_ids = parse_tag_ids(temp_tag_ids)
    return [
        item
        for item in pick_list(data)
        if item_email(item) and temp_matches_tags(item, tag_ids)
    ]


def email_domain_of(email: str) -> str:
    """提取邮箱的域名（小写）；无 @ 时返回空串。"""
    text = str(email or "").strip()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[1].lower()


def _normalize_allowed_domains(allowed_domains) -> set:
    """白名单规范化：小写、精确匹配集合；None/空 = 不限制。"""
    if not allowed_domains:
        return set()
    return {str(d).strip().lower() for d in allowed_domains if str(d).strip()}


def domain_matches_allowed(domain: str, allowed: set[str]) -> bool:
    """Match ``*``, an exact domain, or a verified ``*.suffix`` entry."""
    normalized = str(domain or "").strip().lower()
    if "*" in allowed:
        return True
    if normalized in allowed:
        return True
    return any(
        rule.startswith("*.")
        and normalized.endswith(rule[1:])
        and normalized != rule[2:]
        for rule in allowed
    )


def _reserve_candidate(
    candidates: List[dict],
    *,
    mode: str,
    rejected: set[str],
) -> Optional[dict]:
    """Atomically reserve one candidate without holding the lock during I/O."""
    global _account_index
    with _state_lock:
        if mode == "random":
            ordered = candidates[:]
            random.shuffle(ordered)
            for item in ordered:
                normalized = item_email(item).lower()
                if normalized in rejected or normalized in _reserved_emails:
                    continue
                _reserved_emails.add(normalized)
                return item
            return None

        for _ in range(len(candidates)):
            item = candidates[_account_index % len(candidates)]
            _account_index += 1
            normalized = item_email(item).lower()
            if normalized in rejected or normalized in _reserved_emails:
                continue
            _reserved_emails.add(normalized)
            return item
    return None


def acquire_email(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    *,
    api_key: str = "",
    source: str = "accounts",
    group_id: str = "",
    web_password: str = "",
    session_cookie: str = "",
    temp_tag_ids: str = "",
    pick_mode: str = "random",
    proxies: Optional[dict] = None,
    is_unavailable: Optional[UnavailableCheck] = None,
    allowed_domains=None,
    preflight_messages: bool = True,
    log_callback: LogCallback = None,
) -> tuple[str, str]:
    normalized_source = normalize_source(source)
    if normalized_source == "temp":
        accounts = get_temp_emails(
            http_get,
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            temp_tag_ids=temp_tag_ids,
            proxies=proxies,
        )
    else:
        accounts = get_accounts(http_get, api_base, api_key, group_id=group_id)

    allowed = _normalize_allowed_domains(allowed_domains)
    total = 0
    active = 0
    unavailable_count = 0
    domain_mismatch_count = 0
    candidates = []
    for item in accounts:
        email = item_email(item)
        if not email:
            continue
        total += 1
        if not item_is_active(item):
            continue
        active += 1
        # 白名单过滤在池内完成（active 之后、消费检查之前）：非匹配邮箱
        # 保持 active、绝不释放重试，也不会被烧掉。
        if allowed:
            if not domain_matches_allowed(email_domain_of(email), allowed):
                domain_mismatch_count += 1
                continue
        if is_unavailable:
            # fail-closed：无法确认消费状态（如 SQLite 异常）时，异常向上传播，
            # 本次 acquire 响亮失败；绝不能把 UNKNOWN 当作 AVAILABLE 放行。
            if is_unavailable(email):
                unavailable_count += 1
                continue
        candidates.append(item)
    if not candidates:
        if total <= 0:
            raise Exception("OutlookEmail 邮箱池为空，未返回任何账号")
        if active <= 0:
            raise Exception(
                f"OutlookEmail 邮箱池中没有 active 账号（共 {total} 个，均已停用或不可用）"
            )
        if allowed and domain_mismatch_count > 0:
            wanted = " / ".join(sorted(allowed))
            raise Exception(
                f"当前 Profile 要求 {wanted}，OutlookEmail 池中没有符合条件的可用邮箱"
                f"（active {active} 个，其中域名匹配 {domain_mismatch_count} 个不符、"
                f"已消耗 {unavailable_count} 个）。请补充匹配域名的邮箱或调整白名单。"
            )
        if unavailable_count > 0:
            raise Exception(
                "OutlookEmail 可取邮箱均已在本地标记为已注册/已消耗"
                f"（active {active} 个，其中已消耗 {unavailable_count} 个）。"
                "请补充新邮箱，或检查本地消费账本/历史注册记录是否符合预期。"
            )
        raise Exception("OutlookEmail 当前没有可分配的邮箱")

    mode = str(pick_mode or "random").strip().lower()
    rejected: set[str] = set()
    unreadable_count = 0
    last_read_error = ""
    account = None
    while len(rejected) < len(candidates):
        candidate = _reserve_candidate(candidates, mode=mode, rejected=rejected)
        if candidate is None:
            break
        email = item_email(candidate)
        if normalized_source == "accounts" and preflight_messages:
            try:
                # A registration mailbox is only usable if its authorization can
                # read mail before the irreversible remote submit boundary.
                get_messages(
                    http_get,
                    api_base,
                    api_key,
                    email,
                    folder="inbox",
                    top=1,
                )
            except Exception as exc:
                release_email(email)
                rejected.add(email.lower())
                unreadable_count += 1
                # Do not retain a provider response body in the eventual
                # aggregate error; it can contain mailbox metadata.
                last_read_error = type(exc).__name__
                if log_callback:
                    log_callback(
                        "[!] 跳过提交前无法读取邮件的 OutlookEmail 账号"
                        f"（域名={email_domain_of(email) or 'unknown'}）"
                    )
                continue
        account = candidate
        break
    if account is None:
        if unreadable_count:
            detail = f"；错误类型: {last_read_error}" if last_read_error else ""
            raise Exception(
                "OutlookEmail 符合当前 Profile 的未消费账号均无法读取邮件"
                f"（预检失败 {unreadable_count} 个，已在提交目标站点前停止）{detail}"
            )
        raise Exception("OutlookEmail 可取邮箱均已被当前任务占用（并发预留），不是邮箱池为空")
    email = item_email(account)
    return email, f"outlookemail:{normalized_source}:{email}"


def get_messages(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    email: str,
    *,
    folder: str = "all",
    top: int = 10,
) -> List[dict]:
    try:
        limit = max(1, min(50, int(top)))
    except Exception:
        limit = 10
    requested_folder = str(folder or "all").strip().lower()
    folders = ("inbox", "junkemail") if requested_folder == "all" else (requested_folder,)
    if any(value not in {"inbox", "junkemail"} for value in folders):
        raise ValueError("OutlookEmail 邮件文件夹仅支持 all/inbox/junkemail")

    merged: List[dict] = []
    seen: set[str] = set()
    for api_folder in folders:
        resp = http_get(
            f"{normalize_base(api_base)}/api/external/emails",
            headers=api_headers(api_key),
            params={
                "email": email,
                "folder": api_folder,
                "top": limit,
                "skip": 0,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise Exception(f"OutlookEmail 获取 {api_folder} 邮件失败（响应无效）")
        messages = data.get("emails")
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict):
                continue
            identity = str(
                item.get("id")
                or item.get("message_id")
                or item.get("internet_message_id")
                or ""
            )
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            merged.append(item)
    return merged


def get_temp_messages(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    *,
    web_password: str = "",
    session_cookie: str = "",
    proxies: Optional[dict] = None,
) -> List[dict]:
    resp = http_get(
        f"{normalize_base(api_base)}/api/temp-emails/{email}/messages",
        headers=session_headers(
            session_factory,
            api_base,
            web_password=web_password,
            session_cookie=session_cookie,
            proxies=proxies,
        ),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise Exception("OutlookEmail 获取临时邮箱邮件失败（响应无效）")
    return pick_list(data)


def message_received_at(message: Any) -> Optional[float]:
    """Return an email's received time as a Unix timestamp when available."""
    if not isinstance(message, dict):
        return None

    for key in (
        "timestamp",
        "received_at",
        "receivedAt",
        "receivedDateTime",
        "date",
        "created_at",
        "createdAt",
    ):
        raw_value = message.get(key)
        if raw_value is None or isinstance(raw_value, bool):
            continue

        if isinstance(raw_value, (int, float)):
            timestamp = float(raw_value)
        else:
            value = str(raw_value or "").strip()
            if not value:
                continue
            try:
                timestamp = float(value)
            except ValueError:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        parsed = parsedate_to_datetime(value)
                    except (TypeError, ValueError, OverflowError):
                        continue
                timestamp = parsed.timestamp()

        # Some temp-mail APIs expose Unix milliseconds instead of seconds.
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        if timestamp > 0:
            return timestamp
    return None


def mail_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = []
    for key in ("body_preview", "body", "text", "content", "snippet", "intro"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, dict):
            content = value.get("content") or value.get("text")
            if isinstance(content, str) and content.strip():
                parts.append(content)
    html_value = message.get("html")
    if isinstance(html_value, str):
        parts.append(strip_html(html_value))
    elif isinstance(html_value, list):
        for item in html_value:
            if isinstance(item, str):
                parts.append(strip_html(item))
    return "\n".join(parts)


def sender_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    sender = message.get("from") or message.get("sender") or ""
    if isinstance(sender, str):
        return sender
    if isinstance(sender, dict):
        email_address = sender.get("emailAddress")
        if isinstance(email_address, dict):
            return str(email_address.get("address") or email_address.get("name") or "")
        return str(sender.get("address") or sender.get("email") or sender.get("name") or "")
    return str(sender or "")


def message_recipient_addresses(message: Any) -> Optional[set[str]]:
    """Return exact recipient addresses, or None when metadata is absent."""
    if not isinstance(message, dict):
        return None

    recipient_keys = ("to", "toRecipients", "recipients", "recipient")
    values = [message[key] for key in recipient_keys if key in message]
    if not values:
        return None

    addresses: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str):
            for _, address in getaddresses([value]):
                normalized = address.strip().casefold()
                if "@" in normalized:
                    addresses.add(normalized)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return
        email_address = value.get("emailAddress")
        if email_address is not None:
            collect(email_address)
        for key in ("address", "email", "email_address"):
            if key in value:
                collect(value[key])

    for value in values:
        collect(value)
    return addresses


def message_matches_recipient(message: Any, email: str) -> bool:
    """Keep legacy messages without recipient metadata; isolate known aliases."""
    recipients = message_recipient_addresses(message)
    if recipients is None:
        return True
    return str(email or "").strip().casefold() in recipients


def wait_for_code(
    http_get: HttpGet,
    session_factory: SessionFactory,
    api_base: str,
    email: str,
    *,
    api_key: str = "",
    source: str = "accounts",
    web_password: str = "",
    session_cookie: str = "",
    folder: str = "all",
    top: int = 10,
    proxies: Optional[dict] = None,
    timeout: int = 180,
    poll_interval: int = 3,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    min_received_at: Optional[float] = None,
) -> str:
    deadline = time.time() + timeout
    seen_ids: set[str] = set()
    normalized_source = normalize_source(source)
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            if normalized_source == "temp":
                messages = get_temp_messages(
                    http_get,
                    session_factory,
                    api_base,
                    email,
                    web_password=web_password,
                    session_cookie=session_cookie,
                    proxies=proxies,
                )
            else:
                messages = get_messages(
                    http_get,
                    api_base,
                    api_key,
                    email,
                    folder=folder,
                    top=top,
                )
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] OutlookEmail 拉取邮件失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] OutlookEmail 本轮邮件数量: {len(messages)}")
        for message in messages:
            if not message_matches_recipient(message, email):
                if log_callback:
                    log_callback("[Debug] OutlookEmail 跳过收件人不匹配的共享收件箱邮件")
                continue
            subject = str(message.get("subject", "") or "")
            text = mail_text(message)
            message_id = str(
                message.get("id")
                or message.get("message_id")
                or message.get("internet_message_id")
                or f"{subject}|{text[:120]}"
            )
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            if min_received_at is not None:
                received_at = message_received_at(message)
                if received_at is None:
                    if log_callback:
                        log_callback("[Debug] OutlookEmail 跳过无可用收件时间的邮件")
                    continue
                if received_at <= min_received_at - _RECEIVED_AT_PRECISION_TOLERANCE:
                    if log_callback:
                        log_callback(
                            "[Debug] OutlookEmail 跳过提交邮箱前收到的邮件: "
                            f"received_at={received_at:.3f} <= submitted_at={min_received_at:.3f}"
                        )
                    continue
            if log_callback:
                log_callback(f"[Debug] OutlookEmail 收到邮件: {subject} ({sender_text(message)})")
            code = extract_verification_code(text, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] OutlookEmail 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"OutlookEmail 在 {timeout}s 内未收到验证码邮件")


def acquire(*args, **kwargs):
    """语义接口：从池中获取一个未使用邮箱（= acquire_email）。"""
    return acquire_email(*args, **kwargs)


def wait_code(*args, **kwargs):
    """语义接口：等待注册验证码（= wait_for_code）。"""
    return wait_for_code(*args, **kwargs)
