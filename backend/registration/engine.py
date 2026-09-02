#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sub2API 注册编排引擎。

组合 OutlookEmail 邮箱渠道、Profile 消费账本、Camoufox 浏览器流程与结果持久化，
提供 Web 后台调用的批量注册入口。Profile ID 是唯一注册作用域；
交付物：email----password（SQLite + Web UI + TXT 导出）。
"""

import threading
import datetime
import time
import os
import secrets
import re
import json
import traceback
from urllib.parse import urlsplit

from curl_cffi import requests

# 邮箱渠道适配器在 mailbox 包；编排层只负责调用。
from backend.mailbox import outlook_pool as outlookemail_provider
from backend.mailbox.service import resolve_api_base, resolve_legacy_session_cookie, resolve_login_password

from backend.automation import session as _bs
from backend.integrations import network_checks as _conn
from backend.registration.store import RegistrationRepository, normalize_profile_id
from backend.registration.runtime import (
    RegistrationCancelled,
    RegistrationStopController,
    raise_if_cancelled,
    sleep_with_cancel,
)
from backend.integrations.proxy import redact_proxy_text, redact_proxy_url, resolve_proxy_url
from backend.shared.paths import DATA_ROOT, PROJECT_ROOT
from backend.automation.session import (
    active_browser as _active_browser,
    active_page as _active_page,
    set_browser_session as _set_browser_session,
    start_browser,
    restart_browser,
    assert_fresh_browser_identity,
    stop_browser,
    cleanup_runtime_memory,
    refresh_active_page,
    create_browser_options,
    get_start_fail_streak,
    cleanup_stale_profiles as _cleanup_stale_profiles,
)


APP_DIR = str(PROJECT_ROOT)
DATA_DIR = str(DATA_ROOT)
CONFIG_FILE = os.path.abspath(
    os.path.expanduser(
        os.environ.get("SUB2API_CONFIG_FILE", os.path.join(APP_DIR, "config.json"))
    )
)
# 所有注册运行数据统一放入 data/，避免与前后端代码混放。
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
RESULTS_DB_FILE = os.path.join(ACCOUNTS_DIR, "registration_results.sqlite3")
MEMORY_CLEANUP_INTERVAL = 5
TRACEBACK_MAX_CHARS = 60_000
TRACEBACK_LOG_MAX_CHARS = 16_000

_repository = None
_repository_lock = threading.Lock()
_network_route_log_lock = threading.Lock()
_network_route_log_keys = set()


def current_exception_traceback(max_chars=TRACEBACK_MAX_CHARS):
    """返回当前异常的标准堆栈；没有活动异常时返回空字符串。"""
    text = traceback.format_exc().strip()
    if not text or text == "NoneType: None":
        return ""

    limit = max(1_000, int(max_chars or TRACEBACK_MAX_CHARS))
    if len(text) > limit:
        tail_size = min(4_000, limit // 4)
        text = (
            text[: limit - tail_size]
            + "\n... 异常堆栈过长，已截断 ...\n"
            + text[-tail_size:]
        )
    return text


def ensure_accounts_dir():
    """确保 data/accounts/ 存在，返回目录绝对路径。"""
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    return ACCOUNTS_DIR


def get_registration_repository():
    """懒加载 SQLite（单例）。"""
    global _repository
    if _repository is not None:
        return _repository
    with _repository_lock:
        if _repository is None:
            _repository = RegistrationRepository(RESULTS_DB_FILE)
    return _repository


# ---------------------------------------------------------------------------
# 账本写入失败持久化守卫（fail-closed，跨任务/跨进程 durable）
#
# 消费边界语义：accepted-submit 后邮箱不可逆消费；账本写入失败时保留
# Outlook pool 占用只在当前进程短暂成立（下一任务 reset_runtime_state 会
# 清空 reservation，重启会丢内存态）。因此写入一个持久化 guard 文件：
# 存在时拒绝启动任何注册任务，直到 operator 人工确认该邮箱已补写
# mailbox_consumptions 并删除 guard 文件。不允许自动清理。
# ---------------------------------------------------------------------------
LEDGER_GUARD_FILENAME = "ledger_write_failure.json"
RESULT_GUARD_FILENAME = "sub2api_result_write_failure.json"

# 进程内完整性闩锁：guard 文件本身也写入失败时的兜底——当前进程生命周期
# 内继续拒绝所有注册任务。（SQLite 与 guard 同盘双写的场景跨进程无法绝对
# 保证，作为 accepted residual risk。）
_MEMORY_INTEGRITY_LATCH = {"active": False, "reason": ""}


def _latch_integrity_failure(reason: str) -> None:
    """guard 写入失败时锁定本进程后续所有注册任务（fail-closed 兜底）。"""
    _MEMORY_INTEGRITY_LATCH["active"] = True
    _MEMORY_INTEGRITY_LATCH["reason"] = str(reason)[:500]


def ledger_guard_path() -> str:
    return os.path.join(ACCOUNTS_DIR, LEDGER_GUARD_FILENAME)


def result_guard_path() -> str:
    return os.path.join(ACCOUNTS_DIR, RESULT_GUARD_FILENAME)


def check_ledger_guard() -> dict:
    """检查持久化账本失败守卫；存在返回其内容（损坏也返回标记，fail-closed）。"""
    if _MEMORY_INTEGRITY_LATCH["active"]:
        return {
            "source": "process_memory_latch",
            "error": "完整性守卫写入失败，进程内已锁定: " + _MEMORY_INTEGRITY_LATCH["reason"],
        }
    try:
        path = ledger_guard_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {"error": "guard 内容非 JSON 对象"}
            return data
    except Exception as exc:  # 文件损坏也 fail-closed
        return {"error": f"guard 文件存在但无法解析: {exc}"}
    return {}


def write_ledger_guard(profile_id, email: str, mailbox_source: str, error: str) -> None:
    """原子写入守卫文件（tmp + os.replace）；自身写失败时进程内闩锁兜底。"""
    try:
        ensure_accounts_dir()
        try:
            profile_id_value = int(profile_id or 0)
        except (TypeError, ValueError):
            profile_id_value = 0
        payload = {
            "profile_id": profile_id_value,
            "email": str(email or ""),
            "mailbox_source": str(mailbox_source or ""),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "error": str(error or "")[:500],
        }
        tmp_path = ledger_guard_path() + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, ledger_guard_path())
    except Exception as exc:
        _latch_integrity_failure(f"{LEDGER_GUARD_FILENAME} 写入失败: {exc}")
        raise


def check_result_guard() -> dict:
    """检查 Sub2API 凭据恢复守卫；存在返回其内容（损坏也 fail-closed）。"""
    try:
        path = result_guard_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {"error": "guard 内容非 JSON 对象"}
            return data
    except Exception as exc:  # 文件损坏也 fail-closed
        return {"error": f"guard 文件存在但无法解析: {exc}"}
    return {}


def write_result_guard(payload: dict) -> None:
    """原子写入 Sub2API 凭据恢复守卫；自身写失败时进程内闩锁兜底。"""
    try:
        ensure_accounts_dir()
        tmp_path = result_guard_path() + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(dict(payload or {}), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, result_guard_path())
    except Exception as exc:
        _latch_integrity_failure(f"{RESULT_GUARD_FILENAME} 写入失败: {exc}")
        raise


def _refuse_start_if_ledger_guard() -> None:
    """持久化完整性守卫（账本失败 / Sub2API 凭据恢复）存在时拒绝启动。"""
    guard = check_ledger_guard()
    if guard:
        if guard.get("source") == "process_memory_latch":
            raise RuntimeError(
                "检测到进程内完整性闩锁（guard 文件写入失败），拒绝启动注册任务："
                f"{guard.get('error') or '未知错误'}；请排查磁盘/权限问题后重启服务再试"
            )
        raise RuntimeError(
            "检测到账本写入失败守卫（ledger_write_failure.json），拒绝启动注册任务："
            f"邮箱={guard.get('email') or '未知'}，Profile={guard.get('profile_id') or '未知'}，"
            "请人工确认该邮箱已补写消费账本后删除守卫文件再启动"
        )
    credential_guard = check_result_guard()
    if credential_guard:
        raise RuntimeError(
            "检测到 Sub2API 凭据恢复守卫（sub2api_result_write_failure.json），"
            "拒绝启动注册任务："
            f"邮箱={credential_guard.get('email') or '未知'}，"
            f"Profile={credential_guard.get('profile_id') or '未知'}，"
            "请人工将该凭据补录进 registration_results 后删除守卫文件再启动"
        )


def email_registered_successfully(email, *, profile_id: int) -> bool:
    """指定 Profile 作用域内：数据库或消费账本已有成功/已消耗记录时返回 True。

    检查顺序：消费账本（硬边界，作用域内跨来源）→ 历史 registration_results
    （同作用域）。命中任意一条的邮箱都不允许在该 Profile 内再次参与注册；
    同一邮箱仍可在其它 Profile 各注册一次。

    fail-closed：任一权威数据源无法确认（UNKNOWN）时异常向上传播，由 acquire
    响亮失败；宁可少注册一次，也不能因无法确认而重复消费邮箱。
    """
    normalized = str(email or "").strip()
    if not normalized:
        return False
    pid = normalize_profile_id(profile_id)
    repo = get_registration_repository()
    # 消费硬边界在作用域内跨来源生效：一个 email 在一个 Profile 里就是一个身份，
    # accounts 消费过的邮箱在 temp 中同样不可复用。不吞异常。
    if repo.is_mailbox_consumed_any_source(normalized, profile_id=pid):
        return True
    # 历史记录同样是安全判定：查询失败不得降级为“可用”。
    if repo.has_success(normalized, profile_id=pid):
        return True
    if repo.has_registered_or_consumed(normalized, profile_id=pid):
        return True
    return False


DEFAULT_CONFIG = {
    # Embedded OutlookEmail listens on loopback; explicit external bases remain
    # readable for deliberate legacy deployments.
    "outlookemail_api_base": "http://127.0.0.1:5000",
    "outlookemail_api_key": "",
    "outlookemail_source": "accounts",
    "outlookemail_group_id": "",
    "outlookemail_web_password": "",
    "outlookemail_session_cookie": "",
    "outlookemail_temp_tag_ids": "",
    "outlookemail_folder": "all",
    "outlookemail_top": 10,
    "outlookemail_pick_mode": "random",
    "proxy": "",
    "debug_mode": False,
    "browser_headless": False,
    "browser_locale": "en-US",
    "close_browser_on_stop": False,
    "log_level": "info",
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    # 账号间注册间隔（秒），0=不等待。填一个整数=N秒固定等待，填区间"60-120"=随机等待
    "account_interval": "60-120",
    "relay_enabled": False,
    "relay_strategy": "fill_first",
    "relay_proxy": "",
    "relay_request_timeout_seconds": 600,
    "relay_first_byte_timeout_seconds": 180,
    "relay_cooldown_seconds": 120,
    "relay_rate_cooldown_seconds": 30,
    "relay_model_cache_ttl_seconds": 900,
    "relay_max_attempts": 2,
    "relay_session_affinity_ttl_seconds": 3600,
}

config = DEFAULT_CONFIG.copy()


def classify_failure(exc) -> str:
    """失败分类：用于统计聚合。"""
    msg = str(exc or "")
    # 预期内、可操作的边界提示：邮箱池耗尽（fail-closed 拒绝复用）。
    # 必须在其它规则之前判定，避免被通用分类吞掉。
    if (
        "均已在本地标记为已注册/已消耗" in msg
        or "邮箱池为空" in msg
        or "没有 active 账号" in msg
        or "没有符合条件的可用邮箱" in msg
        or "当前没有可分配的邮箱" in msg
    ):
        return FAIL_NO_EMAIL
    low = msg.lower()
    if "未收到验证码" in msg or "验证码" in msg and "失败" in msg:
        return FAIL_CODE
    if (
        "浏览器" in msg
        or "page disconnected" in low
        or "与页面的连接已断开" in msg
        or "PageDisconnected" in msg
        or "disconnected" in low
    ):
        return FAIL_BROWSER
    return FAIL_OTHER


FAIL_CODE = "code_timeout"
FAIL_BROWSER = "browser"
FAIL_NO_EMAIL = "no_email_available"
FAIL_OTHER = "other"

FAIL_LABELS = {
    FAIL_CODE: "验证码超时",
    FAIL_BROWSER: "浏览器断开",
    FAIL_NO_EMAIL: "无可用邮箱",
    FAIL_OTHER: "其它",
}


def empty_fail_stats():
    return {k: 0 for k in FAIL_LABELS}


def format_fail_stats(stats: dict) -> str:
    parts = [f"{FAIL_LABELS.get(k, k)}={stats.get(k, 0)}" for k in FAIL_LABELS if stats.get(k, 0)]
    if not parts:
        return "无分类失败"
    return " | ".join(parts)


def new_registration_batch_id(source="web"):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{source}-{stamp}-{secrets.token_hex(3)}"


def capture_failure_screenshot(
    *,
    batch_id="",
    worker_id=0,
    email="",
    failure_type="",
    log_callback=None,
):
    """保存当前活动页面；页面不存在或已经断开时返回空路径。"""
    current_page = _active_page()
    if current_page is None:
        return ""

    def _safe_part(value, fallback):
        normalized = re.sub(r"[^A-Za-z0-9._@-]+", "_", str(value or "").strip())
        return normalized.strip("._-")[:80] or fallback

    folder = os.path.join(DATA_DIR, "screenshots", "registration-failures")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = "-".join(
        (
            _safe_part(batch_id, "batch"),
            f"w{max(int(worker_id or 0), 0) + 1}",
            _safe_part(email, "unknown"),
            _safe_part(failure_type, "failure"),
            stamp,
            secrets.token_hex(2),
        )
    ) + ".png"
    path = os.path.abspath(os.path.join(folder, filename))
    try:
        os.makedirs(folder, exist_ok=True)
        current_page.screenshot(path=path, full_page=True)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return ""
        if log_callback:
            log_callback(f"[截图] 浏览器失败现场已保存: {path}")
        return path
    except Exception as exc:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        if log_callback:
            log_callback(f"[Debug] 浏览器失败截图保存失败: {exc}")
        return ""


def persist_registration_result(
    *,
    batch_id,
    source,
    started_at,
    email="",
    password="",
    status="failure",
    provider="",
    worker_id=0,
    consumed_at="",
    failure_type="",
    failure_reason="",
    screenshot_path="",
    extra=None,
    profile_id,
    log_callback=None,
):
    """统一保存 Web 注册结果；写库异常返回 None 并由调用方做完整性处置。

    profile_id 决定记录的 Profile 作用域；邮箱状态固定为 consumed
    （提交目标站点即消费，OutlookEmail 侧保持 active）。
    """
    finished_epoch = time.time()
    try:
        started_epoch = float(started_at or finished_epoch)
    except (TypeError, ValueError):
        started_epoch = finished_epoch
    started_text = datetime.datetime.fromtimestamp(started_epoch).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    finished_text = datetime.datetime.fromtimestamp(finished_epoch).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    extra_data = dict(extra or {})
    # 冻结的邮箱来源随记录持久化（审计用）；消费硬边界仍按 (profile_id, email) 生效。
    if email:
        extra_data.setdefault("mailbox_source", _frozen_mailbox_source(email))
    try:
        repository = get_registration_repository()
        registration_id = repository.add_result(
            {
                "profile_id": normalize_profile_id(profile_id),
                "batch_id": batch_id,
                "source": source,
                "started_at": started_text,
                "finished_at": finished_text,
                "duration_seconds": max(finished_epoch - started_epoch, 0),
                "email": email,
                "password": password,
                "registration_status": status,
                "success": status == "success",
                "provider": provider or "outlookemail",
                "worker_id": worker_id,
                "failure_type": failure_type,
                "registration_error": str(failure_reason or ""),
                # 消费边界：提交目标站点后无论成败，邮箱在本地均为 consumed。
                "mail_status": "consumed" if consumed_at else "not_attempted",
                "consumed_at": str(consumed_at or ""),
                "screenshot_path": screenshot_path,
                "extra": extra_data,
            }
        )
        return registration_id
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] SQLite 保存注册结果失败: {exc}")
        return None


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def parse_account_interval() -> float:
    """解析 account_interval 配置，返回等待秒数。

    "0" / "" → 0（不等待）
    "30" → 30.0（固定 30 秒）
    "60-120" → 60~120 之间的随机值
    """
    import random

    raw = str(config.get("account_interval", "0") or "0").strip()
    if not raw or raw == "0":
        return 0.0
    if "-" in raw:
        parts = raw.split("-", 1)
        try:
            lo = max(int(parts[0].strip()), 0)
            hi = max(int(parts[1].strip()), lo)
            return float(random.randint(lo, hi))
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(int(raw))
    except ValueError:
        return 0.0


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


load_config()

# Camoufox 基于 Firefox，不加载 Chrome 扩展；Turnstile 交互由
# automation/turnstile.py 统一处理。
EXTENSION_PATH = ""


def get_proxies():
    proxy = resolve_proxy_url(config.get("proxy", ""))
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def reset_network_route_logs():
    with _network_route_log_lock:
        _network_route_log_keys.clear()


def _log_actual_http_route(method, url, *, proxies=None, proxy=""):
    """记录实际请求的接口和路由；相同方法/接口/路由只记录一次。"""
    parsed = urlsplit(str(url or ""))
    display_url = (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        if parsed.netloc
        else str(url or "")
    )
    proxy_value = str(proxy or "").strip()
    if not proxy_value and isinstance(proxies, dict):
        proxy_value = str(
            proxies.get(parsed.scheme)
            or proxies.get("all")
            or proxies.get("https")
            or proxies.get("http")
            or ""
        ).strip()
    route = f"代理 {redact_proxy_url(proxy_value)}" if proxy_value else "直连（不使用代理）"
    key = (str(method or "GET").upper(), display_url, route)
    with _network_route_log_lock:
        if key in _network_route_log_keys:
            return
        _network_route_log_keys.add(key)
    registration_log(f"[*] [网络] {key[0]} {display_url} -> {route}")


def get_outlookemail_api_base():
    return resolve_api_base(config)


def get_outlookemail_api_key():
    return str(config.get("outlookemail_api_key", "") or "").strip()


def get_outlookemail_source():
    return outlookemail_provider.normalize_source(config.get("outlookemail_source", "accounts"))


def _outlookemail_account_already_saved(email, profile_id: int):
    return email_registered_successfully(email, profile_id=profile_id)


def reset_outlookemail_runtime_state():
    outlookemail_provider.reset_runtime_state()


def _mailbox_source():
    """当前全局邮箱来源（accounts/temp）；仅作冻结失败时的回退。"""
    return get_outlookemail_source()


# 每次 attempt 在 acquire 时冻结的 mailbox_source，避免运行中改配置导致账本写错来源。
# 键为 lower(email)；attempt 结束后由 _forget_attempt_context 清理。
_frozen_mailbox_sources: dict[str, str] = {}
# 每次 acquire 冻结的 Profile 作用域（profile_id）：后续 mark/持久化都用
# 该值，不受任务运行期间任何切换影响。键为 lower(email)。
_frozen_profile_ids: dict[str, int] = {}
_frozen_mailbox_lock = threading.Lock()

# acquire 时点冻结的 Profile 快照：运行中改 Profile 不影响已获取邮箱的
# 持久化（与 job start 冻结同语义，双层保险）。键为 lower(email)。
_frozen_profile_snapshots: dict[str, dict] = {}


def _freeze_mailbox_source(email: str, source: str) -> None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    with _frozen_mailbox_lock:
        _frozen_mailbox_sources[normalized] = outlookemail_provider.normalize_source(source)


def _frozen_mailbox_source(email: str) -> str:
    """本次 attempt 冻结的来源；无冻结记录时回退到当前配置。"""
    normalized = str(email or "").strip().lower()
    with _frozen_mailbox_lock:
        frozen = _frozen_mailbox_sources.get(normalized, "")
    return frozen or _mailbox_source()


def frozen_mailbox_source(email: str) -> str:
    """公开访问器：acquire 时点冻结的 mailbox_source（无冻结则回退当前配置）。

    验证码拉取必须显式传该值：运行中改 Settings 不得改变本 attempt 的来源。
    """
    return _frozen_mailbox_source(email)


def _freeze_profile_id(email: str, profile_id: int) -> None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    with _frozen_mailbox_lock:
        _frozen_profile_ids[normalized] = normalize_profile_id(profile_id)


def _frozen_profile_id(email: str) -> int:
    """本次 attempt 冻结的 Profile 作用域；无冻结记录返回 0（调用方必须 fail-closed）。"""
    normalized = str(email or "").strip().lower()
    with _frozen_mailbox_lock:
        return int(_frozen_profile_ids.get(normalized, 0) or 0)


def _forget_attempt_context(email: str) -> None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    with _frozen_mailbox_lock:
        _frozen_mailbox_sources.pop(normalized, None)
        _frozen_profile_ids.pop(normalized, None)
        _frozen_profile_snapshots.pop(normalized, None)


def mark_mailbox_consumed(
    email,
    *,
    batch_id="",
    reason="",
    profile_id=None,
    log_callback=None,
) -> bool:
    """消费边界持久化：邮箱一旦提交给目标站点，立即写入 SQLite 消费账本（按 Profile 作用域）。

    remote active ≠ registration available：OutlookEmail 侧保持 active
    （后续可能还要收验证码），但该邮箱在该 Profile 作用域内永久失去注册资格。

    profile_id 为 None 时，使用 acquire 时冻结的 Profile 作用域；
    无冻结记录 → fail-closed 抛异常（绝不猜一个作用域写账本）。
    """
    normalized = str(email or "").strip()
    if not normalized:
        return False
    pid = normalize_profile_id(profile_id) if profile_id else _frozen_profile_id(normalized)
    if pid <= 0:
        raise Exception(
            f"无法确定 {normalized} 的 Profile 作用域（acquire 未冻结 profile_id），拒绝写消费账本"
        )
    try:
        repo = get_registration_repository()
        first = repo.mark_mailbox_consumed(
            pid,
            # 使用 acquire 时冻结的来源，不重新读全局配置。
            _frozen_mailbox_source(normalized),
            normalized,
            batch_id=str(batch_id or ""),
            reason=str(reason or ""),
        )
    except Exception as exc:
        # 账本写入失败必须响亮：这是防止重启后重复取用的唯一硬边界。
        if log_callback:
            log_callback(f"[!] 邮箱消费账本写入失败: {normalized} — {exc}")
        raise
    if first and log_callback:
        log_callback(f"[*] 邮箱已提交，本地标记 consumed（OutlookEmail 保持 active）: {normalized}")
    return first


def handle_cancelled_email(email, *, submitted: bool, log_callback=None) -> None:
    """取消任务时的邮箱生命周期：

    - 已提交目标站点（submitted=True）：消费边界已在提交时写入账本，无需额外处理；
    - 未提交（submitted=False）：释放预留，允许同批次内重新获取。
    """
    normalized = str(email or "").strip()
    if not normalized:
        return
    if submitted:
        if log_callback:
            log_callback(f"[*] 已停止；邮箱此前已提交，保持 consumed 状态: {normalized}")
    else:
        outlookemail_provider.release_email(normalized)
        if log_callback:
            log_callback(f"[*] 未提交即停止，已释放邮箱占用: {normalized}")


def acquire_email(profile: dict, *, log_callback=None):
    """Sub2API 业务的邮箱获取：Profile 白名单在池内过滤 + 按 Profile 查消费。

    返回 (email, source_key)；同时把 acquire 冻结的 mailbox_source 与
    profile_id 写入该邮箱的冻结上下文（后续 mark/persist 都用冻结值）。
    Profile 未启用 / 白名单无匹配邮箱等异常都向上响亮传播。
    """
    profile = dict(profile or {})
    profile_id = profile.get("id")
    if profile_id in (None, "", 0):
        raise Exception("缺少有效的 Sub2API Profile，无法获取邮箱")
    if not profile.get("enabled", True):
        raise Exception(f"Sub2API Profile #{profile_id} 已禁用，无法获取邮箱")
    pid = normalize_profile_id(profile_id)
    whitelist = list(profile.get("whitelist") or [])

    # 冻结本次 acquire 的来源。
    frozen_source = get_outlookemail_source()
    email = outlookemail_provider.acquire(
        http_get,
        direct_http_session,
        get_outlookemail_api_base(),
        api_key=get_outlookemail_api_key(),
        source=frozen_source,
        group_id=str(config.get("outlookemail_group_id", "") or "").strip(),
        web_password=resolve_login_password(config),
        session_cookie=resolve_legacy_session_cookie(config),
        temp_tag_ids=str(config.get("outlookemail_temp_tag_ids", "") or "").strip(),
        pick_mode=str(config.get("outlookemail_pick_mode", "random") or "random"),
        proxies={},
        # 消费判定严格按本 Profile 作用域。
        is_unavailable=lambda e: _outlookemail_account_already_saved(e, pid),
        # 白名单在池内过滤：非匹配邮箱保持 active、绝不烧掉。
        allowed_domains=whitelist or None,
        # accounts 来源必须在远端提交前证明当前授权可读邮件。
        preflight_messages=True,
        log_callback=log_callback,
    )[0]
    _freeze_mailbox_source(email, frozen_source)
    _freeze_profile_id(email, pid)
    with _frozen_mailbox_lock:
        _frozen_profile_snapshots[email.lower()] = {
            "profile_id": pid,
            "name": str(profile.get("name") or ""),
            "register_url": str(profile.get("register_url") or ""),
            "register_origin": str(profile.get("register_origin") or ""),
            "promo_configured": bool(str(profile.get("promo_code") or "").strip()),
            "invitation_configured": bool(str(profile.get("invitation_code") or "").strip()),
            "aff_configured": bool(str(profile.get("aff_code") or "").strip()),
        }
    return email, f"outlookemail:{frozen_source}:{email}"


def frozen_profile_snapshot(email: str) -> dict:
    """该邮箱 acquire 时点冻结的 Profile 快照；无则空 dict。"""
    normalized = str(email or "").strip().lower()
    with _frozen_mailbox_lock:
        snapshot = _frozen_profile_snapshots.get(normalized)
    return dict(snapshot) if snapshot else {}


def outlookemail_get_oai_code(
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    min_received_at=None,
    source=None,
):
    return outlookemail_provider.wait_code(
        http_get,
        direct_http_session,
        get_outlookemail_api_base(),
        email,
        api_key=get_outlookemail_api_key(),
        # 显式传入 source（如按记录冻结值）时优先；否则用当前 Settings。
        source=str(source or "").strip() or get_outlookemail_source(),
        web_password=resolve_login_password(config),
        session_cookie=resolve_legacy_session_cookie(config),
        folder=str(config.get("outlookemail_folder", "all") or "all"),
        top=config.get("outlookemail_top", 10),
        proxies={},
        timeout=timeout,
        poll_interval=poll_interval,
        min_received_at=min_received_at,
        raise_if_cancelled=raise_if_cancelled,
        sleep_with_cancel=sleep_with_cancel,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    # 通用 HTTP 默认直连。
    request_kwargs["proxies"] = proxies or {}
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def _http_request(method, url, **kwargs):
    kwargs.pop("_allow_direct_fallback", None)
    with direct_http_session() as session:
        return session.request(method, url, **_build_request_kwargs(**kwargs))


def http_get(url, **kwargs):
    return _http_request("GET", url, **kwargs)


def http_post(url, **kwargs):
    return _http_request("POST", url, **kwargs)


def http_delete(url, **kwargs):
    return _http_request("DELETE", url, **kwargs)


def direct_http_session():
    """创建不读取项目代理或环境代理的 HTTP 会话。"""
    session = requests.Session(trust_env=False)
    raw_request = session.request

    def logged_request(method, url, *args, **kwargs):
        _log_actual_http_route(
            method,
            url,
            proxies=kwargs.get("proxies"),
            proxy=kwargs.get("proxy", ""),
        )
        return raw_request(method, url, *args, **kwargs)

    session.request = logged_request
    return session


def is_debug_mode():
    return bool(config.get("debug_mode", False))


def is_browser_headless():
    force_headed = str(os.environ.get("SUB2API_FORCE_HEADED", "") or "").strip().lower()
    if force_headed in {"1", "true", "yes", "on"}:
        return False
    return bool(config.get("browser_headless", False))


def get_browser_locale() -> str:
    value = str(config.get("browser_locale", "en-US") or "en-US").strip()
    return value if value in {"en-US", "zh-CN"} else "en-US"


def should_close_browser_after_run(user_stopped: bool) -> bool:
    """正常结束时非调试模式关闭；手动停止时严格以勾选项为准。"""
    if user_stopped:
        return bool(config.get("close_browser_on_stop", False))
    return not is_debug_mode()


def maybe_stop_browser(user_stopped: bool = False, log_callback=None):
    if should_close_browser_after_run(user_stopped):
        # 手动勾选关闭时应优先于调试模式，因此这里显式 force。
        stop_browser(force=True)
        if log_callback:
            reason = "用户停止" if user_stopped else "任务结束"
            log_callback(f"[*] {reason}：已执行浏览器关闭")
        return
    if log_callback:
        if user_stopped:
            log_callback("[*] 用户停止：按当前勾选设置保留浏览器")
        else:
            log_callback("[*] 调试模式：正常结束后保留浏览器")


def get_log_level() -> str:
    level = str(config.get("log_level", "info") or "info").strip().lower()
    return level if level in ("info", "debug") else "info"


def should_emit_log(message: str) -> bool:
    """info 级别过滤 [Debug] 行；debug 全开。"""
    if get_log_level() == "debug":
        return True
    text = str(message or "")
    if text.lstrip().startswith("[Debug]") or " [Debug] " in text:
        return False
    return True


def _wire_runtime_modules():
    """向浏览器运行时注入本次任务依赖。"""
    _bs.configure(
        get_proxies=get_proxies,
        is_debug=is_debug_mode,
        is_headless=is_browser_headless,
        get_locale=get_browser_locale,
        extension_path=EXTENSION_PATH,
    )


def registration_log(message):
    if not should_emit_log(message):
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)


def _release_unconsumed_reservation(email: str) -> None:
    """attempt 未写入消费账本时释放邮箱运行时占用（同任务可再次获取）。

    fail-closed：无法确认账本状态（查询异常）时保留占用，宁可少用一个 slot。
    必须在 _forget_attempt_context 之前调用（需要冻结来源/作用域）。
    """
    normalized = str(email or "").strip()
    if not normalized:
        return
    pid = _frozen_profile_id(normalized)
    if pid <= 0:
        # 没有冻结作用域：不猜测，保留占用。
        return
    try:
        consumed = get_registration_repository().is_mailbox_consumed(
            pid,
            _frozen_mailbox_source(normalized),
            normalized,
        )
    except Exception:
        return
    if not consumed:
        outlookemail_provider.release_email(normalized)
        registration_log(f"[*] 未写入消费账本，已释放邮箱占用: {normalized}")


def run_sub2api_registration_job(count, profile_snapshot):
    """Sub2API 注册任务（唯一业务分支）。

    - 任务输入：count + 启动时冻结的 Profile 快照（运行中改 Profile 不影响本任务）；
    - 每个 attempt 由 sub2api_flow.run_sub2api_registration 执行（共享
      Camoufox 运行时 / Turnstile solver / OutlookEmail）；
    - 结果按 profile_id 持久化。
    """
    from backend.registration import sub2api_flow as _sub2api_flow

    profile_snapshot = dict(profile_snapshot or {})
    profile_id = normalize_profile_id(profile_snapshot.get("id"))
    profile_name = str(profile_snapshot.get("name") or f"#{profile_id}")

    _refuse_start_if_ledger_guard()
    controller = RegistrationStopController()
    reset_outlookemail_runtime_state()

    success_count = 0
    fail_count = 0
    fail_stats = empty_fail_stats()
    batch_id = new_registration_batch_id("web")
    # v1：单 worker 固定（不对外暴露并发浏览器配置）
    registration_log(
        f"[*] 任务启动：Profile={profile_name}(#{profile_id}) 目标={count} 并发=1"
    )
    traceback_log_lock = threading.Lock()
    logged_traceback_signatures = set()

    def _persist_result(*, started_at, worker_id=0, **kwargs):
        expected_failure = str(kwargs.get("failure_type") or "") == FAIL_NO_EMAIL
        trace_text = ""
        if (
            str(kwargs.get("status") or "").strip().lower() == "failure"
            and not expected_failure
            and not kwargs.get("screenshot_path")
        ):
            trace_text = current_exception_traceback()
            if trace_text:
                extra = dict(kwargs.get("extra") or {})
                extra["exception_traceback"] = trace_text
                extra["exception_type"] = trace_text.rstrip().splitlines()[-1]
                kwargs["extra"] = extra
                signature = hash(trace_text)
                with traceback_log_lock:
                    should_log = signature not in logged_traceback_signatures
                    if should_log:
                        logged_traceback_signatures.add(signature)
                if should_log:
                    registration_log(
                        "[异常堆栈]\n" + current_exception_traceback(TRACEBACK_LOG_MAX_CHARS)
                    )
        kwargs.setdefault("profile_id", profile_id)
        return persist_registration_result(
            batch_id=batch_id,
            source="web",
            started_at=started_at,
            worker_id=int(worker_id) + 1,
            log_callback=registration_log,
            **kwargs,
        )

    def _attempt_from_exc(exc):
        """从 flow 附带的异常身份重建最小 attempt 快照。"""
        return _sub2api_flow.Sub2apiAttemptResult(
            email=str(getattr(exc, "sub2api_email", "") or ""),
            password=str(getattr(exc, "sub2api_password", "") or ""),
            status="failure",
            consumed=bool(getattr(exc, "sub2api_consumed", False)),
            final_url=str(getattr(exc, "sub2api_final_url", "") or ""),
        )

    def _handle_result_write_integrity_failure(result, *, status, failure_type="") -> None:
        """已到提交边界的账号 SQLite 结果写库失败：凭据写入恢复守卫后中止。

        密码每账号随机且唯一持久化位置是 registration_results；
        INSERT 失败若被吞掉会留下「已创建成功但无法交付」的账号。此处
        fail-closed：不打印注册成功、不继续下一账号，operator 人工补录后
        删除守卫文件。守卫自身也写失败时由进程内闩锁兜底（write_result_guard）。
        """
        try:
            write_result_guard(
                {
                    "profile_id": profile_id,
                    "profile_name": profile_name,
                    "email": str(result.email or ""),
                    "password": str(result.password or ""),
                    "mailbox_source": _frozen_mailbox_source(result.email),
                    "registration_status": str(status or ""),
                    "failure_type": str(failure_type or ""),
                    "final_url": str(getattr(result, "final_url", "") or ""),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "error": "SQLite registration_results 写入失败：已提交边界后凭据仅存于内存",
                }
            )
            registration_log(
                f"[!] 凭据已保存到恢复守卫 {RESULT_GUARD_FILENAME}；"
                "人工补录 registration_results 并删除该文件前，所有注册任务将被拒绝启动"
            )
        except Exception as guard_exc:
            registration_log(
                f"[!] 恢复守卫写入也失败（当前进程内已锁定后续任务）: {guard_exc}"
            )
        registration_log(
            f"[!] 结果写库失败但账号已提交成功，任务中止: 邮箱={result.email}"[:200]
        )

    try:
        browser_profile_dir = ""
        try:
            start_browser(log_callback=registration_log)
            identity = assert_fresh_browser_identity()
            browser_profile_dir = str(identity.get("profile_dir") or "")
            registration_log("[*] 初始浏览器身份验证通过（Cookie=0，站点存储=0）")
            registration_log("[*] 浏览器已启动")
        except Exception as boot_exc:
            fail_count += count
            fail_stats[FAIL_BROWSER] = fail_stats.get(FAIL_BROWSER, 0) + count
            registration_log(f"[-] 浏览器启动失败，{count} 个任务均记为失败: {boot_exc}")
            for _ in range(max(int(count or 0), 0)):
                _persist_result(
                    started_at=time.time(),
                    status="failure",
                    failure_type=FAIL_BROWSER,
                    failure_reason=str(boot_exc),
                )
            return

        i = 0
        while i < count:
            if controller.should_stop():
                break
            registration_log(f"--- 开始第 {i + 1}/{count} 个账号（Profile {profile_name}） ---")
            attempt_started_at = time.time()
            email = ""
            consumed_at = ""
            result = None
            try:
                if i > 0:
                    registration_log("[*] 正在为下一账号创建全新浏览器身份")
                    try:
                        restart_browser(log_callback=registration_log)
                        identity = assert_fresh_browser_identity(
                            previous_profile_dir=browser_profile_dir
                        )
                        browser_profile_dir = str(identity.get("profile_dir") or "")
                    except Exception as restart_exc:
                        raise RuntimeError(
                            f"浏览器身份隔离重启失败: {restart_exc}"
                        ) from restart_exc
                    registration_log(
                        "[*] 下一账号已切换到全新浏览器身份"
                        "（资料目录已轮换，Cookie=0，站点存储=0）"
                    )
                result = _sub2api_flow.run_sub2api_registration(
                    profile_snapshot,
                    batch_id=batch_id,
                    log_callback=registration_log,
                    cancel_callback=controller.should_stop,
                )
                email = result.email
                if result.consumed:
                    consumed_at = RegistrationRepository.now_text()
                if result.status == "success":
                    registration_id = _persist_result(
                        started_at=attempt_started_at,
                        email=result.email,
                        password=result.password,
                        status="success",
                        consumed_at=consumed_at,
                        extra=_result_extra(result),
                    )
                    if registration_id is None:
                        # 已创建成功的账号密码只存在于内存：绝不吞掉写库失败。
                        _handle_result_write_integrity_failure(result, status="success")
                        controller.stop()
                        break
                    success_count += 1
                    i += 1
                    registration_log(f"[+] 注册成功: {result.email}")
                else:
                    persisted_id = _persist_result(
                        started_at=attempt_started_at,
                        email=result.email,
                        password=result.password,
                        status="failure",
                        consumed_at=consumed_at,
                        failure_type=result.failure_type or FAIL_OTHER,
                        failure_reason=result.failure_reason,
                        screenshot_path=result.screenshot_path,
                        extra=_result_extra(result),
                    )
                    if result.consumed and persisted_id is None:
                        # 已提交但验证码超时等 consumed 失败：密码同样必须可恢复。
                        _handle_result_write_integrity_failure(
                            result,
                            status="failure",
                            failure_type=result.failure_type or FAIL_OTHER,
                        )
                        controller.stop()
                        break
                    fail_count += 1
                    fail_stats[result.failure_type or FAIL_OTHER] = fail_stats.get(
                        result.failure_type or FAIL_OTHER, 0
                    ) + 1
                    i += 1
                    registration_log(
                        f"[-] 失败 [{FAIL_LABELS.get(result.failure_type, result.failure_type)}]: "
                        f"{result.failure_reason}"
                    )
                    if not result.consumed:
                        _release_unconsumed_reservation(result.email)
            except RegistrationCancelled as exc:
                # 取消可能发生在 flow 中途：此时局部 email 仍为空，
                # 必须从 flow 附带的 sub2api_email 恢复邮箱身份才能释放占用。
                email = str(getattr(exc, "sub2api_email", "") or "") or email
                registration_log("[!] 任务被停止")
                _release_unconsumed_reservation(email)
                break
            except _sub2api_flow.LedgerWriteError as exc:
                # 已到提交边界但账本写入失败：fail-closed——保留邮箱占用、
                # 响亮中止任务，绝不释放后继续下一账号。
                # 保留占用只在当前进程短暂成立：下一任务 reset_runtime_state
                # 会清空 reservation，重启丢内存态——因此写持久化守卫文件，
                # 后续任务（含重启后）在 acquire 前直接拒绝启动，直到 operator
                # 人工补写账本并删除守卫。
                registration_log(f"[!] 邮箱已提交但消费账本写入失败，任务中止: {exc}")
                # 异常路径下 runner 局部 email 通常为空，从异常附带的 attempt 身份恢复。
                attempt = _attempt_from_exc(exc)
                if not attempt.email:
                    attempt.email = email or str(getattr(exc, "email", "") or "")
                try:
                    write_ledger_guard(
                        profile_id,
                        attempt.email,
                        _frozen_mailbox_source(attempt.email) if attempt.email else "",
                        str(exc),
                    )
                    registration_log(
                        f"[!] 已写持久化账本失败守卫: {ledger_guard_path()}；"
                        "人工补写账本并删除该文件前，注册任务将被拒绝启动"
                    )
                except Exception as guard_exc:
                    registration_log(f"[!] 持久化账本失败守卫写入失败: {guard_exc}")
                # 账本写入失败发生在 accepted-submit 之后：随机密码仍须可恢复。
                # 同时写凭据恢复守卫（写失败由 write_result_guard 内部进程内闩锁兜底）。
                try:
                    write_result_guard(
                        {
                            "profile_id": profile_id,
                            "profile_name": profile_name,
                            "email": attempt.email,
                            "password": attempt.password,
                            "mailbox_source": (
                                _frozen_mailbox_source(attempt.email)
                                if attempt.email
                                else ""
                            ),
                            "registration_status": "failure",
                            "failure_type": "ledger_write_failure",
                            "final_url": attempt.final_url,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "error": (
                                "accepted-submit 后消费账本写入失败：凭据仅存于内存，"
                                "operator 须补写 ledger 并保存凭据后删除两个守卫文件"
                            ),
                        }
                    )
                    registration_log(
                        f"[!] 凭据已同步写入恢复守卫 {RESULT_GUARD_FILENAME}；"
                        "operator 补写账本+凭据并删除两个守卫文件前，所有注册任务将被拒绝启动"
                    )
                except Exception as cred_guard_exc:
                    registration_log(
                        f"[!] 凭据恢复守卫写入也失败（当前进程内已锁定后续任务）: {cred_guard_exc}"
                    )
                controller.stop()
                break
            except Exception as exc:
                kind = getattr(exc, "failure_type", None) or classify_failure(exc)
                # flow 异常路径附带完整 attempt 身份（acquire 成功后才设置；
                # 密码/消费边界/最终 URL 随异常传递，避免 post-submit
                # 意外异常丢掉随机密码）。
                email = str(getattr(exc, "sub2api_email", "") or "") or email
                password = str(getattr(exc, "sub2api_password", "") or "")
                consumed = bool(getattr(exc, "sub2api_consumed", False))
                if consumed and not consumed_at:
                    consumed_at = RegistrationRepository.now_text()
                if kind == FAIL_NO_EMAIL:
                    # 预期内边界：不落库、不截屏、不打堆栈，直接收尾。
                    registration_log("[!] OutlookEmail 无可用新邮箱（均已消费），任务提前结束")
                    registration_log("[!] 请补充新邮箱后重新点「开始注册」；已消费邮箱不会被复用")
                    controller.stop()
                    break
                fail_count += 1
                fail_stats[kind] = fail_stats.get(kind, 0) + 1
                persisted_id = _persist_result(
                    started_at=attempt_started_at,
                    email=email,
                    password=password,
                    status="failure",
                    consumed_at=consumed_at,
                    failure_type=kind,
                    failure_reason=str(exc),
                    # 异常路径 result 局部仍为 None：用异常附带的 attempt 身份重建快照
                    extra=_result_extra(_attempt_from_exc(exc)),
                )
                registration_log(f"[-] 失败 [{FAIL_LABELS.get(kind, kind)}]: {exc}")
                if consumed:
                    if persisted_id is None:
                        # 已消费但异常路径结果写库失败：同凭据 durability 边界，
                        # 凭据入恢复守卫并中止（绝不静默丢密码）。
                        _handle_result_write_integrity_failure(
                            _attempt_from_exc(exc),
                            status="failure",
                            failure_type=kind,
                        )
                        controller.stop()
                        break
                elif email:
                    # 账本已确认未消费才释放；边界后异常（账本已有行）不会释放。
                    _release_unconsumed_reservation(email)
                i += 1
            finally:
                _forget_attempt_context(email)
    except Exception as exc:
        registration_log(f"[!] 任务异常: {exc}")
    finally:
        try:
            user_stopped = bool(controller.should_stop())
            if user_stopped:
                maybe_stop_browser(user_stopped=True, log_callback=registration_log)
            else:
                cleanup_runtime_memory(log_callback=registration_log, reason="任务结束")
        except BaseException:
            pass
        try:
            registration_log(
                f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}"
                + (f" | {format_fail_stats(fail_stats)}" if fail_count else "")
            )
        except BaseException:
            pass


def _result_extra(result) -> dict:
    """结果 extra_json 快照（审计用；不含 promo/invitation 明文）。"""
    extra = {}
    if result is not None:
        email = str(getattr(result, "email", "") or "")
        snapshot = frozen_profile_snapshot(email) if email else {}
        if snapshot:
            extra.update(snapshot)
        if getattr(result, "final_url", ""):
            extra["final_url"] = str(result.final_url)
        if getattr(result, "diagnostics", None):
            extra["diagnostics"] = result.diagnostics
    return extra
