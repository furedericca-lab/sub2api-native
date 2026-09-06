"""Camoufox-backed captcha solver shared by Sub2API integrations."""
from __future__ import annotations

import html
import time
from typing import Any, Callable, Dict, Optional

from backend.automation import session as browser_session
from backend.automation.turnstile import get_turnstile_token
from backend.registration.runtime import RegistrationCancelled, raise_if_cancelled

from .sub2api_transport import require_http_url


class CaptchaError(RuntimeError):
    pass


class CamoufoxCaptchaSolver:
    def __init__(
        self,
        *,
        attempts: int = 3,
        retry_delay: float = 10.0,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_callback: Optional[Callable[[], bool]] = None,
        deadline_callback: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.log_callback = log_callback
        self.cancel_callback = cancel_callback
        self.deadline_callback = deadline_callback
        self._page = None

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)

    def _ensure_page(self):
        if self._page is None:
            _, self._page = browser_session.start_browser(
                log_callback=self.log_callback,
                geoip_override=False,
            )
        return self._page

    def close(self) -> None:
        browser_session.stop_browser(force=True)
        self._page = None

    def solve(self, provider: str, settings: Dict[str, Any], page_url: str) -> Optional[str]:
        normalized = str(provider or "").strip().lower()
        if normalized in {"", "none"}:
            return None
        if normalized == "cap":
            return self._solve_cap(settings)
        if normalized == "turnstile":
            return self._solve_turnstile(settings, page_url)
        raise CaptchaError(f"站点使用了不支持的验证码类型: {normalized or 'unknown'}")

    def _attempt_budget(self) -> Optional[float]:
        """本次验证码尝试（启动浏览器 + 渲染挑战页 + 等 token）的挂钟预算。

        没有时限（例如注册主流程）时返回 None，行为与以往一致；有时限时最多
        占用 180s，并给登录与密钥同步留出收尾时间。
        """
        if self.deadline_callback is None:
            return None
        try:
            remaining = float(self.deadline_callback() or 0.0)
        except Exception:
            return None
        if remaining <= 12.0:
            raise CaptchaError("账户任务剩余时间不足，已停止自动登录")
        return min(180.0, remaining - 8.0)

    @staticmethod
    def _poll_budget(attempt_deadline: Optional[float]) -> Optional[float]:
        """渲染完成后，留给轮询等 token 的预算。"""
        if attempt_deadline is None:
            return None
        return max(5.0, min(60.0, attempt_deadline - time.monotonic() - 3.0))

    def _solve_cap(self, settings: Dict[str, Any]) -> str:
        try:
            endpoint = require_http_url(settings.get("cap_endpoint"), "Cap endpoint").rstrip("/") + "/"
            asset_base = require_http_url(
                settings.get("cap_asset_url") or "https://cdn.jsdelivr.net/npm/@cap.js/widget",
                "Cap asset URL",
            ).rstrip("/")
        except ValueError as exc:
            raise CaptchaError(str(exc)) from exc
        asset_module = asset_base if asset_base.endswith((".js", "/+esm")) else asset_base + "/+esm"
        page = self._ensure_page().raw_page
        page_html = f"""<!doctype html><html><body
          data-endpoint="{html.escape(endpoint, quote=True)}"
          data-asset-url="{html.escape(asset_module, quote=True)}"
          data-cap-state="pending">
          <main id="challenge"></main>
          <script type="module">
            const body = document.body;
            try {{
              await import(body.dataset.assetUrl);
              await customElements.whenDefined('cap-widget');
              const widget = document.createElement('cap-widget');
              widget.setAttribute('data-cap-api-endpoint', body.dataset.endpoint);
              widget.setAttribute('data-cap-disable-haptics', '');
              document.querySelector('#challenge').replaceChildren(widget);
              widget.addEventListener('solve', (event) => {{
                body.dataset.capToken = String(event.detail?.token || '');
                body.dataset.capState = 'done';
              }}, {{ once: true }});
              widget.addEventListener('error', (event) => {{
                body.dataset.capError = String(event.detail?.message || event.detail?.code || 'Cap error');
                body.dataset.capState = 'error';
              }}, {{ once: true }});
              const trigger = widget.shadowRoot?.querySelector('.captcha-trigger');
              if (!trigger) throw new Error('Cap widget trigger unavailable');
              trigger.removeAttribute('disabled');
              trigger.click();
            }} catch (error) {{
              body.dataset.capError = String((error && error.message) || error);
              body.dataset.capState = 'error';
            }}
          </script>
        </body></html>"""
        last_error = "Cap 未返回 token"
        for attempt in range(1, self.attempts + 1):
            try:
                page.set_content(page_html, wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => ['done', 'error'].includes(document.body.dataset.capState)",
                    timeout=70_000,
                )
                result = page.evaluate(
                    """() => ({token: document.body.dataset.capToken || '', error: document.body.dataset.capError || ''})"""
                )
                token = str(result.get("token") if isinstance(result, dict) else "").strip()
                if len(token) >= 16:
                    return token
                if token:
                    last_error = "Cap 返回了无效的短 token"
                detail = str(result.get("error") if isinstance(result, dict) else result or "").strip()
                last_error = detail or last_error
                if "instr_blocked" in last_error.lower() or "automated_browser" in last_error.lower():
                    break
            except Exception as exc:
                last_error = str(exc)[:300] or last_error
            # 取消统一上抛 RegistrationCancelled，由调用方判定它是失败还是取消。
            raise_if_cancelled(self.cancel_callback)
            if attempt < self.attempts:
                self._log(f"[*] Cap 第 {attempt} 次未完成，等待后重试")
                time.sleep(self.retry_delay)
        raise CaptchaError(f"Cap 验证未完成: {last_error}")

    def _solve_turnstile(self, settings: Dict[str, Any], page_url: str) -> str:
        try:
            return self._solve_turnstile_page(settings, page_url)
        except (CaptchaError, RegistrationCancelled):
            # 取消不是验证码失败，必须原样上抛给调用方判定结论。
            raise
        except Exception as exc:
            raise CaptchaError(f"Turnstile 验证未完成: {str(exc)[:300]}") from exc

    def _solve_turnstile_page(self, settings: Dict[str, Any], page_url: str) -> str:
        site_key = str(settings.get("captcha_site_key") or settings.get("turnstile_site_key") or "").strip()
        if not site_key:
            raise CaptchaError("Turnstile site key 缺失")
        try:
            target_url = require_http_url(page_url, "Turnstile page URL")
        except ValueError as exc:
            raise CaptchaError(str(exc)) from exc
        raise_if_cancelled(self.cancel_callback)
        # 预算必须在启动浏览器前就算出来：冷启动可拖到分钟级，等到要 token 时
        # 再看剩余时间已经保不住任务时限。
        budget = self._attempt_budget()
        attempt_deadline = time.monotonic() + budget if budget else None
        raw_page = self._ensure_page().raw_page
        if attempt_deadline is not None and time.monotonic() >= attempt_deadline:
            raise CaptchaError("验证码阶段超过任务时限，已停止自动登录（可重试）")
        disarm = None
        if attempt_deadline is not None:
            disarm = browser_session.arm_browser_watchdog(
                max(1.0, attempt_deadline - time.monotonic()) + 5.0,
                browser_session.current_profile_dir(),
                self.log_callback,
            )
        try:
            return self._render_and_wait_token(
                raw_page, target_url, site_key, settings, attempt_deadline
            )
        except BaseException as exc:
            if attempt_deadline is not None and time.monotonic() >= attempt_deadline:
                raise CaptchaError(
                    "验证码阶段超过任务时限，已停止自动登录（可重试）"
                ) from exc
            raise
        finally:
            if disarm is not None:
                disarm()

    @staticmethod
    def _challenge_html(site_key: str, action: str, cdata: str) -> str:
        return f"""<!doctype html><html><body
          data-site-key="{html.escape(site_key, quote=True)}"
          data-action="{html.escape(action, quote=True)}"
          data-cdata="{html.escape(cdata, quote=True)}">
          <div id="cf-challenge"></div>
          <script>
            function renderTurnstile() {{
              const body = document.body;
              const options = {{ sitekey: body.dataset.siteKey }};
              if (body.dataset.action) options.action = body.dataset.action;
              if (body.dataset.cdata) options.cData = body.dataset.cdata;
              window.turnstile.render('#cf-challenge', options);
            }}
          </script>
          <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" onload="renderTurnstile()" async defer></script>
        </body></html>"""

    def _render_and_wait_token(
        self,
        raw_page: Any,
        target_url: str,
        site_key: str,
        settings: Dict[str, Any],
        attempt_deadline: Optional[float],
    ) -> str:
        action = str(settings.get("captcha_action") or "").strip()
        cdata = str(settings.get("captcha_cdata") or "").strip()
        challenge_html = self._challenge_html(site_key, action, cdata)

        def fulfill_challenge(route) -> None:
            route.fulfill(status=200, content_type="text/html", body=challenge_html)

        raw_page.route(target_url, fulfill_challenge)
        try:
            raw_page.goto(target_url, wait_until="domcontentloaded")
        finally:
            raw_page.unroute(target_url, fulfill_challenge)
        raw_page.wait_for_selector(
            'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]',
            state="attached",
            timeout=30_000,
        )
        try:
            return get_turnstile_token(
                log_callback=self.log_callback,
                cancel_callback=self.cancel_callback,
                budget_seconds=self._poll_budget(attempt_deadline),
                arm_watchdog=False,
            )
        except RegistrationCancelled:
            # 调用方停止不是验证码失败，原样上抛才能被归为 cancelled。
            raise
        except Exception as exc:
            raise CaptchaError(f"Turnstile 验证未完成: {str(exc)[:300]}") from exc
