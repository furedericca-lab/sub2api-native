"""Shared Sub2API JSON transport with explicit proxy routing."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from curl_cffi import requests


class Sub2ApiApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, code: str = "") -> None:
        self.status_code = int(status_code)
        self.code = str(code or "")
        super().__init__(message)


class Sub2ApiNetworkError(RuntimeError):
    pass


def require_http_url(value: Any, label: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} 不是有效的 HTTP(S) 地址")
    return text


class Sub2ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        proxies: Optional[Dict[str, str]] = None,
        session: Any = None,
    ) -> None:
        self.base_url = require_http_url(base_url, "Sub2API origin").rstrip("/")
        self.timeout = max(float(timeout), 1.0)
        self.proxies = dict(proxies or {})
        self.session = session or requests.Session(trust_env=False)
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        token: str = "",
    ) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "sub2api-native/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                json=payload if payload is not None else None,
                headers=headers,
                timeout=self.timeout,
                proxies=self.proxies,
            )
        except Exception as exc:
            raise Sub2ApiNetworkError("上游请求超时或网络不可达") from exc

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            if int(response.status_code) >= 400:
                raise Sub2ApiApiError(
                    response.status_code, f"上游返回 HTTP {response.status_code}"
                ) from exc
            raise Sub2ApiApiError(response.status_code, "上游返回了非 JSON 响应") from exc
        if not isinstance(body, dict):
            raise Sub2ApiApiError(response.status_code, "上游响应结构无效")
        if int(response.status_code) >= 400:
            message = body.get("message") or body.get("detail") or f"HTTP {response.status_code}"
            raise Sub2ApiApiError(
                response.status_code,
                str(message)[:300],
                str(body.get("code") or ""),
            )
        data = body.get("data")
        return data if isinstance(data, dict) else body
