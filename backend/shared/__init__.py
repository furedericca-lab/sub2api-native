"""跨业务包复用的运行时基础设施。"""

from .paths import DATA_ROOT, PROJECT_ROOT, STATIC_ROOT, resolve_project_path

__all__ = ["DATA_ROOT", "PROJECT_ROOT", "STATIC_ROOT", "resolve_project_path"]
