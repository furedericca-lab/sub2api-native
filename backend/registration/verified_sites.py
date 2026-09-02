"""Catalog of registration sites that have been verified by the operator.

Only entries in this module may be used by a new Sub2API Profile.  Site URLs
are implementation data, not user-provided input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List
from urllib.parse import urlsplit


@dataclass(frozen=True)
class VerifiedSite:
    key: str
    name: str
    register_url: str
    default_aff_code: str = ""
    email_suffix_whitelist: tuple[str, ...] = ()
    checkin_supported: bool = False


VERIFIED_SITES: tuple[VerifiedSite, ...] = (
    VerifiedSite(
        "true-sota", "TrueSOTA", "https://true-sota.com/register", "U4Z83MFFZ9LP",
        (
            "126.com", "139.com", "163.com", "189.cn", "aliyun.com", "apache.org",
            "deepseek.com", "*.edu.cn", "*.edu.hk", "*.edu.mo", "*.edu.tw", "foxmail.com",
            "gmail.com", "*.gov.cn", "qq.com", "sina.cn", "sina.com", "sohu.com",
            "xiaomi.com", "yahoo.com", "privaterelay.appleid.com", "xpertiise.com",
            "linux.do",
        ),
    ),
    VerifiedSite(
        "ctai", "CTAI", "https://ai.chengtingkj.org/register", "YCB45KQXLMD3",
        ("qq.com", "gmail.com", "icloud.com", "163.com", "hotmail.com", "outlook.com", "nodeloc.cc", "foxmail.com"),
    ),
    VerifiedSite(
        "bmapi", "BMAPI", "https://bmapi.020212.xyz/register", "WMHL43737MPD",
        ("qq.com", "gmail.com", "126.com", "163.com", "*.edu.cn"),
        True,
    ),
    VerifiedSite(
        "xxcy", "XXCY", "https://ai.xxcy.shop/register", "V7LCN6FNVX37",
        ("qq.com",),
    ),
    VerifiedSite(
        "sharezzz", "ShareZZZ", "https://www.sharezzz.com/register", "L3N8QGNFP2X9",
        ("qq.com", "163.com", "gmail.com"),
    ),
    VerifiedSite(
        "zaion", "ZAION", "https://api.060913.xyz/register", "PVF8NAUAGZ8M",
        ("qq.com",),
    ),
    # An empty live suffix list means unrestricted registration.  The catalog
    # canonicalizes that upstream representation to the shared "*" contract.
    VerifiedSite(
        "lianjieai", "连接AI", "https://lianjieai.top/register", "Z4NPCESZBC9K",
        ("*",),
    ),
)

_BY_KEY: Dict[str, VerifiedSite] = {site.key: site for site in VERIFIED_SITES}


def list_verified_sites() -> List[Dict[str, Any]]:
    return [
        {
            "key": site.key,
            "name": site.name,
            "register_url": site.register_url,
            "default_aff_code": site.default_aff_code,
            "email_suffix_whitelist": list(site.email_suffix_whitelist),
            "checkin_supported": site.checkin_supported,
        }
        for site in VERIFIED_SITES
    ]


def get_verified_site(site_key: str) -> VerifiedSite | None:
    return _BY_KEY.get(str(site_key or "").strip().lower())


def find_verified_site_by_url(register_url: str) -> VerifiedSite | None:
    """Resolve a legacy stored URL to the verified catalog by origin."""
    parsed = urlsplit(str(register_url or "").strip())
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    for site in VERIFIED_SITES:
        site_parts = urlsplit(site.register_url)
        if origin == f"{site_parts.scheme.lower()}://{site_parts.netloc.lower()}":
            return site
    return None
