"""Bootstrap data/config.json for the Docker runtime.

Invoked by docker/entrypoint.sh before the application starts. It only
creates a missing config; an existing config.json is never modified, so the
on-disk file stays the single configuration source.
"""

import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_EXAMPLE = "/app/config.example.json"

# Embedded OutlookEmail runs beside Sub2API in the same container.
DEFAULT_OUTLOOKEMAIL_API_BASE = "http://127.0.0.1:5000"


def _load_example_config(example_path):
    source = Path(example_path or os.environ.get("SUB2API_CONFIG_EXAMPLE", DEFAULT_CONFIG_EXAMPLE))
    return json.loads(source.read_text(encoding="utf-8"))


def bootstrap(target, example_path=None):
    """Create the target config file if missing. Returns a process exit code."""
    target = Path(target)
    if target.is_dir():
        print(f"[docker] 配置路径是目录而不是文件: {target}", file=sys.stderr)
        return 1
    if target.exists():
        return 0

    config = _load_example_config(example_path)
    config["browser_headless"] = False
    config["outlookemail_api_base"] = DEFAULT_OUTLOOKEMAIL_API_BASE

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[docker] 已创建容器默认配置: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(bootstrap(sys.argv[1]))
