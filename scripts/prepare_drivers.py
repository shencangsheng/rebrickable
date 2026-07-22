"""Download browser drivers as offline fallback for PyInstaller bundles.

Runtime prefers Selenium Manager to match the installed Chrome/Edge version;
these bundled drivers are only used when online resolution fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVERS_DIR = ROOT / "drivers"


def find_selenium_manager() -> Path:
    import selenium

    base = Path(selenium.__file__).parent / "webdriver" / "common"
    if sys.platform == "win32":
        return base / "windows" / "selenium-manager.exe"
    if sys.platform == "darwin":
        return base / "macos" / "selenium-manager"
    return base / "linux" / "selenium-manager"


def fetch_driver(browser: str) -> None:
    manager = find_selenium_manager()
    if not manager.is_file():
        raise FileNotFoundError(f"selenium-manager not found: {manager}")

    result = subprocess.run(
        [str(manager), "--browser", browser, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"selenium-manager failed for {browser} "
            f"(exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    payload = json.loads(result.stdout)
    driver_path = Path(payload["result"]["driver_path"])
    DRIVERS_DIR.mkdir(parents=True, exist_ok=True)
    dest = DRIVERS_DIR / driver_path.name
    shutil.copy2(driver_path, dest)
    print(f"Copied {browser} driver -> {dest}")


def main() -> None:
    browsers = ["chrome"]
    if sys.platform == "win32":
        browsers.append("edge")
    for browser in browsers:
        fetch_driver(browser)


if __name__ == "__main__":
    main()
