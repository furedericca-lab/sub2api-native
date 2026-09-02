"""Structured classification for verified-site registration failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


ALREADY_REGISTERED = "already_registered"

_ALREADY_REGISTERED_CODES = {
    "ACCOUNT_EXISTS",
    "EMAIL_ALREADY_EXISTS",
    "EMAIL_EXISTS",
    "USER_EMAIL_EXISTS",
}

_ALREADY_REGISTERED_PHRASES = (
    "email already exists",
    "email address already exists",
    "email is already registered",
    "email has already been registered",
    "email already registered",
    "account already exists",
    "account is already registered",
    "邮箱已存在",
    "邮箱地址已存在",
    "邮箱已经注册",
    "邮箱已注册",
    "该邮箱已被注册",
    "此邮箱已注册",
)

_ERROR_KEYS = {
    "code",
    "error",
    "error_code",
    "errorcode",
    "detail",
    "message",
    "reason",
}

_REGISTRATION_ENDPOINTS = (
    "/auth/register",
    "/auth/send-verify-code",
)


@dataclass(frozen=True)
class RegistrationFailureSignal:
    kind: str
    code: str = ""
    message: str = ""


def _error_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key or "").strip().casefold()
            if normalized_key in _ERROR_KEYS:
                values.extend(_error_values(item))
            elif normalized_key in {"body", "data", "errors", "response"}:
                values.extend(_error_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.extend(_error_values(item))
    elif value is not None and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            values.append(text)
    return values


def classify_registration_failure(value: Any) -> RegistrationFailureSignal | None:
    values = _error_values(value)
    if isinstance(value, str) and value.strip():
        values.append(value.strip())
    for item in values:
        code = item.strip().upper().replace("-", "_").replace(" ", "_")
        if code in _ALREADY_REGISTERED_CODES:
            return RegistrationFailureSignal(ALREADY_REGISTERED, code=code)
    combined = "\n".join(values).casefold()
    if any(phrase in combined for phrase in _ALREADY_REGISTERED_PHRASES):
        return RegistrationFailureSignal(ALREADY_REGISTERED, message=combined[:240])
    return None


class RegistrationResponseMonitor:
    """Collect bounded registration error responses from a Playwright page."""

    def __init__(self, page: Any):
        self._responses: list[Any] = []
        self._raw_page = getattr(page, "raw_page", None)
        self._handler = self._on_response
        if self._raw_page is not None and hasattr(self._raw_page, "on"):
            self._raw_page.on("response", self._handler)

    def _on_response(self, response: Any) -> None:
        try:
            status = int(getattr(response, "status", 0) or 0)
            path = urlsplit(str(getattr(response, "url", "") or "")).path.casefold()
        except Exception:
            return
        if status < 400 or not any(path.endswith(item) for item in _REGISTRATION_ENDPOINTS):
            return
        self._responses.append(response)
        if len(self._responses) > 10:
            del self._responses[:-10]

    def latest_signal(self) -> RegistrationFailureSignal | None:
        while self._responses:
            response = self._responses.pop(0)
            try:
                payload = response.json()
            except Exception:
                try:
                    payload = {"message": response.text()}
                except Exception:
                    continue
            signal = classify_registration_failure(payload)
            if signal is not None:
                return signal
        return None

    def close(self) -> None:
        if self._raw_page is not None and hasattr(self._raw_page, "remove_listener"):
            try:
                self._raw_page.remove_listener("response", self._handler)
            except Exception:
                pass
