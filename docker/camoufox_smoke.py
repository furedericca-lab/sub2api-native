"""Launch the production Camoufox runtime and verify headed browser access."""

import glob
import os
import pwd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _use_runtime_identity() -> None:
    authorities = glob.glob("/tmp/xvfb-run.*/Xauthority")
    if authorities:
        os.environ["XAUTHORITY"] = authorities[0]
    if os.geteuid() == 0:
        account = pwd.getpwnam("app")
        os.environ["HOME"] = account.pw_dir
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)


def main() -> None:
    _use_runtime_identity()
    from backend.automation import session
    session.configure(
        get_proxies=lambda: {},
        is_debug=lambda: False,
        is_headless=lambda: False,
        get_locale=lambda: "en-US",
    )
    try:
        _, page = session.start_browser(geoip_override=False)
        page.raw_page.goto("data:text/html,<title>camoufox-ok</title><h1>ok</h1>")
        result = page.raw_page.evaluate(
                "() => ({title: document.title, webdriver: navigator.webdriver, width: screen.width})"
        )
        if result.get("title") != "camoufox-ok" or result.get("webdriver") is not False:
            raise RuntimeError(f"unexpected browser result: {result}")
        print(f"Camoufox headed smoke OK: {result}", flush=True)
    finally:
        session.stop_browser(force=True)


if __name__ == "__main__":
    main()
