"""集中管理项目运行目录，避免子包层级变化影响数据路径。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
STATIC_ROOT = PROJECT_ROOT / "front" / "dist"


def resolve_project_path(value: str | Path) -> Path:
    """将用户配置路径解析为绝对路径；相对路径以项目根目录为基准。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
