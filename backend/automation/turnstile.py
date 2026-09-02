# -*- coding: utf-8 -*-
"""Cloudflare Turnstile token 获取（直接点击 + 轮询等待）。

与站点注册共用同一 Camoufox 浏览器运行时（backend.automation.session），
不引入第二套验证码运行时。复用当前已验证的 solver，原样
迁移：iframe 内无 checkbox DOM 元素（canvas/overlay 渲染），managed 模式
不会自动通过，因此通过 raw_page.frames 定位 frame 并坐标点击，再轮询等待
token 出现；未通过则间隔重试点击。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.automation.session import active_page, page
from backend.registration.runtime import raise_if_cancelled, sleep_with_cancel


def get_turnstile_token(
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    force_reset: bool = False,
) -> str:
    """获取 Turnstile token（直接点击 + 轮询等待）。

    Turnstile iframe 内无 checkbox DOM 元素（canvas/overlay 渲染），
    managed 模式不会自动通过，所以：
    1. 直接通过 raw_page.frames 定位 frame 并坐标点击
    2. 轮询等待 token 出现
    3. 未通过则间隔重试点击
    """
    if active_page() is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    click_attempted = False
    last_click_round = -100
    TOTAL_ROUNDS = 20
    POLL_INTERVAL = 2.0

    for _ in range(0, TOTAL_ROUNDS):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            # 直接点击（首次或间隔重试）
            if not click_attempted or (_ - last_click_round >= 4):
                if not click_attempted:
                    if log_callback:
                        log_callback("[*] 尝试点击 Turnstile...")
                else:
                    if log_callback:
                        log_callback("[*] 再次尝试点击 Turnstile...")
                _try_click_turnstile_frame(log_callback=log_callback)
                click_attempted = True
                last_click_round = _
                sleep_with_cancel(3.0, cancel_callback)
                continue
        except Exception:
            pass
        sleep_with_cancel(POLL_INTERVAL, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def _try_click_turnstile_frame(log_callback: Optional[Callable[[str], None]] = None) -> None:
    """通过 Playwright frame API 点击 Turnstile checkbox。

    全链路诊断日志 + 多策略点击：
    1. 遍历 frames 找到 Turnstile frame（日志输出找到/未找到 + frame URL）
    2. 在 frame 内搜索 checkbox 元素（日志输出尝试了哪些选择器）
    3. 找到则点击；未找到则走 body 坐标点击 fallback
    4. frame 内点击失败则尝试 page 级 iframe 坐标点击
    """
    try:
        raw_page: Any = page.raw_page
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile 点击失败：无法获取 raw_page: {exc}")
        return

    # ---- 遍历 Playwright frames 找到 Turnstile frame ----
    turnstile_frame = None
    all_frame_urls = []
    for frame in raw_page.frames:
        frame_url = str(frame.url or "")
        all_frame_urls.append(frame_url[:80])
        if "challenges.cloudflare.com" in frame_url or "turnstile" in frame_url.lower():
            turnstile_frame = frame
            break

    if not turnstile_frame:
        if log_callback:
            log_callback(
                f"[Debug] Turnstile frame 未找到。当前 frames({len(all_frame_urls)}): "
                f"{all_frame_urls}"
            )
        return

    frame_url = str(turnstile_frame.url or "")
    if log_callback:
        log_callback(f"[Debug] Turnstile frame 已定位: {frame_url[:100]}")

    # ---- 策略 1：frame body 强制坐标点击 ----
    # Turnstile 的交互层可能没有可定位的 checkbox，且空 body 会被
    # Playwright 的 actionability 检查判为不可点击，因此必须使用 force。
    try:
        body_info = turnstile_frame.evaluate(
            """
() => {
  const b = document.body;
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return { w: r.width, h: r.height };
}
            """
        )
        if log_callback:
            bi = body_info or {}
            log_callback(
                f"[Debug] Turnstile frame body: w={bi.get('w', 0):.0f} h={bi.get('h', 0):.0f}"
            )

        if not body_info or body_info.get("w", 0) <= 0:
            if log_callback:
                log_callback("[Debug] Turnstile frame body 未渲染好，跳过")
            return

        click_x = 24
        click_y = body_info["h"] / 2
        turnstile_frame.locator("body").click(
            position={"x": click_x, "y": click_y},
            force=True,
            timeout=3000,
        )
        if log_callback:
            log_callback(f"[*] 已点击 Turnstile frame body ({click_x}, {click_y:.0f})")
        return
    except Exception as frame_click_exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile frame body 点击失败: {frame_click_exc}")

    # ---- 策略 2：直接点击 frame element ----
    # 动态 Turnstile iframe 的 src 属性可能仍为空，不能依赖 page selector
    # 重新定位；Playwright frame_element() 保留了已定位 frame 的权威关系。
    try:
        iframe_el = turnstile_frame.frame_element()
        if iframe_el:
            box = iframe_el.bounding_box()
            if box and box["width"] > 0:
                iframe_el.click(
                    position={"x": 24, "y": box["height"] / 2},
                    force=True,
                    timeout=3000,
                )
                if log_callback:
                    log_callback(
                        f"[*] 已点击 Turnstile iframe element (24, {box['height'] / 2:.0f})"
                    )
                return
    except Exception as element_click_exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile iframe element 点击失败: {element_click_exc}")

    # ---- 策略 3：page mouse 绝对坐标点击 ----
    try:
        iframe_el = turnstile_frame.frame_element()
        box = iframe_el.bounding_box() if iframe_el else None
        if box and box["width"] > 0:
            px = box["x"] + 24
            py = box["y"] + box["height"] / 2
            raw_page.mouse.click(px, py)
            if log_callback:
                log_callback(f"[*] 已在 page 级点击 Turnstile iframe ({px:.0f}, {py:.0f})")
    except Exception as page_click_exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile page 级点击失败: {page_click_exc}")
