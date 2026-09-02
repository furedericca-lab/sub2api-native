"""backend/tests 共享 fixture。

Web API 测试必须 hermetic：不能依赖本机恰好存在的 data/web_auth.json。
application 的登录中间件分两道闸门——
  1) 管理员未设置 → 401 setup_required（CI 全新 checkout 无 web_auth.json 时触发）
  2) 会话无效     → 401 请先登录
各 API harness 已自行 patch `_valid_session=True`（假定已登录），但缺少第 1 道
的 hermetic 假设。这里统一假定“管理员已存在”，使套件在任意干净环境（含 CI）
都稳定通过，而不再依赖本机凭据文件。

注意：这只让测试独立于本机状态，不削弱 live 系统的 fail-closed 行为——真实
部署仍要求先通过 /api/auth/setup 创建管理员。
"""
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _web_admin_exists():
    from backend.web import application

    with mock.patch.object(application, "_web_auth_enabled", return_value=True):
        yield
