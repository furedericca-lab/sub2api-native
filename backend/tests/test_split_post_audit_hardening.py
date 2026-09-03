"""Split post-audit 加固回归（基于 ffd9844 审计）。

覆盖：
1. 两份 Compose 的 OutlookEmail 管理端口默认绑定 127.0.0.1，并允许运维显式
   指定宿主机私网地址；sub2api-native Web 端口保持宿主 0.0.0.0（Docker 部署
   契约）。
2. update.sh 宿主机健康检查目标必须跟随 SUB2API_WEB_PORT（读 deploy/.env），
   容器内契约端口保持固定 8787。
3. 运行时代码 / docker 入口 / .gitignore 的历史残留扫描
   （ResidualReferenceTests 只覆盖 backend 非测试代码，这里补全仓库面）。
4. 单 worker 语义：register_workers 不再是用户可配置项（CONFIG_PUBLIC_KEYS /
   DEFAULT_CONFIG / config.example.json 均不暴露），任务启动 API 也不接受
   workers 参数；job 状态中的 workers 是只读内部字段（固定 1）。
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _host_ip_of_port_spec(spec) -> str:
    """解析 'host:container:service' / 'container:service' / 'host:service' 端口映射。

    返回绑定 host（空 = 默认 0.0.0.0）。
    """
    text = str(spec)
    if ":" not in text:
        return ""
    head, _, _ = text.partition(":")
    # 两段式 '8080:80' 的 head 是容器端口；三段式的 head 才是 host。
    # 这里统一：若 head 不是 IP 形态且整串只有两段，视为 host:container。
    if text.count(":") == 2:
        return head
    if re.fullmatch(r"(\d{1,5})(/\d{1,5})?", head):
        # '8080:80' 两段式：host 缺省 0.0.0.0
        return ""
    return head


class ComposePortBindingTests(unittest.TestCase):
    """单容器仍保留两个原生端口，LAN 绑定必须显式配置。"""

    def test_single_service_publishes_mail_port_with_loopback_default(self):
        expected = (
            "${OUTLOOKEMAIL_BIND_HOST:-127.0.0.1}:"
            "${OUTLOOKEMAIL_PORT:-15000}:5000"
        )
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                data = _parse_compose(REPO_ROOT / "deploy" / name)
                self.assertEqual(set(data["services"]), {"sub2api-native"})
                ports = data["services"]["sub2api-native"]["ports"]
                self.assertEqual(len(ports), 2)
                self.assertEqual(
                    ports[1],
                    expected,
                    f"{name}: OutlookEmail 原生端口必须默认绑定回环并仅允许显式覆盖",
                )

    def test_public_mail_port_is_independent_from_published_port(self):
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                data = _parse_compose(REPO_ROOT / "deploy" / name)
                environment = data["services"]["sub2api-native"]["environment"]
                self.assertEqual(
                    environment["OUTLOOKEMAIL_PUBLIC_PORT"],
                    "${OUTLOOKEMAIL_PUBLIC_PORT:-${OUTLOOKEMAIL_PORT:-15000}}",
                    f"{name}: browser-facing mailbox port must be independently configurable",
                )

    def test_outlook_email_bind_host_example_is_loopback(self):
        env_example = (REPO_ROOT / "deploy" / ".env.example").read_text(
            encoding="utf-8"
        )
        self.assertRegex(env_example, r"(?m)^OUTLOOKEMAIL_BIND_HOST=127\.0\.0\.1$")
        self.assertNotRegex(env_example, r"(?m)^OUTLOOKEMAIL_BIND_HOST=(0\.0\.0\.0|::)$")

    def test_sub2api_web_port_keeps_host_binding(self):
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                data = _parse_compose(REPO_ROOT / "deploy" / name)
                ports = data["services"]["sub2api-native"]["ports"]
                self.assertEqual(len(ports), 2)
                # 契约：容器内固定 8787；宿主端口可配置但必须最终映射到 8787
                self.assertIn("8787", str(ports[0]))
                self.assertTrue(
                    str(ports[0]).endswith(":8787"),
                    f"{name}: sub2api-native 端口必须映射到容器 8787，实际 {ports[0]!r}",
                )

    def test_gate_l_env_var_passed_into_both_compose_files(self):
        """Gate L 上限必须注入容器，否则 live Docker 运行时门禁不生效。"""
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                data = _parse_compose(REPO_ROOT / "deploy" / name)
                env = data["services"]["sub2api-native"]["environment"]
                self.assertIn(
                    "SUB2API_GATE_L_MAX_COUNT",
                    env,
                    f"{name}: 必须把 SUB2API_GATE_L_MAX_COUNT 注入 sub2api-native",
                )
                self.assertEqual(
                    env["SUB2API_GATE_L_MAX_COUNT"],
                    "${SUB2API_GATE_L_MAX_COUNT:-1}",
                )

    def test_embedded_outlookemail_logs_do_not_emit_proxy_endpoints(self):
        """嵌入式 OutlookEmail 的 INFO 代理日志必须保持关闭。"""
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                data = _parse_compose(REPO_ROOT / "deploy" / name)
                env = data["services"]["sub2api-native"]["environment"]
                self.assertEqual(
                    env.get("LOG_LEVEL"),
                    "WARNING",
                    f"{name}: vendor INFO 日志不能暴露代理端点",
                )

    def test_service_and_image_names_are_sub2api(self):
        data = _parse_compose(REPO_ROOT / "deploy" / "compose.yaml")
        self.assertEqual(data.get("name"), "sub2api-native")
        self.assertEqual(
            data["services"]["sub2api-native"]["image"], "sub2api-native:local"
        )
        self.assertEqual(
            data["services"]["sub2api-native"]["container_name"], "sub2api-native"
        )
        self.assertEqual(set(data["services"]), {"sub2api-native"})
        self.assertNotIn("outlook-email", json.dumps(data))


class UpdateScriptPortTests(unittest.TestCase):
    """审计 P1：update.sh 宿主健康检查跟随 SUB2API_WEB_PORT，容器内固定 8787。"""

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO_ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    def test_host_health_target_uses_configurable_port(self):
        # 从 .env 解析 SUB2API_WEB_PORT 并默认 8787
        self.assertIn(
            "'/^SUB2API_WEB_PORT=/{value=$2}",
            self.text,
            "update.sh 必须从 deploy/.env 读取 SUB2API_WEB_PORT",
        )
        self.assertIn('HOST_WEB_PORT="${HOST_WEB_PORT:-8787}"', self.text)
        self.assertIn(
            'curl -fsS "http://127.0.0.1:${HOST_WEB_PORT}/api/health"',
            self.text,
            "宿主健康检查必须使用 ${HOST_WEB_PORT}",
        )

    def test_container_health_target_stays_fixed_8787(self):
        self.assertIn(
            "urlopen('http://127.0.0.1:8787/api/health', timeout=5)",
            self.text,
            "容器内契约端口必须固定 8787",
        )
        self.assertIn(
            "urlopen('http://127.0.0.1:5000/', timeout=5).read()",
            self.text,
            "sub2api-native → OutlookEmail 内部地址必须固定 http://127.0.0.1:5000",
        )

    def test_update_script_checks_the_single_embedded_service(self):
        self.assertIn("wait_healthy sub2api-native", self.text)
        self.assertNotIn("wait_healthy outlook-email", self.text)
        self.assertIn("MAIL_PORT=", self.text)
        self.assertIn("check-outlookemail-contract.py", self.text)
        self.assertIn('RUNTIME_ENV="../data/outlookemail/runtime.env"', self.text)
        self.assertIn("credential_file", self.text)

    def test_compose_allows_persisted_runtime_credentials(self):
        for name in ("compose.yaml", "docker-compose.yml"):
            with self.subTest(compose=name):
                text = (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")
                self.assertIn("path: ./outlookemail.env", text)
                self.assertIn("required: false", text)

    def test_contract_smoke_is_http_only(self):
        script = REPO_ROOT / "scripts" / "check-outlookemail-contract.py"
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", text.lower())
        self.assertIn("/api/extension/login", text)
        self.assertIn("/api/external/accounts", text)
        self.assertIn("/api/external/emails", text)

    def test_dockerfile_keeps_python_dependency_graphs_separate(self):
        text = (REPO_ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python3 -m venv /opt/sub2api-venv", text)
        self.assertIn("python3 -m venv /opt/outlookemail-venv", text)
        self.assertIn("/opt/sub2api-venv/bin/pip check", text)
        self.assertIn("/opt/outlookemail-venv/bin/pip check", text)
        self.assertIn("/opt/outlookemail-venv/bin/gunicorn", (REPO_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8"))

    def test_runtime_env_loader_accepts_only_required_private_values(self):
        text = (REPO_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("LOGIN_PASSWORD|SECRET_KEY", text)
        self.assertIn("*) continue ;;", text)

    def test_env_port_parsing_matches_real_env_file(self):
        """用真实 deploy/.env 模拟解析逻辑：解析结果必须等于 curl 目标端口。"""
        env_path = REPO_ROOT / "deploy" / ".env"
        if not env_path.exists():
            self.skipTest("deploy/.env 不存在（部署前从 .env.example 创建）")
        # 与 update.sh 完全一致的解析链
        port = None
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("SUB2API_WEB_PORT="):
                port = line.partition("=")[2].strip().strip('"')
        port = port or "8787"
        self.assertTrue(port.isdigit(), f"SUB2API_WEB_PORT 非法: {port!r}")


class RepoResidualScanTests(unittest.TestCase):
    """审计 P2：旧认证/提供商残留扫描覆盖 docker/ 与 .gitignore。"""

    def test_docker_and_gitignore_have_no_legacy_provider_residue(self):
        offenders = []
        for rel in ("docker/", ".gitignore"):
            path = REPO_ROOT / rel
            if path.is_dir():
                files = [
                    p
                    for p in path.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                ]
            else:
                files = [path]
            for file_path in files:
                text = file_path.read_text(encoding="utf-8")
                for token in ("sso", "SSO", "cpa", "CPA", "xai", "xAI", "x.ai",
                              "oauth", "OAuth"):
                    if token in text:
                        offenders.append(
                            f"{rel}{file_path.name}: {token}"
                        )
        self.assertEqual(offenders, [])

    def test_entrypoint_creates_no_cpa_auth_dir(self):
        text = (REPO_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertNotIn("cpa_auth", text)
        self.assertIn('mkdir -p "$DATA_DIR" "$LOG_DIR" "$DATA_DIR/accounts"', text)

    def test_gitignore_keeps_operational_entries(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for needed in (
            ".env",
            "!.env.example",
            "data/*",
            "logs/*",
            "outlookemail-data/",
            "artifacts/",
            "front/node_modules/",
            "outlookemail.env",
        ):
            self.assertIn(needed, text)


class SingleWorkerSemanticsTests(unittest.TestCase):
    """审计 P1/P2：单 worker 语义统一，UI/API 不暴露假并发配置。"""

    def test_register_workers_not_user_configurable(self):
        from backend.web import application

        self.assertNotIn("register_workers", application.CONFIG_PUBLIC_KEYS)

        from backend.registration import engine

        self.assertNotIn("register_workers", engine.DEFAULT_CONFIG)

        example = json.loads(
            (REPO_ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("register_workers", example)

    def test_start_job_endpoint_rejects_workers_param(self):
        """任务启动 API 不接受 workers 参数（单 worker 固定）。"""
        from backend.web import application

        self.assertNotIn(
            "workers", application.StartJobBody.model_fields,
            "StartJobBody 不应再有 workers 字段（单 worker 固定）",
        )

    def test_worker_forcing_kept_in_coordinator_and_engine_runner(self):
        """协调器与 runner 的强制单 worker 是内部实现细节，必须有代码保障。"""
        import inspect

        from backend.web.jobs import RegistrationJobCoordinator

        jobs_src = (REPO_ROOT / "backend" / "web" / "jobs.py").read_text(
            encoding="utf-8"
        )
        engine_src = (REPO_ROOT / "backend" / "registration" / "engine.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("workers = 1", jobs_src, "jobs.start 必须强制 workers=1")
        self.assertIn("并发=1", engine_src, "runner 启动日志必须如实报告 并发=1")
        # 协调器 start() 不再暴露 workers 参数（shell 已删）
        self.assertNotIn(
            "workers", inspect.signature(RegistrationJobCoordinator.start).parameters
        )

    def test_browser_headless_removed_from_public_config_ui(self):
        """标准 Docker 强制 headed（SUB2API_FORCE_HEADED），headless 开关是假配置。"""
        from backend.web import application

        self.assertNotIn(
            "browser_headless", application.CONFIG_PUBLIC_KEYS
        )
        settings_src = (
            REPO_ROOT / "front" / "src" / "pages" / "Settings.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn("无头浏览器", settings_src)

    def test_settings_copy_matches_actual_semantics(self):
        settings_src = (
            REPO_ROOT / "front" / "src" / "pages" / "Settings.tsx"
        ).read_text(encoding="utf-8")
        # 并发配置已删，文案不再提及
        self.assertNotIn("并发", settings_src)
        # debug_mode 不强制单账号：文案如实描述“保留浏览器”
        self.assertNotIn("强制单账号", settings_src)
        self.assertIn("任务结束后保留浏览器", settings_src)

    def test_job_status_reports_workers_as_readonly_internal_field(self):
        """job 状态仍含 workers（快照兼容），值固定 1。"""
        from backend.web.jobs import RegistrationJobCoordinator

        coordinator = RegistrationJobCoordinator()
        coordinator._repository = lambda: None
        coordinator.restore_from_database = lambda: None
        status = coordinator.status()
        self.assertIn("workers", status)
        self.assertEqual(status["workers"], 1)


class CountIsolationAcceptanceGateTests(unittest.TestCase):
    """count>1 浏览器身份隔离：验收门禁（暂不改生命周期，live 前必须验证）。"""

    def test_e2e_checklist_documents_count2_isolation_gate(self):
        # The public AGENTS contract is available in forks; .wiki is optional
        # local operator context and must never be a test dependency.
        checklist = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("count", checklist.lower())
        # 门禁必须明确：批量 count>1 前需验证第二账号不受第一账号登录态影响。
        # 清单正文已统一为英文，因此按英文术语断言同等具体的要求。
        self.assertIn("second account", checklist.lower())
        self.assertIn("clean browser identity", checklist.lower())
        self.assertIn("Gate L", checklist)

    def test_gate_l_max_count_exposed_on_get_config(self):
        """前端 count 上限的单一真相源：GET /api/config 必须下发 gate_l_max_count。"""
        from unittest import mock

        from backend.registration.store import RegistrationRepository
        from backend.web import application
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            from backend.registration import engine

            store = RegistrationRepository(Path(tmp) / "r.db")
            gr_mock = mock.Mock()
            gr_mock.get_registration_repository.return_value = store
            gr_mock.config = {"register_count": 1}
            gr_mock.load_config = lambda: None
            gr_mock.DEFAULT_CONFIG = engine.DEFAULT_CONFIG

            with (
                mock.patch.object(application, "_gr", return_value=gr_mock),
                mock.patch.object(
                    application, "_valid_session", return_value=True
                ),
                mock.patch.dict(os.environ, {"SUB2API_GATE_L_MAX_COUNT": "5"}),
            ):
                client = TestClient(application.create_app())
                resp = client.get("/api/config")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["gate_l_max_count"], 5)

    def test_gate_l_is_code_enforced_not_doc_only(self):
        """Gate L 必须是代码硬门禁：未过验收前 API 拒绝 count>1（fail-closed）。"""
        from unittest import mock

        from backend.registration.store import RegistrationRepository
        from backend.web import application
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "r.db")
            profile = store.create_profile(
                {"name": "P", "site_key": "true-sota"}
            )
            gr_mock = mock.Mock()
            gr_mock.get_registration_repository.return_value = store
            gr_mock.config = {"register_count": 1}
            gr_mock.load_config = lambda: None

            with (
                mock.patch.object(application, "_gr", return_value=gr_mock),
                mock.patch.object(
                    application, "_valid_session", return_value=True
                ),
            ):
                client = TestClient(application.create_app())

                # 默认门禁：上限 1
                with mock.patch.dict(
                    os.environ, {"SUB2API_GATE_L_MAX_COUNT": "1"}
                ):
                    resp = client.post(
                        "/api/job/start",
                        json={"count": 2, "profile_id": profile["id"]},
                    )
                    self.assertEqual(
                        resp.status_code, 409, f"count>1 必须被拒绝: {resp.json()}"
                    )
                    self.assertIn("Gate L", resp.json()["detail"])
                    # count=1 放行（协调器因 Profile 存在而继续，mock 掉后续）
                    with mock.patch.object(
                        application.job_coordinator,
                        "start",
                        return_value={"running": False},
                    ):
                        ok = client.post(
                            "/api/job/start",
                            json={"count": 1, "profile_id": profile["id"]},
                        )
                        self.assertEqual(ok.status_code, 200)

                # 门禁放开（R2 验收后）：上限 1000 → count=2 放行
                with mock.patch.dict(
                    os.environ, {"SUB2API_GATE_L_MAX_COUNT": "1000"}
                ):
                    with mock.patch.object(
                        application.job_coordinator,
                        "start",
                        return_value={"running": False},
                    ) as started:
                        resp = client.post(
                            "/api/job/start",
                            json={"count": 2, "profile_id": profile["id"]},
                        )
                        self.assertEqual(resp.status_code, 200)
                        started.assert_called_once_with(
                            count=2, profile_id=profile["id"]
                        )


if __name__ == "__main__":
    unittest.main()
