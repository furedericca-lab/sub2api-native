"""Shared Sub2API authentication contract."""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .sub2api_transport import Sub2ApiApiError, Sub2ApiClient


@dataclass(frozen=True)
class LoginCaptchaContract:
    provider: str
    payload_field: str


def resolve_login_captcha(settings: Dict[str, Any]) -> LoginCaptchaContract:
    provider = str(settings.get("captcha_provider") or "").strip().lower()
    if provider == "cap":
        return LoginCaptchaContract("cap", "captcha_token")
    if provider == "turnstile" or bool(settings.get("turnstile_enabled")):
        return LoginCaptchaContract("turnstile", "turnstile_token")
    if provider in {"", "none"}:
        return LoginCaptchaContract("none", "")
    raise ValueError(f"站点使用了不支持的登录验证码类型: {provider}")


def _totp_code(secret: str, timestamp: Optional[int] = None) -> str:
    normalized = "".join(str(secret or "").upper().split()).replace("-", "")
    try:
        key = base64.b32decode(normalized, casefold=True)
    except Exception as exc:
        raise ValueError("TOTP 密钥格式无效") from exc
    counter = int((timestamp if timestamp is not None else time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


class Sub2ApiAuthService:
    def __init__(self, client: Sub2ApiClient, captcha_solver: Any) -> None:
        self.client = client
        self.captcha_solver = captcha_solver

    def public_settings(self) -> Dict[str, Any]:
        return self.client.request("GET", "/api/v1/settings/public")

    def login(
        self,
        email: str,
        password: str,
        settings: Dict[str, Any],
        *,
        totp_secret: str = "",
    ) -> str:
        contract = resolve_login_captcha(settings)
        captcha_token = self.captcha_solver.solve(
            contract.provider,
            settings,
            self.client.base_url + "/login",
        )
        payload: Dict[str, Any] = {"email": email, "password": password}
        if captcha_token:
            payload[contract.payload_field] = captcha_token
        response = self.client.request("POST", "/api/v1/auth/login", payload=payload)
        token = response.get("access_token") or response.get("token")
        if isinstance(token, str) and token:
            return token

        temp_token = response.get("temp_token")
        if not isinstance(temp_token, str) or not temp_token:
            raise Sub2ApiApiError(401, "登录未返回 access token", "INVALID_LOGIN_RESPONSE")
        if not totp_secret:
            raise Sub2ApiApiError(
                401,
                "账号启用了两步验证，当前记录没有 TOTP 密钥",
                "TOTP_REQUIRED",
            )
        second = self.client.request(
            "POST",
            "/api/v1/auth/login/2fa",
            payload={"temp_token": temp_token, "totp_code": _totp_code(totp_secret)},
        )
        token = second.get("access_token") or second.get("token")
        if not isinstance(token, str) or not token:
            raise Sub2ApiApiError(401, "两步验证未返回 access token", "INVALID_2FA_RESPONSE")
        return token
