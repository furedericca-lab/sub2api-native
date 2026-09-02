# -*- coding: utf-8 -*-
"""注册运行时公共能力：取消控制。

Sub2API 注册流程与邮箱验证码轮询共用的最小取消运行时。所有长等待都必须
可被停止信号打断；停止判定统一通过 cancel_callback（返回 True = 已请求停止）。
"""
from __future__ import annotations

import time


class RegistrationCancelled(Exception):
    """用户请求停止注册任务。"""


class RegistrationStopController:
    """Web 协调器 / 任务 runner 共用的停止控制器。"""

    def __init__(self):
        self.stop_requested = False

    def should_stop(self) -> bool:
        return self.stop_requested

    def stop(self) -> None:
        self.stop_requested = True


def raise_if_cancelled(cancel_callback=None) -> None:
    """cancel_callback 返回 True 时抛出停止异常。"""
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None) -> None:
    """可被停止打断的等待：每 0.2s 检查一次停止信号。"""
    deadline = time.time() + max(float(seconds or 0), 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
