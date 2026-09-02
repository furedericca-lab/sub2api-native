"""docker/config_bootstrap.py 只在配置缺失时创建；已有配置绝不修改。"""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "config_bootstrap.py"

spec = importlib.util.spec_from_file_location("config_bootstrap", SCRIPT)
config_bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config_bootstrap)

EXAMPLE_CONFIG = {
    "browser_headless": True,
    "outlookemail_api_base": "",
}


def _write_example(tmp_path):
    example = tmp_path / "config.example.json"
    example.write_text(json.dumps(EXAMPLE_CONFIG), encoding="utf-8")
    return example


def _run_bootstrap(target, example, monkeypatch):
    # 环境变量不再参与 seed；即使被误设也必须使用固定默认值。
    monkeypatch.setenv("LEGACY_OUTLOOKEMAIL_API_BASE", "http://192.0.2.5:5000")
    assert config_bootstrap.bootstrap(target, example_path=example) == 0


def test_missing_config_created_with_fixed_default(tmp_path, monkeypatch):
    example = _write_example(tmp_path)
    target = tmp_path / "data" / "config.json"

    _run_bootstrap(target, example, monkeypatch)

    config = json.loads(target.read_text(encoding="utf-8"))
    assert (
        config["outlookemail_api_base"]
        == config_bootstrap.DEFAULT_OUTLOOKEMAIL_API_BASE
    )
    assert config["browser_headless"] is False


def test_existing_config_never_touched(tmp_path, monkeypatch):
    example = _write_example(tmp_path)
    target = tmp_path / "config.json"
    existing = {
        "outlookemail_api_base": "http://192.0.2.9:5000",
        "browser_headless": True,
        "custom_key": "keep-me",
    }
    original = json.dumps(existing)
    target.write_text(original, encoding="utf-8")

    _run_bootstrap(target, example, monkeypatch)

    assert target.read_text(encoding="utf-8") == original
