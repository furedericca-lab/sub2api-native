# -*- coding: utf-8 -*-
"""Cloudflare Turnstile token 获取（直接点击 + 轮询等待）。

与站点注册共用同一 Camoufox 浏览器运行时（backend.automation.session），
不引入第二套验证码运行时。复用当前已验证的 solver，原样
迁移：iframe 内无 checkbox DOM 元素（canvas/overlay 渲染），managed 模式
不会自动通过，因此通过 raw_page.frames 定位 frame 并坐标点击，再轮询等待
token 出现；未通过则间隔重试点击。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from backend.automation.session import (
    active_page,
    arm_browser_watchdog,
    current_profile_dir,
    page,
)
from backend.registration.runtime import raise_if_cancelled, sleep_with_cancel


def _poll_turnstile_token(
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    force_reset: bool = False,
    deadline: Optional[float] = None,
    budget_seconds: float = 0.0,
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
    # 上游可能把托管模式降级成看得见的人工挑战框（300x65）并反复重建 iframe，
    # 点击和页面 JS 读取都会因此变慢甚至卡住，所以硬上限是挂钟预算而不是轮数。
    expired = False

    # 进度看门狗：浏览器进程自己卡住时（实测有头 Camoufox 在无显示主机上会
    # 出现 GPU 进程失败），带 timeout 的 Playwright 调用同样会阻塞到进程被回收，
    # 调用级超时救不回来。所以这里只看“还有没有成功的轮次”：读到响应就续期，
    # 连续十几秒读不到就把本会话浏览器回收，把操作员最坏等待从“预算+5s”压下来。
    progress_timer: Optional[Callable[[], None]] = None
    progress_rearm_at = 0.0
    progress_profile_dir = current_profile_dir() if budget_seconds and budget_seconds > 0 else ""

    def _rearm_progress_watchdog() -> None:
        nonlocal progress_timer, progress_rearm_at
        now = time.monotonic()
        if not progress_profile_dir or now < progress_rearm_at:
            return
        progress_rearm_at = now + 5.0
        if progress_timer is not None:
            progress_timer()
        progress_timer = arm_browser_watchdog(15.0, progress_profile_dir, log_callback)

    def _stop_progress_watchdog() -> None:
        nonlocal progress_timer
        if progress_timer is not None:
            progress_timer()
            progress_timer = None

    # 第一轮读取前先挂上，否则“启动完就卡住”这种最常见的形态反而没有进度定时器。
    _rearm_progress_watchdog()

    for _ in range(0, TOTAL_ROUNDS):
        raise_if_cancelled(cancel_callback)
        if deadline is not None and time.monotonic() >= deadline:
            expired = True
            break
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
            # 读到响应（哪怕是空串）就说明页面 JS 还活着，续期即可。
            _rearm_progress_watchdog()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                _stop_progress_watchdog()
                return token

            # 直接点击（首次或间隔重试）；挑战框重建后很快可以再点，不必等 4 轮
            if not click_attempted or (_ - last_click_round >= 2):
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

    _stop_progress_watchdog()
    if expired:
        reason = (
            f"Turnstile 在 {budget_seconds:.0f}s 预算内未通过，"
            "上游保持人工交互挑战（常见于当前出口 IP 风险评分较高，或挑战页反复重建）"
        )
        if log_callback:
            log_callback(f"[!] {reason}")
        raise Exception(reason)
    raise Exception("Turnstile 获取 token 失败")


def get_turnstile_token(
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    force_reset: bool = False,
    budget_seconds: Optional[float] = None,
    arm_watchdog: bool = True,
) -> str:
    """获取 Turnstile token，带挂钟预算与卡死看门狗。

    Playwright 的 evaluate / frame_element / bounding_box 没有超时，上游挑战页
    反复重建 iframe 时这些调用会永久阻塞，轮顶的协作式取消也就轮不到。预算
    到期后看门狗强制回收本会话浏览器，阻塞中的调用随即报错，任务才能终止。
    """
    budget = float(budget_seconds) if budget_seconds and float(budget_seconds) > 0 else 0.0
    if budget <= 0:
        return _poll_turnstile_token(
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            force_reset=force_reset,
        )
    deadline = time.monotonic() + budget
    disarm = (
        arm_browser_watchdog(budget + 5.0, current_profile_dir(), log_callback)
        if arm_watchdog
        else None
    )
    try:
        return _poll_turnstile_token(
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            force_reset=force_reset,
            deadline=deadline,
            budget_seconds=budget,
        )
    finally:
        if disarm is not None:
            disarm()


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
            timeout=2000,
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
                    timeout=2000,
                )
                if log_callback:
                    log_callback(
                        f"[*] 已点击 Turnstile iframe element (24, {box['height'] / 2:.0f})"
                    )
                return
    except Exception as element_click_exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile iframe element 点击失败: {element_click_exc}")

    # ---- 策略 3：frame 坐标的 page 级点击（同样必须带超时）----
    # 旧版本用 raw_page.mouse.click，那个 API 没有超时：上游给出人工交互挑战时
    # 渲染主线程是卡的，它会挂 40+ 秒再把整个有界预算吞掉，还要靠看门狗杀浏览器
    # 才能打断（浏览器一死，现场截图也拍不到）。今天所有成功日志都来自策略 1/2，
    # 这条只贡献过挂死，因而不再用无限等待的 API。
    try:
        iframe_el = turnstile_frame.frame_element()
        box = iframe_el.bounding_box() if iframe_el else None
        if box and box["width"] > 0:
            px = box["x"] + 24
            py = box["y"] + box["height"] / 2
            raw_page.locator("body").click(
                position={"x": px, "y": py},
                force=True,
                timeout=2000,
            )
            if log_callback:
                log_callback(f"[*] 已在 page 级点击 Turnstile iframe ({px:.0f}, {py:.0f})")
            return
        if log_callback:
            # 挑战 iframe 正在重建时拿不到坐标，过去这里会静默返回，日志上看起来
            # 像“点了但没用”。
            log_callback("[Debug] Turnstile iframe 暂无尺寸（挑战页重建中），本轮不点击")
    except Exception as page_click_exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile page 级点击失败: {str(page_click_exc)[:120]}")
