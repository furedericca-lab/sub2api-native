# -*- coding: utf-8 -*-
"""启动前依赖检查。

集中验证代理出口与 OutlookEmail 邮箱渠道配置，返回适合控制台展示的结构化
结果。不对任意 Sub2API register_url 做严格 HTTP 成功判断：Cloudflare、
登录跳转、WAF、403 等都可能被浏览器流程正常处理，却会被 curl/preflight
误判；真正站点可注册性由 Camoufox runner 判断。
"""
from __future__ import annotations

import socket
from typing import Callable, List, Tuple
from urllib.parse import urlparse

from backend.integrations.proxy import redact_proxy_text, resolve_proxy_url
from backend.mailbox.service import (
    resolve_api_base,
    resolve_legacy_session_cookie,
    resolve_login_password,
)

CheckResult = Tuple[str, bool, str]  # name, ok, detail


def _tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _trace_exit_ip(http_get: Callable, proxies: dict) -> str:
    """请求 Cloudflare trace 端点并解析出口 IP（失败返回空串）。"""
    resp = http_get(
        "https://www.cloudflare.com/cdn-cgi/trace",
        timeout=8,
        proxies=proxies,
    )
    text = str(getattr(resp, "text", "") or "")
    ip = ""
    loc = ""
    for line in text.splitlines():
        if line.startswith("ip="):
            ip = line[3:].strip()
        elif line.startswith("loc="):
            loc = line[4:].strip()
    if ip and loc:
        return f"{ip} ({loc})"
    return ip


def check_proxy(proxy_url: str, http_get: Callable) -> CheckResult:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        # 直连也打印出口 IP，方便与走代理时对比确认代理是否生效
        try:
            direct_ip = _trace_exit_ip(http_get, {})
        except Exception:
            direct_ip = ""
        detail = "未配置（直连）"
        if direct_ip:
            detail += f"，出口IP {direct_ip}"
        return "代理", True, detail
    try:
        u = urlparse(proxy_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        if not _tcp_open(host, port):
            return "代理", False, f"无法连接 {host}:{port}"
        # 轻量探测 + 解析出口 IP，确认代理确实生效
        try:
            exit_ip = _trace_exit_ip(
                http_get, {"http": proxy_url, "https": proxy_url}
            )
        except Exception as exc:
            # TCP 通但出站失败也提示
            return "代理", False, f"TCP 通，出站探测失败: {redact_proxy_text(exc)}"
        if exit_ip:
            return "代理", True, f"{host}:{port} 可用，出口IP {exit_ip}"
        return "代理", True, f"{host}:{port} 可用（未解析到出口IP）"
    except Exception as exc:
        return "代理", False, redact_proxy_text(exc)


def check_browser_runtime() -> CheckResult:
    """浏览器运行时（Camoufox）依赖检查：可执行文件与基础库是否就绪。"""
    try:
        from camoufox.pkgman import camoufox_path, launch_path

        path = camoufox_path(download_if_missing=False)
        if not path:
            return "浏览器运行时", False, "Camoufox 未下载（请执行 camoufox fetch）"
        executable = launch_path(path)
        return "浏览器运行时", True, f"Camoufox 已就绪: {executable}"
    except Exception as exc:
        return "浏览器运行时", False, f"Camoufox 检查失败: {exc}"


def check_email_api(provider: str, config: dict, http_get: Callable, http_post: Callable) -> CheckResult:
    try:
        base = resolve_api_base(config)
        source = str(config.get("outlookemail_source", "accounts") or "accounts").strip().lower()
        if source == "temp":
            password = resolve_login_password(config)
            cookie = resolve_legacy_session_cookie(config)
            if password:
                resp = http_post(
                    f"{base}/api/extension/login",
                    json={"password": password, "next": "/"},
                    headers={"Content-Type": "application/json"},
                    timeout=12,
                    proxies={},
                )
                if resp.status_code >= 400:
                    return "邮箱API", False, f"OutlookEmail 网页登录 HTTP {resp.status_code}"
                data = resp.json()
                ok = isinstance(data, dict) and bool(data.get("success")) and bool(data.get("launch_url"))
                return "邮箱API", ok, "OutlookEmail temp 网页登录可用" if ok else "OutlookEmail 登录响应无效"
            if not cookie:
                return "邮箱API", False, "OutlookEmail temp 未取得运行时登录凭据"
            resp = http_get(
                f"{base}/api/temp-emails",
                headers={"Cookie": cookie},
                timeout=12,
                proxies={},
            )
            if resp.status_code >= 400:
                return "邮箱API", False, f"OutlookEmail temp HTTP {resp.status_code}"
            data = resp.json()
            ok = not (isinstance(data, dict) and data.get("success") is False)
            return "邮箱API", ok, f"OutlookEmail temp HTTP {resp.status_code}"

        key = str(config.get("outlookemail_api_key", "") or "").strip()
        if not key:
            return "邮箱API", False, "OutlookEmail accounts 需配置 API Key"
        params = {"limit": 1, "offset": 0, "sort_by": "created_at", "sort_order": "asc"}
        group_id = str(config.get("outlookemail_group_id", "") or "").strip()
        if group_id:
            params["group_id"] = group_id
        resp = http_get(
            f"{base}/api/external/accounts",
            headers={"X-API-Key": key},
            params=params,
            timeout=12,
            proxies={},
        )
        if resp.status_code >= 400:
            return "邮箱API", False, f"OutlookEmail accounts HTTP {resp.status_code}"
        data = resp.json()
        ok = isinstance(data, dict) and bool(data.get("success")) and isinstance(data.get("accounts"), list)
        return "邮箱API", ok, f"OutlookEmail accounts HTTP {resp.status_code}"
    except Exception:
        # Provider exceptions can include response bodies or credential-bearing
        # URLs. The connectivity panel only needs a stable failure category.
        return "邮箱API", False, "OutlookEmail 检查失败"


def run_connectivity_checks(config: dict, http_get: Callable, http_post: Callable) -> List[CheckResult]:
    results = []
    proxy = resolve_proxy_url(config.get("proxy", ""))
    results.append(check_proxy(proxy, http_get))
    results.append(check_browser_runtime())
    results.append(
        check_email_api(
            "outlookemail",
            config,
            http_get,
            http_post,
        )
    )
    return results


def format_check_results(results: List[CheckResult]) -> str:
    lines = []
    for name, ok, detail in results:
        mark = "OK" if ok else "FAIL"
        lines.append(f"[{mark}] {name}: {detail}")
    return "\n".join(lines)
