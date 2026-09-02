# -*- coding: utf-8 -*-
"""凭据批量导出：每行一条 email----password。"""
from __future__ import annotations

from typing import Any, Dict, Iterable


def build_credentials_text(records: Iterable[Dict[str, Any]]) -> tuple[bytes, int]:
    """Sub2API 凭据导出：每行一条 email----password。

    Account 资产只导 active；registration history 仍只导 success。
    导出只含邮箱 + 每账号当前密码。
    """
    lines_out: list = []
    exported = 0
    for record in records:
        status = str(record.get("registration_status") or record.get("status") or "").strip().lower()
        if status not in {"success", "active"}:
            continue
        email = str(record.get("email") or "").strip()
        password = str(record.get("password") or "").strip()
        if not email or not password:
            continue
        lines_out.append(f"{email}----{password}")
        exported += 1
    payload = ("\n".join(lines_out) + "\n") if lines_out else ""
    return payload.encode("utf-8"), exported
