# -*- coding: utf-8 -*-
"""注册产物定位与清理。

根据数据库记录收集关联产物（失败截图 / 诊断文件等明确属于该记录的
artifact），并限制删除范围以保护数据库。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Set, Tuple


_PATH_IN_TEXT_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|\\|/)[^\r\n\t\"']+\.(?:json|txt|png|jpe?g|webp))",
    re.IGNORECASE,
)
_PROTECTED_BASENAMES = {
    "registration_results.sqlite3",
    "registration_results.sqlite3-wal",
    "registration_results.sqlite3-shm",
    "ledger_write_failure.json",
    "sub2api_result_write_failure.json",
}


def _extract_paths_from_text(text: Any) -> List[str]:
    """从自由文本中提取疑似本地文件路径。"""
    found: List[str] = []
    seen: Set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().strip("\"'")
        if not line:
            continue
        candidates: List[str] = []
        if ": " in line:
            candidates.append(line.split(": ", 1)[1].strip().strip("\"'"))
        candidates.append(line)
        for match in _PATH_IN_TEXT_RE.finditer(line):
            candidates.append(match.group("path").strip().strip("\"'"))
        for candidate in candidates:
            value = str(candidate or "").strip().strip("\"'")
            if not value:
                continue
            lower = value.lower()
            if not lower.endswith((".json", ".txt", ".png", ".jpg", ".jpeg", ".webp")):
                continue
            if value in seen:
                continue
            seen.add(value)
            found.append(value)
    return found


def collect_related_file_paths(
    record: Dict[str, Any],
    *,
    accounts_dir: str,
    app_dir: str = "",
) -> List[str]:
    """收集注册记录关联的本地真实文件路径（去重，仅已存在文件）。

    Sub2API 记录只删自身产物（screenshot_path / extra 中记录的诊断文件）。
    """
    email = str(record.get("email") or "").strip()
    _ = email  # Sub2API 没有按邮箱命名的账号文件

    candidates: List[str] = []
    for key in ("screenshot_path",):
        value = str(record.get(key) or "").strip()
        if value:
            candidates.append(value)

    # extra_json 中记录的诊断产物（注册时明确属于该记录的 artifact）。
    extra = record.get("extra_json") or {}
    if isinstance(extra, str):
        try:
            import json

            extra = json.loads(extra) if extra.strip() else {}
        except (TypeError, ValueError):
            extra = {}
    if isinstance(extra, dict):
        for key in ("diagnostics_path", "diagnostic_file"):
            value = str(extra.get(key) or "").strip()
            if value:
                candidates.append(value)

    resolved: List[str] = []
    seen: Set[str] = set()
    for raw in candidates:
        path = os.path.abspath(os.path.expanduser(str(raw or "").strip()))
        if not path or path in seen:
            continue
        base = os.path.basename(path).lower()
        if base in _PROTECTED_BASENAMES:
            continue
        if not os.path.isfile(path):
            continue
        seen.add(path)
        resolved.append(path)
    return resolved


def delete_related_files(paths: Iterable[str]) -> Tuple[List[str], List[str]]:
    """删除关联文件，返回 (成功列表, 失败描述列表)。"""
    deleted: List[str] = []
    errors: List[str] = []
    for raw in paths:
        path = os.path.abspath(str(raw or "").strip())
        if not path:
            continue
        base = os.path.basename(path).lower()
        if base in _PROTECTED_BASENAMES:
            continue
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            deleted.append(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return deleted, errors
