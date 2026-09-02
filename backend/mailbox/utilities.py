"""邮箱渠道共享的小型解析工具。"""

from __future__ import annotations

import re
import secrets
import string
from email import message_from_string
from email.header import decode_header, make_header
from typing import Any, List, Optional


def generate_username(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(max(3, length)))


def pick_list_payload(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return [item for item in data["results"] if isinstance(item, dict)]
        if isinstance(data.get("hydra:member"), list):
            return [item for item in data["hydra:member"] if isinstance(item, dict)]
        if isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
        if isinstance(data.get("messages"), list):
            return [item for item in data["messages"] if isinstance(item, dict)]
        if isinstance(data.get("data"), dict):
            nested = data.get("data") or {}
            if isinstance(nested.get("messages"), list):
                return [item for item in nested["messages"] if isinstance(item, dict)]
    return []


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# 验证码形如 I6R-B2W：必须全大写，否则邮件模板里的 CSS 类名（如 sm-w-per-100）会被误判。
_CODE_TOKEN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"
_CODE_WITH_CONTEXT_RE = re.compile(
    r"(?:code|验证码)\s*(?:is|：|:)?\s*\b(" + _CODE_TOKEN + r")\b", re.IGNORECASE
)
_CODE_BARE_RE = re.compile(r"\b(" + _CODE_TOKEN + r")\b")
_NUMERIC_CODE_RES = [
    re.compile(
        r"(?:your\s+)?(?:verification|confirmation|confirm)\s+code"
        r"\s*(?:is)?\s*[:：]?\s*(\d{3}-\d{3}|\d{4,8})",
        re.IGNORECASE,
    ),
    re.compile(
        r"your\s+code\s*(?:is)?\s*[:：]?\s*(\d{3}-\d{3}|\d{4,8})",
        re.IGNORECASE,
    ),
    re.compile(r"验证码\s*(?:是|为)?\s*[:：]?\s*(\d{3}-\d{3}|\d{4,8})"),
]


def strip_html(html: str) -> str:
    """剥掉 HTML 标签，取纯文本。

    必须先删除 script/style 块与注释：只删尖括号的话，<style> 里的 CSS 正文
    会原样留在结果里，其中的类名（如 .sm-w-per-100）会被验证码正则误命中。
    """
    if not html:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    return _TAG_RE.sub(" ", cleaned)


def looks_like_raw_email(value: str) -> bool:
    """粗判是否为 RFC822 原文：开头若干行里出现邮件头即认为是。"""
    if not value:
        return False
    for line in value.lstrip().splitlines()[:12]:
        if not line.strip():
            break
        if re.match(r"^[A-Za-z\-]{2,40}:\s", line):
            return True
    return False


def _decode_mime_header(value: str) -> str:
    """解 RFC2047 编码字（=?UTF-8?B?...?=）；失败时原样返回。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_raw_email(raw: str) -> dict:
    """把 RFC822 原文解成 {"subject", "text"}。

    cloudflare_temp_email 的 /api/mails、/admin/mails 按设计只回原始 MIME，
    不保证带已解析的 subject/text/html。直接对原文跑验证码正则会漏：
    base64 正文整段是乱码，quoted-printable 的软换行（I6R=\\n-B2W）会把
    验证码劈成两半，主题还可能是 =?UTF-8?B?...?= 编码字。
    """
    if not raw:
        return {"subject": "", "text": ""}
    try:
        message = message_from_string(raw)
    except Exception:
        return {"subject": "", "text": raw}

    subject = _decode_mime_header(str(message.get("Subject", "") or ""))
    chunks: List[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            raw_payload = part.get_payload()
            body = raw_payload if isinstance(raw_payload, str) else ""
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = payload.decode("utf-8", errors="replace")
        if not body.strip():
            continue
        chunks.append(strip_html(body) if content_type == "text/html" else body)

    if not chunks:
        # 没有可识别的 text/* part（如整封就是裸正文），退回原文，避免丢内容。
        return {"subject": subject, "text": raw}
    return {"subject": subject, "text": "\n".join(chunks)}


def _match_code(pattern: re.Pattern, source: str) -> Optional[str]:
    """取第一个含字母的匹配，纯数字串（如 100-200）不是验证码。"""
    for match in pattern.finditer(source):
        token = match.group(1)
        if any(ch.isalpha() for ch in token):
            return token
    return None


def extract_verification_code(text: str, subject: str = "") -> Optional[str]:
    subject = subject or ""
    text = text or ""
    # 渠道适配器通常已清理 HTML；这里再做一次兜底，避免直接传入原始
    # HTML 时把 style/script 中的数字片段误当验证码。
    if "<" in text:
        text = strip_html(text)
    # 主题最干净，优先；正文里带 code 关键字的上下文次之，裸 token 最后。
    for pattern in (_CODE_WITH_CONTEXT_RE, _CODE_BARE_RE):
        for source in (subject, text):
            code = _match_code(pattern, source)
            if code:
                return code
    for pattern in _NUMERIC_CODE_RES:
        match = pattern.search(text) or pattern.search(subject)
        if match:
            return match.group(1)
    return None
