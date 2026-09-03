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

同样必须 hermetic 的是注册库。本仓库目录在这台宿主上同时是部署数据根，
engine.RESULTS_DB_FILE 默认就指向正在服务的线上库
（data/accounts/registration_results.sqlite3），而
RegistrationJobCoordinator._persist_snapshot 会无条件走默认仓库。实测
（2026-09-03）一个裸 coordinator 的进度测试把线上 registration_job_snapshot
覆盖成了 fixture 值（target=3、first@example.com、last_error=服务重启），
因此这里把整个测试会话的默认注册库改指临时目录，并另加一条守卫：任何
测试改写本机真实快照行都直接失败。
"""
import os
import sqlite3
from unittest import mock

import pytest

from backend.registration import engine as _engine

# 必须在任何隔离 patch 之前抓取，守卫比较的才是真实部署路径。
LIVE_RESULTS_DB_FILE = str(_engine.RESULTS_DB_FILE)

_SNAPSHOT_COLUMNS = (
    "id, batch_id, running, started_at, finished_at, target_count, workers, source,"
    " profile_id, last_error, completed_count, success_count, failure_count,"
    " current_stage, current_email, updated_at"
)


def _live_snapshot_row(path: str):
    """只读快照行；库不存在或不可读时返回 None（CI 干净环境即属此类）。"""
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        return conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM registration_job_snapshot"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@pytest.fixture(scope="session", autouse=True)
def _isolated_registration_store(tmp_path_factory):
    """把默认注册库指向会话临时目录，禁止测试写入部署数据根。"""
    root = tmp_path_factory.mktemp("registration-store")
    database = root / "registration_results.sqlite3"
    repository = _engine.RegistrationRepository(database)
    with (
        mock.patch.object(_engine, "RESULTS_DB_FILE", str(database)),
        mock.patch.object(_engine, "_repository", repository),
    ):
        yield


@pytest.fixture(autouse=True)
def _never_write_live_registration_db():
    """守卫：改写本机真实快照行的测试直接失败，防止隔离再次失效。"""
    before = _live_snapshot_row(LIVE_RESULTS_DB_FILE)
    yield
    after = _live_snapshot_row(LIVE_RESULTS_DB_FILE)
    if before is not None and after is not None and before != after:
        pytest.fail(
            "测试改写了本机部署的 registration_job_snapshot（"
            f"{LIVE_RESULTS_DB_FILE}）；必须注入隔离仓库，不得依赖默认路径",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _web_admin_exists():
    from backend.web import application

    with mock.patch.object(application, "_web_auth_enabled", return_value=True):
        yield
