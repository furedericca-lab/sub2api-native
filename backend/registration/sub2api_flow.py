# -*- coding: utf-8 -*-
"""Sub2API 站点注册流程（Camoufox/PageAdapter 移植，无第二套浏览器/验证码运行时）。

Sub2API 注册使用的公共运行时边界：
- 浏览器运行时：backend.automation.session（thread-local page，复用 Camoufox）；
- Turnstile：存在则调用 backend.automation.turnstile.get_turnstile_token
  （现有 solver），不存在跳过；
- 邮箱/验证码：engine.acquire_email（白名单池内过滤 + profile 消费判定 +
  冻结 mailbox_source）与 engine.outlookemail_get_oai_code。

消费边界（accepted-submit）：
- 点击提交后进入 /email-verify → 先 mark_mailbox_consumed，再轮询验证码；
- 直接进入 /dashboard（免验证码站点）→ mark consumed 并记成功；
- 仍停在注册页 / 按钮 disabled / CF 挑战中 → 未消费，重试直到超时；
- 意外页面 → 截图 + URL + DOM 诊断后失败，绝不盲目消费。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import secrets
import string

from backend.automation import session as _bs
from backend.automation.session import page as _page_proxy
from backend.automation.turnstile import get_turnstile_token
from backend.registration import engine as _engine
from backend.registration.runtime import raise_if_cancelled, sleep_with_cancel
from backend.registration.site_failures import (
    ALREADY_REGISTERED,
    RegistrationResponseMonitor,
    classify_registration_failure,
)

# 失败类型（与 engine 命名空间一致，便于统计聚合）
FAIL_OTHER = "other"
FAIL_FORM = "form_mismatch"
FAIL_CODE = "code_timeout"
FAIL_TURNSTILE = "turnstile_failed"
FAIL_PAGE = "unexpected_page"
FAIL_ALREADY_REGISTERED = ALREADY_REGISTERED

# 验证码轮询（秒）
CODE_POLL_TIMEOUT = 240.0
CODE_POLL_INTERVAL = 3.0

# 提交后 URL 判定窗口（秒）
SUBMIT_NAV_TIMEOUT = 20.0

# Turnstile 检测选择器（任一命中即认为页面有 CF 挑战）
TURNSTILE_SELECTORS = (
    "input[name='cf-turnstile-response']",
    "iframe[src*='challenges.cloudflare.com'] iframe",
    ".cf-turnstile",
    "[data-testid='registration-turnstile']",
)

# 登录协议勾选：常见 label 特征（存在且未勾选时才点击）
AGREEMENT_HINTS = (
    "terms", "条款", "privacy", "隐私", "agree", "同意",
)


class Sub2apiFlowError(Exception):
    """Sub2API 流程失败；failure_type 用于结果统计。"""

    def __init__(self, message: str, *, failure_type: str = FAIL_OTHER):
        super().__init__(message)
        self.failure_type = failure_type


class LedgerWriteError(Exception):
    """已到提交边界但消费账本写入失败（fail-closed：保留占用并中止任务）。"""

    def __init__(self, message: str, *, email: str = ""):
        super().__init__(message)
        self.email = email


# ---------------------------------------------------------------------------
# 页面原语（全部走共享 PageAdapter / 共享 solver，不引入第二套运行时）
# ---------------------------------------------------------------------------

def _require_page():
    p = _page_proxy if bool(_page_proxy) else _bs.active_page()
    if p is None:
        raise Sub2apiFlowError("浏览器页面未就绪", failure_type="browser")
    return p


def _log(log_callback: Optional[Callable[[str], None]], message: str) -> None:
    if log_callback:
        log_callback(message)


def _sleep(seconds: float, cancel_callback: Optional[Callable[[], bool]]) -> None:
    sleep_with_cancel(seconds, cancel_callback)


def _raise_if_cancelled(cancel_callback: Optional[Callable[[], bool]]) -> None:
    raise_if_cancelled(cancel_callback)


def _js(p, script: str, *args):
    return p.run_js(script, *args)


def _visible_inputs(p) -> list:
    """当前页面可见的表单控件快照（诊断 + 存在性判定）。"""
    result = _js(
        p,
        r"""
try {
  function vis(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  }
  const nodes = Array.from(document.querySelectorAll('input, textarea'));
  const out = [];
  for (const node of nodes) {
    if (node.type === 'hidden' || !vis(node)) continue;
    out.push({tag: node.tagName, type: node.type || '', id: node.id || '',
              name: node.name || '', autocomplete: node.autocomplete || '',
              placeholder: node.placeholder || ''});
  }
  const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
  const btns = [];
  for (const node of buttons.slice(0, 20)) {
    if (!vis(node)) continue;
    btns.push({
      text: (node.innerText || node.textContent || '').trim().slice(0, 40),
      disabled: !!node.disabled,
      type: node.type || '',
      id: node.id || '',
    });
  }
  return {url: location.href, title: document.title, inputs: out, buttons: btns,
          body: (document.body && document.body.innerText || '').slice(0, 500)};
} catch (e) { return {error: String(e)}; }
""",
    )
    return result if isinstance(result, dict) else {"error": str(result)}


def _find_input(
    p,
    *,
    ids: tuple,
    names: tuple,
    types: Optional[tuple] = None,
    autocompletes: Optional[tuple] = None,
    placeholders: Optional[tuple] = None,
    marker: str = "field",
):
    """按语义属性定位可见输入框，并返回可复用的临时 selector。"""
    found = _js(
        p,
        r"""
return (function (arg_ids, arg_names, arg_types, arg_autocompletes, arg_placeholders, arg_marker) {
  function vis(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  }
  const nodes = Array.from(document.querySelectorAll('input, textarea'));
  const visible = nodes.filter((node) => node.type !== 'hidden' && vis(node));
  const ids = arg_ids.map((value) => String(value || '').replace(/^#/, '').toLowerCase());
  const names = arg_names.map((value) => String(value || '').toLowerCase());
  const types = arg_types.map((value) => String(value || '').toLowerCase());
  const completions = arg_autocompletes.map((value) => String(value || '').toLowerCase());
  const hints = arg_placeholders.map((value) => String(value || '').toLowerCase());
  const find = (predicate) => visible.find(predicate);
  const node =
    find((item) => ids.includes(String(item.id || '').toLowerCase())) ||
    find((item) => names.includes(String(item.name || '').toLowerCase())) ||
    find((item) => completions.includes(String(item.autocomplete || '').toLowerCase())) ||
    find((item) => types.includes(String(item.type || '').toLowerCase())) ||
    find((item) => hints.some((hint) =>
      hint && String(item.placeholder || '').toLowerCase().includes(hint)
    ));
  if (node) {
    const marker = `sub2api-${String(arg_marker || 'field').replace(/[^a-z0-9_-]/gi, '-')}`;
    node.setAttribute('data-sub2api-native-field', marker);
    return {
      ok: true,
      selector: `[data-sub2api-native-field="${marker}"]`,
      id: node.id || '',
      name: node.name || '',
      type: node.type || ''
    };
  }
  return {ok: false};
})(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4], arguments[5]);
""",
        list(ids),
        list(names),
        list(types or []),
        list(autocompletes or []),
        list(placeholders or []),
        str(marker or "field"),
    )
    return found if isinstance(found, dict) and found.get("ok") else None


def _set_value(p, selector: str, value: str) -> bool:
    """用 React 兼容的原生 setter 写入并触发 input 事件。"""
    return bool(
        _js(
            p,
            r"""
return (function (arg_sel, arg_val) {
  const node = document.querySelector(arg_sel);
  if (!node) return false;
  const proto = node.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) setter.set.call(node, arg_val);
  else node.value = arg_val;
  node.dispatchEvent(new Event('input', {bubbles: true}));
  node.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})(arguments[0], arguments[1]);
""",
            selector,
            value,
        )
    )


def _button_snapshot(p) -> list:
    snap = _visible_inputs(p)
    return list(snap.get("buttons") or [])


def _page_reports_already_registered(p) -> bool:
    snapshot = _visible_inputs(p)
    return classify_registration_failure(
        {"message": str(snapshot.get("body") or "")}
    ) is not None


def _already_registered_result(
    p,
    result: Sub2apiAttemptResult,
    batch_id: str,
    worker_id: int,
    email: str,
    log_callback=None,
) -> Sub2apiAttemptResult:
    try:
        _engine.mark_mailbox_consumed(
            email,
            batch_id=batch_id,
            reason="目标站点确认该完整邮箱地址已注册",
            log_callback=log_callback,
        )
    except Exception as exc:
        raise LedgerWriteError(str(exc), email=email) from exc
    result.consumed = True
    result.failure_type = FAIL_ALREADY_REGISTERED
    result.failure_reason = "目标站点确认该完整邮箱地址已注册"
    result.diagnostics = _capture_diagnostics(p, "already_registered")
    result.screenshot_path = _take_failure_screenshot(
        p, batch_id, worker_id, email, FAIL_ALREADY_REGISTERED
    )
    return result


def _find_submit_button(p) -> Optional[str]:
    """定位提交按钮的 CSS 选择器；未找到返回 None。"""
    result = _js(
        p,
        r"""
return (function () {
  function vis(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  }
  const hints = ['create', 'register', 'sign up', 'continue', 'submit',
                 'create account', '注册', '创建', '继续', '提交'];
  const nodes = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'));
  for (const node of nodes) {
    if (node.disabled || !vis(node)) continue;
    const text = (node.innerText || node.textContent || node.value || '').trim().toLowerCase();
    if (hints.some((h) => text.includes(h))) {
      const idAttr = node.id ? document.querySelector(`#${CSS.escape(node.id)}`) : null;
      if (node.id) return {ok: true, selector: `#${CSS.escape(node.id)}`, text: text.slice(0, 40)};
      return {ok: true, selector: '__text__:' + text.slice(0, 40), text: text.slice(0, 40)};
    }
  }
  return {ok: false};
})();
""",
    )
    if isinstance(result, dict) and result.get("ok"):
        return str(result.get("selector") or "")
    return None


def _click_button(p, selector: str) -> bool:
    if selector.startswith("__text__:"):
        text = selector[len("__text__:") :]
        return bool(
            _js(
                p,
                r"""
return (function (arg_text) {
  const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
  for (const node of nodes) {
    if (node.disabled) continue;
    const t = (node.innerText || node.textContent || '').trim();
    if (t === arg_text || t.toLowerCase() === arg_text.toLowerCase()) {
      node.click();
      return true;
    }
  }
  return false;
})(arguments[0]);
""",
                text,
            )
        )
    return bool(
        _js(
            p,
            r"""
return (function (arg_sel) {
  const node = document.querySelector(arg_sel);
  if (!node || node.disabled) return false;
  node.click();
  return true;
})(arguments[0]);
""",
            selector,
        )
    )


# ---------------------------------------------------------------------------
# 密码 / URL 构造
# ---------------------------------------------------------------------------

def generate_password() -> str:
    """每账号随机密码（高强度策略：N + 8 位 hex + !a7# + urlsafe + 随机尾）。"""
    letters = string.ascii_letters + string.digits
    token = secrets.token_hex(4)
    tail = secrets.token_urlsafe(6)
    return f"N{token}!a7#{tail[:6]}{secrets.choice(letters)}"


def build_registration_url(profile: dict, aff_code: str = "") -> str:
    """由 register_url 构造最终 URL；推广码写入站点约定的 ``aff`` 参数。

    若 URL 已带 #affiliate_code 锚点且为空，则同时把该 query 参数补上
    （部分站点用 hash 参数识别推广位）。
    """
    base = str((profile or {}).get("register_url") or "").strip()
    if not base:
        raise Sub2apiFlowError("Profile 缺少 register_url", failure_type=FAIL_FORM)
    code = str(aff_code or (profile or {}).get("aff_code") or "").strip()
    parts = urlsplit(base)
    if code:
        query = parse_qs(parts.query, keep_blank_values=True)
        # 三个当前测试站点均从 ?aff=... 读取推荐码；数据库字段仍叫
        # aff_code 以保持 API/存储兼容，不能把内部字段名泄露到站点 URL。
        query["aff"] = [code]
        # hash 形式的 #affiliate_code 参数（存在但为空时补上）
        hash_query = parse_qs(urlsplit("#" + parts.fragment).query if parts.fragment else "", keep_blank_values=True)
        if parts.fragment and "affiliate_code" in hash_query and not hash_query["affiliate_code"][0]:
            hash_query["affiliate_code"] = [code]
            fragment = urlencode(hash_query, doseq=True)
            parts = parts._replace(fragment=fragment)
        parts = parts._replace(query=urlencode(query, doseq=True))
    return urlunsplit(parts)


# ---------------------------------------------------------------------------
# 表单填写
# ---------------------------------------------------------------------------

def _accept_login_agreement(p, log_callback) -> None:
    """Accept checkbox or modal agreements before locating the real submit."""
    accepted = _js(
        p,
        r"""
return (function () {
  const hints = %s;
  const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
  for (const box of boxes) {
    const label = (box.closest('label') || box.parentElement || {});
    const text = ((label && (label.innerText || label.textContent)) || '')
      .toLowerCase().slice(0, 200);
    const idText = String(box.id || '') + ' ' + String(box.name || '');
    if (hints.some((h) => text.includes(h) || idText.toLowerCase().includes(h))) {
      if (!box.checked) {
        const proto = window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'checked');
        if (setter && setter.set) setter.set.call(box, true);
        else box.checked = true;
        box.dispatchEvent(new Event('input', {bubbles: true}));
        box.dispatchEvent(new Event('change', {bubbles: true}));
        return {handled: true, kind: 'checkbox'};
      }
      return {handled: false, already: true, kind: 'checkbox'};
    }
  }
  function vis(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  }
  const dialogs = Array.from(document.querySelectorAll(
    '[role="dialog"], [aria-modal="true"], .modal, .fixed.inset-0'
  )).filter(vis);
  const agreementHints = ['terms', 'privacy', 'policy', 'agreement', '条款', '隐私', '协议'];
  const acceptHints = ['accept', 'agree', 'continue', '同意', '接受', '继续'];
  for (const dialog of dialogs) {
    const body = (dialog.innerText || dialog.textContent || '').toLowerCase();
    if (!agreementHints.some((hint) => body.includes(hint))) continue;
    const buttons = Array.from(dialog.querySelectorAll('button, [role="button"]'));
    const button = buttons.find((node) => {
      if (!vis(node) || node.disabled) return false;
      const text = (node.innerText || node.textContent || '').trim().toLowerCase();
      return acceptHints.some((hint) => text.includes(hint));
    });
    if (button) {
      button.click();
      return {handled: true, kind: 'modal'};
    }
  }
  return {handled: false};
})();
""" % "['terms','条款','privacy','隐私','agree','同意']",
    )
    if isinstance(accepted, dict) and accepted.get("handled"):
        label = "协议弹窗" if accepted.get("kind") == "modal" else "登录协议"
        _log(log_callback, f"[*] 已接受{label}")
        _sleep(0.3, None)
    elif isinstance(accepted, dict) and accepted.get("already"):
        _log(log_callback, "[Debug] 登录协议已勾选")


def _fill_form_field(
    p,
    profile: dict,
    *,
    ids: tuple,
    names: tuple,
    types: tuple = (),
    autocompletes: tuple = (),
    placeholders: tuple = (),
    value: str,
    label: str,
    required: bool,
    log_callback=None,
) -> None:
    found = _find_input(
        p,
        ids=ids,
        names=names,
        types=types,
        autocompletes=autocompletes,
        placeholders=placeholders,
        marker=str(names[0] if names else label),
    )
    if found is None:
        if required:
            raise Sub2apiFlowError(
                f"页面缺少 {label} 输入框（form 结构不符）", failure_type=FAIL_FORM
            )
        _log(log_callback, f"[Debug] 页面无 {label} 字段（未配置，跳过）")
        return
    selector = str(found.get("selector") or "")
    if not selector:
        raise Sub2apiFlowError(f"{label} 定位结果无可用 selector", failure_type=FAIL_FORM)
    if not _set_value(p, selector, value):
        raise Sub2apiFlowError(f"{label} 写入失败", failure_type=FAIL_FORM)


def fill_registration_form(
    p,
    profile: dict,
    email: str,
    password: str,
    *,
    log_callback=None,
    cancel_callback=None,
) -> None:
    """填写 email/password/promo/invitation；缺失必填字段 → form_mismatch。"""
    _fill_form_field(
        p, profile, ids=("email",), names=("email",), types=("email",),
        autocompletes=("email", "username"), placeholders=("email", "邮箱"),
        value=email, label="邮箱", required=True, log_callback=log_callback,
    )
    _fill_form_field(
        p, profile, ids=("password",), names=("password",), types=("password",),
        autocompletes=("new-password",), placeholders=("password", "密码"),
        value=password, label="密码", required=True, log_callback=log_callback,
    )
    if str((profile or {}).get("promo_code") or "").strip():
        _fill_form_field(
            p, profile, ids=("#promo_code", "promo_code"),
            names=("promo_code", "promoCode", "promo"),
            value=str(profile["promo_code"]).strip(),
            label="promo_code", required=True, log_callback=log_callback,
        )
    if str((profile or {}).get("invitation_code") or "").strip():
        _fill_form_field(
            p, profile, ids=("#invitation_code", "invitation_code"),
            names=("invitation_code", "invitationCode", "invite", "invite_code"),
            value=str(profile["invitation_code"]).strip(),
            label="invitation_code", required=True, log_callback=log_callback,
        )
    _accept_login_agreement(p, log_callback)


def _turnstile_present(p) -> bool:
    for selector in TURNSTILE_SELECTORS:
        try:
            count = _js(
                p,
                r"""
return (function (arg_sel) {
  try {
    const nodes = document.querySelectorAll(arg_sel);
    return nodes ? nodes.length : 0;
  } catch (e) { return 0; }
})(arguments[0]);
""",
                selector,
            )
            if int(count or 0) > 0:
                return True
        except Exception:
            continue
    # iframe 特征（challenges.cloudflare.com）
    try:
        frames = getattr(_bs.active_page(), "raw_page", None)
        if frames is not None:
            for frame in frames.frames:
                if "challenges.cloudflare.com" in str(getattr(frame, "url", "") or ""):
                    return True
    except Exception:
        pass
    return False


def _ensure_turnstile(
    p,
    *,
    log_callback=None,
    cancel_callback=None,
) -> str:
    """Turnstile 存在 → 复用共享 solver 取 token；不存在 → 跳过（返回空）。"""
    if not _turnstile_present(p):
        _log(log_callback, "[Debug] 页面未检测到 Turnstile，跳过")
        return ""
    _log(log_callback, "[*] 检测到 Turnstile，调用共享 solver…")
    token = get_turnstile_token(
        log_callback=log_callback, cancel_callback=cancel_callback
    )
    return str(token or "")


def _ensure_cap_widget(p, *, log_callback=None, cancel_callback=None) -> str:
    """Solve an in-page Cap widget and let its Vue owner receive the token."""
    present = _js(
        p,
        "return !!document.querySelector('cap-widget, .cap-container');",
    )
    if not present:
        return ""
    _log(log_callback, "[*] 检测到 Cap 注册验证，驱动页面内 widget…")
    raw_page = getattr(p, "raw_page", None)
    try:
        if raw_page is not None:
            raw_page.wait_for_function(
                "() => !!document.querySelector('cap-widget')?.shadowRoot?.querySelector('.captcha-trigger')",
                timeout=30_000,
            )
    except Exception as exc:
        raise Sub2apiFlowError(
            f"Cap widget 未完成初始化: {str(exc)[:180]}", failure_type=FAIL_TURNSTILE
        ) from exc
    armed = _js(
        p,
        r"""
return (function () {
  const widget = document.querySelector('cap-widget');
  if (!widget) return false;
  window.__sub2apiCapToken = '';
  if (!widget.__sub2apiNativeListener) {
    widget.addEventListener('solve', (event) => {
      window.__sub2apiCapToken = String(event.detail?.token || '');
    });
    widget.__sub2apiNativeListener = true;
  }
  const trigger = widget.shadowRoot?.querySelector('.captcha-trigger');
  if (!trigger) return false;
  trigger.removeAttribute('disabled');
  trigger.click();
  return true;
})();
""",
    )
    if not armed:
        raise Sub2apiFlowError("Cap widget 未就绪", failure_type=FAIL_TURNSTILE)
    try:
        if raw_page is not None:
            raw_page.wait_for_function(
                "() => (window.__sub2apiCapToken || '').length >= 16",
                timeout=70_000,
            )
        token = _js(p, "return String(window.__sub2apiCapToken || '');")
    except Exception as exc:
        raise Sub2apiFlowError(f"Cap 验证未完成: {str(exc)[:180]}", failure_type=FAIL_TURNSTILE) from exc
    token = str(token or "").strip()
    if len(token) < 16:
        raise Sub2apiFlowError("Cap 返回了无效 token", failure_type=FAIL_TURNSTILE)
    _log(log_callback, f"[*] Cap 注册验证通过，token 长度={len(token)}")
    return token


def _ctai_send_verify_code(p, *, origin: str, email: str, aff_code: str = "") -> None:
    """CTAI 独有的两阶段注册：先在浏览器上下文发送邮箱验证码。

    CTAI 注册页不会提交表单，而是把数据放入 sessionStorage 后进入
    ``/email-verify``。直接调用同源 API 保留 Camoufox cookies、代理和浏览器
    指纹，随后仍由真实验证页完成最终注册。
    """
    # CTAI's public client sends only the email at this stage; referral data is
    # submitted with the final /auth/register request from the verify page.
    payload = {"email": email}
    response = _js(
        p,
        r"""
return (async function (arg_origin, arg_payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const res = await fetch(arg_origin + '/auth/send-verify-code', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(arg_payload), signal: controller.signal
    });
    let body = null;
    try { body = await res.json(); } catch (_) {}
    return {status: res.status, body: body};
  } catch (error) { return {status: 0, error: String(error)}; }
  finally { clearTimeout(timer); }
})(arguments[0], arguments[1]);
""",
        origin.rstrip("/"),
        payload,
    )
    status = int(response.get("status") or 0) if isinstance(response, dict) else 0
    if status < 200 or status >= 300:
        signal = classify_registration_failure(response)
        if signal is not None and signal.kind == FAIL_ALREADY_REGISTERED:
            raise Sub2apiFlowError(
                "目标站点确认该完整邮箱地址已注册",
                failure_type=FAIL_ALREADY_REGISTERED,
            )
        detail = ""
        if isinstance(response, dict):
            body = response.get("body")
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("detail") or body.get("code") or "")
            detail = detail or str(response.get("error") or "")
        raise Sub2apiFlowError(
            f"CTAI 发送验证码失败（HTTP {status}）{(': ' + detail[:160]) if detail else ''}",
            failure_type=FAIL_PAGE if status == 0 else FAIL_FORM,
        )


# ---------------------------------------------------------------------------
# 提交与消费边界
# ---------------------------------------------------------------------------

def _classify_post_submit_url(url: str, origin: str) -> str:
    """提交后的 URL 归类：dashboard / email-verify / register / unknown。"""
    text = str(url or "")
    lowered = text.lower()
    if not lowered.startswith("http"):
        return "unknown"
    path = urlsplit(text).path.rstrip("/").lower()
    if "/dashboard" in lowered or path.endswith("/dashboard") or path == "/app":
        return "dashboard"
    if "/email-verify" in lowered or "/verify" in path or "/check-email" in lowered:
        return "email-verify"
    if origin and origin.lower() in lowered:
        # 仍停在站点注册/登录页
        if "/register" in lowered or "/signup" in lowered or "/sign-up" in lowered:
            return "register"
        if path == "" or path == "/":
            return "homepage"
    return "unknown"


@dataclass
class Sub2apiAttemptResult:
    email: str = ""
    password: str = ""
    status: str = "failure"
    failure_type: str = ""
    failure_reason: str = ""
    final_url: str = ""
    consumed: bool = False
    screenshot_path: str = ""
    diagnostics: dict = field(default_factory=dict)


def _capture_diagnostics(p, note: str) -> dict:
    try:
        snapshot = _visible_inputs(p)
    except Exception:
        snapshot = {"error": "snapshot_failed"}
    return {
        "url": str(p.url or "") if p is not None else "",
        "title": str(p.title or "") if p is not None else "",
        "note": note,
        "snapshot": snapshot,
    }


def _take_failure_screenshot(p, batch_id: str, worker_id: int, email: str, failure_type: str):
    try:
        path = _engine.capture_failure_screenshot(
            batch_id=batch_id,
            worker_id=worker_id,
            email=email,
            failure_type=failure_type,
            log_callback=None,
        )
        return str(path or "")
    except Exception:
        return ""


def run_sub2api_registration(
    profile: dict,
    *,
    batch_id: str,
    worker_id: int = 0,
    log_callback=None,
    cancel_callback=None,
    acquire=None,
) -> Sub2apiAttemptResult:
    """单次 Sub2API 注册 attempt（邮箱获取 → 表单 → Turnstile → 提交边界 → 验证码）。"""
    profile = dict(profile or {})
    p = _require_page()
    result = Sub2apiAttemptResult()
    start_at = time.time()

    # 1) 邮箱获取（白名单池内过滤 + profile 消费判定 + 冻结）
    if acquire is None:
        email, _token = _engine.acquire_email(profile, log_callback=log_callback)
    else:
        email, _token = acquire(profile)
    result.email = email
    password = generate_password()
    result.password = password
    _log(log_callback, f"[*] Sub2API 获取邮箱: {email}")
    response_monitor = RegistrationResponseMonitor(p)

    try:
        return _run_sub2api_attempt(
            profile=profile,
            p=p,
            result=result,
            email=email,
            password=password,
            batch_id=batch_id,
            worker_id=worker_id,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            response_monitor=response_monitor,
        )
    except Sub2apiFlowError as exc:
        if exc.failure_type == FAIL_ALREADY_REGISTERED:
            return _already_registered_result(
                p, result, batch_id, worker_id, email, log_callback
            )
        result.failure_type = exc.failure_type
        result.failure_reason = str(exc)
        result.diagnostics = _capture_diagnostics(p, exc.failure_type)
        result.screenshot_path = _take_failure_screenshot(
            p, batch_id, worker_id, email, exc.failure_type
        )
        return result
    except Exception as exc:
        # 附带完整 attempt 身份（fix4）：邮箱/密码/消费边界/最终 URL，
        # 使 runner 在意外异常路径达到与正常返回路径同等的凭据 durability。
        try:
            setattr(exc, "sub2api_email", result.email)
            setattr(exc, "sub2api_password", result.password)
            setattr(exc, "sub2api_consumed", bool(result.consumed))
            setattr(exc, "sub2api_final_url", str(result.final_url or ""))
        except Exception:
            pass
        raise
    finally:
        response_monitor.close()


def _run_sub2api_attempt(
    *,
    profile: dict,
    p,
    result: Sub2apiAttemptResult,
    email: str,
    password: str,
    batch_id: str,
    worker_id: int,
    log_callback=None,
    cancel_callback=None,
    response_monitor: Optional[RegistrationResponseMonitor] = None,
) -> Sub2apiAttemptResult:
    origin = str(profile.get("register_origin") or "")
    ctai_two_phase = str(profile.get("site_key") or "").strip().lower() == "ctai"
    verification_requested_at = 0.0
    # 2) 打开注册页（内部 aff_code 映射为站点公开的 aff 参数）
    url = build_registration_url(profile)
    p.get(url)
    p.wait.doc_loaded()
    _sleep(0.5, cancel_callback)
    _raise_if_cancelled(cancel_callback)

    # 3) 填写表单；CTAI 使用独立的发送验证码协议。
    if ctai_two_phase:
        verification_requested_at = time.time()
        _ctai_send_verify_code(
            p, origin=origin or urlunsplit(urlsplit(url)._replace(path="", query="", fragment="")),
            email=email,
            aff_code=str(profile.get("aff_code") or "").strip(),
        )
        _js(
            p,
            "sessionStorage.setItem('register_data', JSON.stringify(arguments[0]));",
            {
                "email": email,
                "password": password,
                "aff_code": str(profile.get("aff_code") or "").strip() or None,
            },
        )
        p.get((origin or urlunsplit(urlsplit(url)._replace(path="", query="", fragment=""))).rstrip("/") + "/email-verify")
        p.wait.doc_loaded()
    else:
        fill_registration_form(
            p, profile, email, password,
            log_callback=log_callback, cancel_callback=cancel_callback,
        )

    # 4) Turnstile（存在 → 现有 solver；不存在 → 跳过）
    _ensure_turnstile(p, log_callback=log_callback, cancel_callback=cancel_callback)
    _ensure_cap_widget(p, log_callback=log_callback, cancel_callback=cancel_callback)

    # 5) 提交 + accepted-submit 边界
    origin = str(profile.get("register_origin") or "")
    server_accept_at = 0.0
    boundary_reached = False
    submit_selector = ""
    if ctai_two_phase and _classify_post_submit_url(str(p.url or ""), origin) == "email-verify":
        server_accept_at = verification_requested_at or time.time()
        boundary_reached = True
    else:
        submit_selector = _find_submit_button(p) or ""
        if not submit_selector:
            diag = _capture_diagnostics(p, "no_submit_button")
            result.failure_type = FAIL_FORM
            result.failure_reason = "未找到可用的提交按钮"
            result.diagnostics = diag
            result.screenshot_path = _take_failure_screenshot(p, batch_id, worker_id, email, FAIL_FORM)
            return result

    deadline = time.time() + SUBMIT_NAV_TIMEOUT
    clicked = False
    while not boundary_reached and time.time() < deadline:
        _raise_if_cancelled(cancel_callback)
        current = str(p.url or "")
        kind = _classify_post_submit_url(current, origin)
        if kind in ("register", "homepage", "unknown"):
            response_signal = (
                response_monitor.latest_signal() if response_monitor is not None else None
            )
            if clicked and (
                (response_signal and response_signal.kind == FAIL_ALREADY_REGISTERED)
                or _page_reports_already_registered(p)
            ):
                return _already_registered_result(
                    p, result, batch_id, worker_id, email, log_callback
                )
            if _js(p, "return document.readyState") != "complete":
                _sleep(1.0, cancel_callback)
                continue
            if not clicked:
                _log(log_callback, "[*] 点击提交按钮")
                click_started_at = time.time()
                clicked = _click_button(p, submit_selector)
                if clicked and verification_requested_at <= 0:
                    # Mail may be emitted before navigation to /email-verify is
                    # observable. Use the action that requested it, not the
                    # later page observation, as the freshness boundary.
                    verification_requested_at = click_started_at
                if not clicked:
                    refreshed = _find_submit_button(p)
                    if refreshed:
                        submit_selector = refreshed
                    _log(log_callback, "[Debug] 提交按钮点击未生效，等待后重试")
                _sleep(1.5, cancel_callback)
                continue
            # 已点击仍停留：按钮 disabled / CF 挑战中 / 校验未过 → 等待后重判，
            # 绝不提前消费。
            buttons = _button_snapshot(p)
            disabled = [b for b in buttons if b.get("disabled")]
            _log(
                log_callback,
                f"[Debug] 提交后仍停在 {kind}（buttons={len(buttons)}, disabled={len(disabled)}），继续等待",
            )
            _sleep(2.0, cancel_callback)
            continue
        if kind == "email-verify":
            # 通用站点在进入验证页时已接受注册；CTAI 仍需验证码后才注册。
            server_accept_at = verification_requested_at or time.time()
            if not ctai_two_phase:
                result.consumed = True
                try:
                    _engine.mark_mailbox_consumed(
                        email,
                        batch_id=batch_id,
                        reason="已提交 Sub2API 注册（/email-verify）",
                        log_callback=log_callback,
                    )
                except Exception as exc:
                    raise LedgerWriteError(str(exc), email=email) from exc
            boundary_reached = True
            break
        if kind == "dashboard":
            server_accept_at = time.time()
            result.consumed = True
            try:
                _engine.mark_mailbox_consumed(
                    email,
                    batch_id=batch_id,
                    reason="已提交 Sub2API 注册（直达 /dashboard）",
                    log_callback=log_callback,
                )
            except Exception as exc:
                raise LedgerWriteError(str(exc), email=email) from exc
            result.status = "success"
            result.final_url = current
            result.diagnostics = _capture_diagnostics(p, "dashboard_without_verification")
            _log(log_callback, f"[+] Sub2API 注册成功（免验证码直达 dashboard）: {email}")
            return result
        _sleep(1.0, cancel_callback)

    if not boundary_reached:
        # 超时未到达消费边界 → 未消费（邮箱可释放），失败 + 诊断
        diag = _capture_diagnostics(p, "post_submit_timeout")
        result.failure_type = FAIL_PAGE
        result.failure_reason = (
            f"提交后 {SUBMIT_NAV_TIMEOUT:.0f}s 未到达 /email-verify 或 /dashboard"
            f"（final_url={diag.get('url') or 'empty'}）"
        )
        result.diagnostics = diag
        result.screenshot_path = _take_failure_screenshot(p, batch_id, worker_id, email, FAIL_PAGE)
        return result

    # 6) 验证码：使用 acquire 冻结的 mailbox_source + min_received_at
    code_deadline = time.time() + CODE_POLL_TIMEOUT
    code = ""
    last_error = ""
    while time.time() < code_deadline:
        _raise_if_cancelled(cancel_callback)
        try:
            code = _engine.outlookemail_get_oai_code(
                email,
                timeout=max(5.0, CODE_POLL_INTERVAL * 3),
                poll_interval=CODE_POLL_INTERVAL,
                min_received_at=server_accept_at,
                # acquire 时冻结的来源（accounts/temp）：运行中改 Settings 不影响本 attempt。
                source=_engine.frozen_mailbox_source(email),
            )
            break
        except _engine.RegistrationCancelled:
            raise
        except Exception as exc:
            last_error = str(exc)
            _log(log_callback, f"[Debug] 验证码轮询未完成: {last_error[:120]}")
            _sleep(CODE_POLL_INTERVAL, cancel_callback)
    if not code:
        # 已消费：保持 consumed（邮箱已提交），失败类型 code_timeout
        diag = _capture_diagnostics(p, "code_timeout")
        result.failure_type = FAIL_CODE
        result.failure_reason = (
            f"等待邮箱验证码超时（总轮询窗口 {CODE_POLL_TIMEOUT:.0f}s）"
            f"；最后一次轮询: {last_error[:200]}"
        )
        result.diagnostics = diag
        result.screenshot_path = _take_failure_screenshot(p, batch_id, worker_id, email, FAIL_CODE)
        return result

    # 7) 填入验证码并提交
    code_field = _find_input(
        p,
        ids=("code",),
        names=("code", "otp", "verification_code"),
        types=("text", "tel", "number"),
        autocompletes=("one-time-code",),
        placeholders=("code", "otp", "验证码"),
        marker="verification-code",
    )
    if code_field is None:
        diag = _capture_diagnostics(p, "code_field_missing")
        result.failure_type = FAIL_FORM
        result.failure_reason = "验证码页面缺少 code 输入框"
        result.diagnostics = diag
        result.screenshot_path = _take_failure_screenshot(p, batch_id, worker_id, email, FAIL_FORM)
        return result
    selector = str(code_field.get("selector") or "")
    if not selector:
        result.failure_type = FAIL_FORM
        result.failure_reason = "验证码输入框定位结果无可用 selector"
        result.diagnostics = _capture_diagnostics(p, "code_field_selector_missing")
        result.screenshot_path = _take_failure_screenshot(
            p, batch_id, worker_id, email, FAIL_FORM
        )
        return result
    _set_value(p, selector, str(code))
    _sleep(0.5, cancel_callback)
    code_submit = _find_submit_button(p) or submit_selector
    _click_button(p, code_submit)
    _sleep(2.0, cancel_callback)

    # 8) 成功判定：必须到达 dashboard/登录态证据；绝不因离开 /email-verify 就盲目记成功
    verify_deadline = time.time() + SUBMIT_NAV_TIMEOUT
    final_url = ""
    while time.time() < verify_deadline:
        _raise_if_cancelled(cancel_callback)
        final_url = str(p.url or "")
        kind = _classify_post_submit_url(final_url, origin)
        if kind == "dashboard":
            if ctai_two_phase and not result.consumed:
                result.consumed = True
                try:
                    _engine.mark_mailbox_consumed(
                        email,
                        batch_id=batch_id,
                        reason="CTAI 验证码注册成功（dashboard）",
                        log_callback=log_callback,
                    )
                except Exception as exc:
                    raise LedgerWriteError(str(exc), email=email) from exc
            result.status = "success"
            result.final_url = final_url
            result.diagnostics = _capture_diagnostics(p, "dashboard_after_code")
            _log(log_callback, f"[+] Sub2API 注册成功: {email}（dashboard 证据确认）")
            return result
        response_signal = (
            response_monitor.latest_signal() if response_monitor is not None else None
        )
        if (
            (response_signal and response_signal.kind == FAIL_ALREADY_REGISTERED)
            or _page_reports_already_registered(p)
        ):
            return _already_registered_result(
                p, result, batch_id, worker_id, email, log_callback
            )
        if kind == "register" or kind == "email-verify":
            _sleep(1.0, cancel_callback)
            continue
        _sleep(1.0, cancel_callback)
    # 无明确 dashboard 证据 → 失败 + 截图 + URL + DOM 诊断
    diag = _capture_diagnostics(p, "no_dashboard_evidence")
    result.failure_type = FAIL_PAGE
    result.failure_reason = (
        f"验证码提交后未获得 dashboard 登录态证据（final_url={diag.get('url') or 'empty'}）"
    )
    result.diagnostics = diag
    result.screenshot_path = _take_failure_screenshot(p, batch_id, worker_id, email, FAIL_PAGE)
    return result
