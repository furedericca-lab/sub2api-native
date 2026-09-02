"""Camoufox-backed captcha solver shared by Sub2API integrations."""
from __future__ import annotations

import html
import time
from typing import Any, Callable, Dict, Optional

from backend.automation import session as browser_session
from backend.automation.turnstile import get_turnstile_token

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
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.log_callback = log_callback
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
            if attempt < self.attempts:
                self._log(f"[*] Cap 第 {attempt} 次未完成，等待后重试")
                time.sleep(self.retry_delay)
        raise CaptchaError(f"Cap 验证未完成: {last_error}")

    def _solve_turnstile(self, settings: Dict[str, Any], page_url: str) -> str:
        try:
            return self._solve_turnstile_page(settings, page_url)
        except CaptchaError:
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
        raw_page = self._ensure_page().raw_page
        action = str(settings.get("captcha_action") or "").strip()
        cdata = str(settings.get("captcha_cdata") or "").strip()
        challenge_html = f"""<!doctype html><html><body
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
            return get_turnstile_token(log_callback=self.log_callback)
        except Exception as exc:
            raise CaptchaError(f"Turnstile 验证未完成: {str(exc)[:300]}") from exc
