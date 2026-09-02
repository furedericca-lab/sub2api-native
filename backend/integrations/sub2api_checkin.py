"""Sub2API daily check-in protocol."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

from .sub2api_auth import Sub2ApiAuthService
from .sub2api_captcha import CamoufoxCaptchaSolver, CaptchaError
from .sub2api_transport import (
    Sub2ApiApiError,
    Sub2ApiClient,
    Sub2ApiNetworkError,
)


CheckinApiError = Sub2ApiApiError
CheckinNetworkError = Sub2ApiNetworkError

STATUS_SUCCESS = "success"
STATUS_ALREADY = "already_checked_in"
STATUS_AUTH_FAILURE = "authentication_failure"
STATUS_CAPTCHA_REQUIRED = "captcha_manual_required"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNCERTAIN = "uncertain"
STATUS_UPSTREAM_FAILURE = "upstream_failure"


@dataclass(frozen=True)
class CheckinResult:
    status: str
    message: str
    checkin_date: str = ""
    next_reset_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "checkin_date": self.checkin_date,
            "next_reset_at": self.next_reset_at,
        }


class Sub2ApiCheckinService:
    def __init__(
        self,
        client: Sub2ApiClient,
        captcha_solver: Any,
        *,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self.client = client
        self.captcha_solver = captcha_solver
        self.auth = Sub2ApiAuthService(client, captcha_solver)
        self.timezone = str(timezone or "Asia/Shanghai")

    def _status(self, token: str) -> Dict[str, Any]:
        return self.client.request(
            "GET",
            "/api/v1/checkin/status?timezone=" + quote(self.timezone),
            token=token,
        )

    def _login(
        self,
        email: str,
        password: str,
        settings: Dict[str, Any],
        totp_secret: str,
    ) -> str:
        return self.auth.login(
            email,
            password,
            settings,
            totp_secret=totp_secret,
        )

    @staticmethod
    def _result(
        status: str,
        message: str,
        remote: Optional[Dict[str, Any]] = None,
    ) -> CheckinResult:
        data = remote or {}
        return CheckinResult(
            status,
            message,
            str(data.get("checkin_date") or ""),
            str(data.get("next_reset_at") or ""),
        )

    def _api_error_result(self, exc: CheckinApiError, stage: str) -> CheckinResult:
        code = exc.code.upper()
        message = str(exc).upper()
        if stage == "login" and (
            exc.status_code in {401, 403}
            or any(marker in code for marker in ("INVALID_CREDENTIAL", "TOTP", "AUTH"))
        ):
            return self._result(STATUS_AUTH_FAILURE, "账号认证失败或需要两步验证")
        if "CAP" in code or "CAPTCHA" in code or "CAP" in message or "CAPTCHA" in message:
            label = "登录" if stage == "login" else "签到"
            return self._result(STATUS_CAPTCHA_REQUIRED, f"{label}验证码被上游拒绝，请稍后重试")
        labels = {
            "settings": "读取站点设置",
            "login": "登录",
            "status": "读取签到状态",
            "attempt": "准备签到",
            "claim": "提交签到",
        }
        return self._result(
            STATUS_UPSTREAM_FAILURE,
            f"{labels.get(stage, '上游接口')}失败: {str(exc)[:220]}",
        )

    def verify_credentials(
        self,
        email: str,
        password: str,
        *,
        totp_secret: str = "",
    ) -> CheckinResult:
        stage = "settings"
        try:
            settings = self.auth.public_settings()
            stage = "login"
            self._login(email, password, settings, totp_secret)
            return self._result(STATUS_SUCCESS, "账号凭据验证成功（未执行签到）")
        except CaptchaError as exc:
            return self._result(STATUS_CAPTCHA_REQUIRED, str(exc)[:300])
        except CheckinApiError as exc:
            return self._api_error_result(exc, stage)
        except CheckinNetworkError:
            labels = {"settings": "读取站点设置", "login": "登录"}
            return self._result(
                STATUS_UPSTREAM_FAILURE,
                f"{labels.get(stage, '上游请求')}时网络不可达或超时",
            )
        except (ValueError, TypeError) as exc:
            return self._result(STATUS_UPSTREAM_FAILURE, str(exc)[:300])

    def run(
        self,
        email: str,
        password: str,
        *,
        totp_secret: str = "",
    ) -> CheckinResult:
        stage = "settings"
        try:
            settings = self.auth.public_settings()
            stage = "login"
            token = self._login(email, password, settings, totp_secret)
            stage = "status"
            status = self._status(token)
            if not status.get("enabled"):
                return self._result(STATUS_UNSUPPORTED, "该站点或账号未启用每日签到", status)
            if status.get("checked_in"):
                return self._result(STATUS_ALREADY, "今天已经签到", status)

            fingerprint = f"checkin-{int(time.time())}-{secrets.token_hex(8)}"
            stage = "attempt"
            attempt = self.client.request(
                "POST",
                "/api/v1/checkin/attempt",
                payload={"fingerprint_key": fingerprint},
                token=token,
            )
            attempt_id = attempt.get("attempt_id") or attempt.get("id")
            if not isinstance(attempt_id, (str, int)) or not str(attempt_id).strip():
                return self._result(STATUS_UPSTREAM_FAILURE, "签到准备接口未返回 attempt_id")

            stage = "status"
            current = self._status(token)
            claim: Dict[str, Any] = {
                "attempt_id": attempt_id,
                "fingerprint_key": fingerprint,
            }
            if current.get("captcha_enabled"):
                provider = str(
                    attempt.get("captcha_provider")
                    or current.get("captcha_provider")
                    or settings.get("captcha_provider")
                    or ""
                ).lower()
                captcha_settings = dict(settings)
                captcha_settings.update(
                    {key: value for key, value in current.items() if value is not None}
                )
                captcha_settings.update(
                    {key: value for key, value in attempt.items() if value is not None}
                )
                stage = "captcha"
                captcha_token = self.captcha_solver.solve(
                    provider,
                    captcha_settings,
                    self.client.base_url + "/dashboard",
                )
                if not captcha_token:
                    return self._result(
                        STATUS_CAPTCHA_REQUIRED,
                        "签到需要验证码，但未获得 token",
                    )
                claim["captcha_token"] = captcha_token

            stage = "claim"
            try:
                final = self.client.request(
                    "POST",
                    "/api/v1/checkin",
                    payload=claim,
                    token=token,
                )
            except CheckinApiError as exc:
                return self._api_error_result(exc, stage)
            except CheckinNetworkError:
                try:
                    observed = self._status(token)
                except (CheckinApiError, CheckinNetworkError):
                    return self._result(
                        STATUS_UNCERTAIN,
                        "签到请求已发出，但无法确认最终结果",
                    )
                if observed.get("checked_in"):
                    return self._result(STATUS_SUCCESS, "签到成功", observed)
                return self._result(
                    STATUS_UNCERTAIN,
                    "签到请求已发出，远端状态仍未确认",
                    observed,
                )
            return self._result(STATUS_SUCCESS, "签到成功", final)
        except CaptchaError as exc:
            return self._result(STATUS_CAPTCHA_REQUIRED, str(exc)[:300])
        except CheckinApiError as exc:
            return self._api_error_result(exc, stage)
        except CheckinNetworkError:
            labels = {
                "settings": "读取站点设置",
                "login": "登录",
                "status": "读取签到状态",
                "attempt": "准备签到",
                "claim": "提交签到",
            }
            return self._result(
                STATUS_UPSTREAM_FAILURE,
                f"{labels.get(stage, '上游请求')}时网络不可达或超时",
            )
        except (ValueError, TypeError) as exc:
            return self._result(STATUS_UPSTREAM_FAILURE, str(exc)[:300])
