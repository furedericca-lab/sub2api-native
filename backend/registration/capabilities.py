"""Normalize public registration settings into a stable capability contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SiteCapabilities:
    """Capabilities discovered from a site's public settings endpoint."""

    registration_enabled: bool = False
    email_verification: bool = False
    captcha_provider: str = "none"
    captcha_site_key: str = ""
    email_suffixes: tuple[str, ...] = ()
    invitation_code: bool = False
    affiliate: bool = False


def _payload_data(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Accept both raw settings and common ``{data: ...}`` API envelopes."""
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def discover_capabilities(payload: Mapping[str, Any] | None) -> SiteCapabilities:
    """Convert site-specific public settings into the internal contract."""
    data = _payload_data(payload)
    provider = str(data.get("captcha_provider") or "").strip().lower()
    if not provider:
        if bool(data.get("turnstile_enabled")):
            provider = "turnstile"
        elif bool(data.get("cap_endpoint") or data.get("cap_asset_url")):
            provider = "cap"
        elif bool(data.get("tencent_captcha_enabled")):
            provider = "tencent"
        elif bool(data.get("aliyun_captcha_enabled")):
            provider = "aliyun"
        else:
            provider = "none"

    raw_suffixes = data.get("registration_email_suffix_whitelist") or ()
    if isinstance(raw_suffixes, str):
        suffixes = (raw_suffixes.strip(),) if raw_suffixes.strip() else ()
    else:
        suffixes = tuple(
            str(value).strip()
            for value in raw_suffixes
            if str(value).strip()
        )

    return SiteCapabilities(
        registration_enabled=bool(data.get("registration_enabled")),
        email_verification=bool(data.get("email_verify_enabled")),
        captcha_provider=provider,
        captcha_site_key=str(
            data.get("captcha_site_key") or data.get("turnstile_site_key") or ""
        ).strip(),
        email_suffixes=suffixes,
        invitation_code=bool(data.get("invitation_code_enabled")),
        affiliate=bool(data.get("affiliate_enabled")),
    )
