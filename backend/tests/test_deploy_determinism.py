"""Deployment determinism and repository hygiene regressions.

Scope: "deployment false-failure + Gate L fail-closed hardening".

1. ``scripts/check-outlookemail-contract.py`` must stop judging a healthy
   deployment dead when only Microsoft/Graph tail latency is exceeded, while
   still failing closed on a definite HTTP contract violation or an
   unreachable endpoint. The strict probes keep strict semantics.
2. ``deploy/check-gate-l.sh`` must assert that the *rendered* Gate L limit
   equals the expected limit and must default to fail-closed ``1``. A
   ``deploy/.env`` override must not silently reopen batch registration.
3. ``front/dist`` is ignored generated output and must never be tracked, so it
   cannot become a second apparent source of truth.
4. No repository command may hand an unexpanded ``*`` glob to a file-creating
   command: that silently creates a file literally named ``*`` and succeeds.

All fixtures are synthetic. These tests never contact a running container.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-outlookemail-contract.py"
GATE_L = REPO_ROOT / "deploy" / "check-gate-l.sh"


def _load_contract_module():
    spec = importlib.util.spec_from_file_location("contract_smoke", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTransport:
    """Stand-in for the smoke's HTTP layer, scripted per URL path.

    Records every call so retry and per-endpoint budget behaviour is asserted
    rather than inferred. Exceptions come from the loaded module so the test
    exercises the real classification contract.
    """

    def __init__(self, module, **behaviour):
        self.module = module
        self.behaviour = behaviour
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, opener, method, url, *, payload=None, api_key="", timeout=0.0):
        self.calls.append((method, url, timeout))
        path = urllib.parse.urlsplit(url).path

        if path == "/":
            return self._strict("root", 200, {"ok": True})
        if path == "/api/extension/login":
            return self._strict(
                "login", 200, {"success": True, "launch_url": "/extension-login/tok"}
            )
        if path == "/api/external/accounts":
            return self._strict(
                "accounts", 200, {"success": True, "accounts": [{"email": "user@example.test"}]}
            )
        if path == "/api/external/emails":
            return self._emails()
        raise AssertionError(f"unexpected request {method} {url}")

    def _strict(self, probe, status, payload):
        mode = self.behaviour.get(probe, "ok")
        if mode == "timeout":
            raise self.module.ContractTimeout("read timeout")
        if mode == "unreachable":
            raise self.module.EndpointUnreachable("refused")
        if mode == "status":
            return 500, {"success": False}
        if mode == "shape":
            return 200, {"unexpected": "payload"}
        return status, payload

    def _emails(self):
        mode = self.behaviour.get("emails", "ok")
        if mode == "ok":
            return 200, {"success": True, "messages": []}
        if mode == "unauthorized":
            return 200, {"success": False, "messages": []}
        if mode == "timeout":
            raise self.module.ContractTimeout("read timeout")
        if mode == "unreachable":
            raise self.module.EndpointUnreachable("refused")
        if mode == "status":
            return 500, {"success": False}
        if mode == "shape":
            return 200, {"unexpected": "payload"}
        raise AssertionError(f"unknown emails behaviour {mode!r}")

    def count(self, fragment: str) -> int:
        return sum(1 for _method, url, _timeout in self.calls if fragment in url)


def _write_secrets(directory: Path) -> tuple[Path, Path]:
    runtime = directory / "runtime.env"
    runtime.write_text(
        "LOGIN_PASSWORD=synthetic-login-password\nSECRET_KEY=synthetic-secret\n",
        encoding="utf-8",
    )
    config = directory / "config.json"
    config.write_text(
        json.dumps({"outlookemail_api_key": "synthetic-api-key"}), encoding="utf-8"
    )
    return runtime, config


class ContractSmokeTailLatencyTests(unittest.TestCase):
    """P0-a: probe budget must not false-fail a healthy deployment."""

    def setUp(self):
        self.module = _load_contract_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime, self.config = _write_secrets(self.tmp)
        # The retry sleep is real; make it instant so the suite stays fast.
        sleep_patcher = unittest.mock.patch.object(self.module.time, "sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _run_main(self, behaviour: str, **kwargs) -> int:
        self.module._request = _FakeTransport(self.module, **behaviour)
        argv = [
            "--base",
            "http://synthetic.invalid:5000",
            "--runtime-env",
            str(self.runtime),
            "--config",
            str(self.config),
        ]
        for key, value in kwargs.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return self.module.main(argv)

    def _transport(self, behaviour: str):
        transport = _FakeTransport(self.module, **behaviour)
        self.module._request = transport
        return transport

    def test_failure_classes_are_public_and_distinguishable(self):
        self.assertTrue(issubclass(self.module.ContractTimeout, self.module.ContractError))
        self.assertTrue(issubclass(self.module.EndpointUnreachable, self.module.ContractError))
        self.assertTrue(callable(self.module.probe_external_emails))

    def test_tail_timeout_is_unverified_not_failed(self):
        self.assertEqual(self._run_main({"emails": "timeout"}), 0)

    def test_tail_retry_is_bounded_to_exactly_one_extra_attempt(self):
        transport = self._transport({"emails": "timeout"})
        self.assertEqual(
            self.module.main(
                [
                    "--base", "http://synthetic.invalid:5000",
                    "--runtime-env", str(self.runtime),
                    "--config", str(self.config),
                ]
            ),
            0,
        )
        self.assertEqual(transport.count("/api/external/emails"), 2)
        # Strict probes are never retried.
        self.assertEqual(transport.count("/api/extension/login"), 1)
        self.assertEqual(transport.count("/api/external/accounts"), 1)
        self.assertEqual(sum(1 for _m, url, _t in transport.calls if url.endswith("/")), 1)

    def test_emails_budget_is_independent_and_strict_default_is_untouched(self):
        transport = self._transport({"emails": "ok"})
        self.module.main(
            [
                "--base", "http://synthetic.invalid:5000",
                "--runtime-env", str(self.runtime),
                "--config", str(self.config),
            ]
        )
        budgets = [timeout for _m, _url, timeout in transport.calls]
        self.assertEqual(len(budgets), 4)
        self.assertEqual(budgets[:3], [8.0, 8.0, 8.0], "strict probes keep the 8s budget")
        self.assertGreaterEqual(budgets[3], 20.0)
        self.assertLessEqual(budgets[3], 30.0)
        self.assertGreater(budgets[3], budgets[0])

    def test_emails_budget_flag_overrides_the_tail_budget(self):
        transport = self._transport({"emails": "ok"})
        self.module.main(
            [
                "--base", "http://synthetic.invalid:5000",
                "--runtime-env", str(self.runtime),
                "--config", str(self.config),
                "--emails-timeout", "20",
            ]
        )
        self.assertEqual([t for _m, _u, t in transport.calls][3], 20.0)

    def test_unreachable_endpoint_still_fails_closed(self):
        self.assertEqual(self._run_main({"emails": "unreachable"}), 1)

    def test_contract_shape_violation_fails_and_is_not_retried(self):
        transport = self._transport({"emails": "shape"})
        self.assertEqual(
            self.module.main(
                [
                    "--base", "http://synthetic.invalid:5000",
                    "--runtime-env", str(self.runtime),
                    "--config", str(self.config),
                ]
            ),
            1,
        )
        self.assertEqual(transport.count("/api/external/emails"), 1, "no retry on contract errors")

    def test_http_status_violation_still_fails(self):
        self.assertEqual(self._run_main({"emails": "status"}), 1)

    def test_strict_probes_fail_closed_on_timeout(self):
        for probe in ("root", "login", "accounts"):
            with self.subTest(probe=probe):
                self.assertEqual(
                    self._run_main({probe: "timeout", "emails": "ok"}),
                    1,
                    f"{probe} must fail closed",
                )

    def test_strict_probes_fail_closed_on_unreachable(self):
        for probe in ("root", "login", "accounts"):
            with self.subTest(probe=probe):
                self.assertEqual(self._run_main({probe: "unreachable", "emails": "ok"}), 1)

    def test_strict_probes_are_never_retried_on_timeout(self):
        for probe, fragment in (
            ("root", "/"),
            ("login", "/api/extension/login"),
            ("accounts", "/api/external/accounts"),
        ):
            with self.subTest(probe=probe):
                transport = self._transport({probe: "timeout", "emails": "ok"})
                self.assertEqual(
                    self.module.main(
                        [
                            "--base", "http://synthetic.invalid:5000",
                            "--runtime-env", str(self.runtime),
                            "--config", str(self.config),
                        ]
                    ),
                    1,
                )
                self.assertEqual(transport.count(fragment), 1, f"{probe} must not retry")

    def test_upstream_business_unavailable_is_not_a_contract_failure(self):
        self.assertEqual(self._run_main({"emails": "unauthorized"}), 0)

    def test_cli_defaults_match_the_agreed_budget(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(text, r"--timeout[^\n]*default=8\.0")
        self.assertIn("--emails-timeout", text)
        self.assertIn("--emails-retries", text)
        self.assertRegex(text, r"EMAILS_TAIL_BUDGET_SECONDS\s*=\s*2[0-9](?:\.0)?")
        self.assertRegex(text, r"MAX_EMAILS_RETRIES\s*=\s*1")


class GateLFailClosedTests(unittest.TestCase):
    """P0-b: rendered Gate L must equal the expected value, default 1."""

    def setUp(self):
        self.assertTrue(GATE_L.is_file(), "deploy/check-gate-l.sh must exist")
        self.assertTrue(os.access(GATE_L, os.X_OK), "check-gate-l.sh must be executable")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _compose(self, rendered_value):
        env_block = (
            "      TZ: UTC"
            if rendered_value is None
            else f'      SUB2API_GATE_L_MAX_COUNT: "{rendered_value}"'
        )
        path = self.tmp / "compose.yaml"
        path.write_text(
            "services:\n"
            "  sub2api-native:\n"
            "    image: fixture:local\n"
            "    environment:\n"
            f"{env_block}\n",
            encoding="utf-8",
        )
        return path

    def _run(self, *args):
        # The assertion's own value is a *rendered* ceiling, so the behavioural
        # cases need real Compose rendering. Without it, skip rather than let an
        # environment gap look like a broken gate; the static cases below always
        # run.
        if shutil.which("docker") is None or shutil.which("jq") is None:
            self.skipTest("docker compose and jq are required to render fixtures")
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, check=False
        )
        if probe.returncode != 0:
            self.skipTest("docker compose v2 unavailable")
        return subprocess.run(
            [str(GATE_L), *args], capture_output=True, text=True, check=False
        )

    def test_rendered_one_passes_with_default_expectation(self):
        result = self._run(str(self._compose("1")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rendered_override_above_one_fails(self):
        result = self._run(str(self._compose("1000")))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Gate L", result.stdout + result.stderr)

    def test_missing_rendered_value_fails_closed(self):
        self.assertNotEqual(self._run(str(self._compose(None))).returncode, 0)

    def test_empty_rendered_value_fails_closed(self):
        self.assertNotEqual(self._run(str(self._compose(""))).returncode, 0)

    def test_non_numeric_value_fails_closed(self):
        self.assertNotEqual(self._run(str(self._compose("true"))).returncode, 0)

    def test_zero_fails_closed(self):
        self.assertNotEqual(self._run(str(self._compose("0"))).returncode, 0)

    def test_raising_the_limit_needs_the_acceptance_flag(self):
        result = self._run("--expected", "1000", str(self._compose("1000")))
        self.assertNotEqual(result.returncode, 0, "--acceptance-ack is required")
        self.assertIn("--acceptance-ack", result.stdout + result.stderr)

    def test_acceptance_flag_permits_a_raised_limit(self):
        result = self._run(
            "--expected", "1000", "--acceptance-ack", str(self._compose("1000"))
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_acceptance_flag_cannot_mask_a_mismatch(self):
        result = self._run(
            "--expected", "1000", "--acceptance-ack", str(self._compose("5"))
        )
        self.assertNotEqual(result.returncode, 0)

    def test_unreadable_compose_fails_closed(self):
        self.assertNotEqual(self._run(str(self.tmp / "missing.yaml")).returncode, 0)

    def test_script_is_read_only_and_never_names_credentials(self):
        text = GATE_L.read_text(encoding="utf-8")
        offenders = re.findall(
            r"docker[^\n#]*\b(create|up|down|start|stop|restart|build|pull|push|"
            r"rm|rmi|run|exec|prune|tag)\b",
            text,
        )
        self.assertEqual(offenders, [], "Gate L assertion must stay read-only")
        for token in ("LOGIN_PASSWORD", "SECRET_KEY", "outlookemail.env"):
            self.assertNotIn(token, text)

    def test_update_script_gates_before_build_and_before_recreate(self):
        text = (REPO_ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
        build_at = text.index("compose.yaml build")
        up_at = text.index("docker-compose.yml up -d")
        gate_calls = [m.start() for m in re.finditer(r"check-gate-l\.sh", text)]
        self.assertGreaterEqual(len(gate_calls), 2)
        self.assertLess(gate_calls[0], build_at, "must gate before build")
        self.assertTrue(
            any(build_at < pos < up_at for pos in gate_calls),
            "must re-assert after build and before recreate",
        )
        self.assertIn("SUB2API_GATE_L_EXPECTED", text)


class TrackedGeneratedFrontendTests(unittest.TestCase):
    """P1: front/dist is generated output, never tracked truth."""

    def test_gitignore_permanently_ignores_front_dist(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/front/dist/", text)

    def test_docker_build_context_still_ignores_host_dist(self):
        for name in (".dockerignore", "deploy/Dockerfile.dockerignore"):
            with self.subTest(ignore=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("front/dist", text)

    def test_front_dist_is_not_tracked(self):
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--", "front/dist"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", "front/dist must not be tracked")


class UnexpandedGlobRegressionTests(unittest.TestCase):
    """P2: never hand an unexpanded ``*`` to a file-creating command.

    Executable record of the original defect: in a directory containing no
    ``.db`` file, ``sqlite3 'data/*.db' 'PRAGMA integrity_check;'`` creates a
    0-byte file literally named ``*.db``, prints ``ok``, and exits 0.
    """

    def test_unexpanded_glob_reproduces_the_artifact(self):
        sqlite3_bin = shutil.which("sqlite3")
        if sqlite3_bin is None:
            self.skipTest("sqlite3 CLI unavailable")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "data").mkdir()
        result = subprocess.run(
            [sqlite3_bin, "data/*.db", "PRAGMA integrity_check;"],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        stray = tmp / "data" / "*.db"
        if not stray.exists():
            self.skipTest("sqlite3 no longer creates a literal-glob file")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(stray.stat().st_size, 0)

    def test_no_command_passes_a_glob_to_file_writers(self):
        pattern = re.compile(
            r"\b(?:sqlite3|touch|cp|mv|tee|dd|scp|rsync)\b[^\n|;&]*?\*\.(?:db|sqlite3?)(?![\w-])"
        )
        roots = [
            REPO_ROOT / "deploy",
            REPO_ROOT / "scripts",
            REPO_ROOT / "docker",
            REPO_ROOT / ".github",
        ]
        offenders: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(offenders, [])

    def test_runbook_documents_the_safe_invocation(self):
        text = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
        self.assertIn("integrity_check", text)
        self.assertRegex(
            text,
            r"unexpanded|never pass a glob|glob into sqlite",
            msg="deploy/README.md must warn about the unexpanded-glob artifact",
        )

    def test_repository_data_tree_has_no_literal_glob_named_files(self):
        hits = [p.relative_to(REPO_ROOT) for p in REPO_ROOT.glob("data/*") if set(p.name) & set("*?[]")]
        self.assertEqual(hits, [], "a runtime command is creating literal-glob files")


if __name__ == "__main__":
    unittest.main()
