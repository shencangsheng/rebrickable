import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
import PIL.Image  # noqa: F401 — required at runtime; ensures PyInstaller bundles Pillow
from openpyxl.drawing.image import Image as OpenpyxlImage
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from cookie_store import (
    cookies_to_session,
    has_session_cookie,
    load_cookies,
    save_cookies,
)
from paths import data_dir

logger = logging.getLogger(__name__)

BROWSER_START_TIMEOUT_SECONDS = 90
CLOUDFLARE_RETRY_MAX = 10
CLOUDFLARE_RETRY_DELAY_SECONDS = 3
CLOUDFLARE_PAGE_WAIT_SECONDS = 90
PERSISTENT_PROFILE_NAME = "chrome"

REBRICKABLE_BASE = "https://rebrickable.com"
LOGIN_URL = f"{REBRICKABLE_BASE}/login/"
INVENTORY_URL_TEMPLATE = (
    "https://rebrickable.com/inventory/{inventory_id}/parts/?format=table"
)


def build_inventory_url(inventory_id: str) -> str:
    return INVENTORY_URL_TEMPLATE.format(inventory_id=inventory_id.strip())


def create_requests_session(cookies: list[dict] | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    if cookies:
        cookies_to_session(session, cookies)
    return session


def is_logged_in(cookies: list[dict] | None = None) -> bool:
    cookies = cookies or load_cookies()
    return bool(cookies) and has_session_cookie(cookies)


def _configure_selenium_for_frozen() -> None:
    """Point selenium-manager at the PyInstaller bundle on Windows."""
    if not getattr(sys, "frozen", False):
        return
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if not bundle_dir:
        return
    if sys.platform == "win32":
        manager = os.path.join(
            bundle_dir,
            "selenium",
            "webdriver",
            "common",
            "windows",
            "selenium-manager.exe",
        )
    elif sys.platform == "darwin":
        manager = os.path.join(
            bundle_dir,
            "selenium",
            "webdriver",
            "common",
            "macos",
            "selenium-manager",
        )
    else:
        manager = os.path.join(
            bundle_dir,
            "selenium",
            "webdriver",
            "common",
            "linux",
            "selenium-manager",
        )
    if os.path.isfile(manager):
        os.environ["SE_MANAGER_PATH"] = manager


def _bundled_driver_path(browser: str) -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if not bundle_dir:
        return None
    if sys.platform == "win32":
        names = {"chrome": "chromedriver.exe", "edge": "msedgedriver.exe"}
    else:
        names = {"chrome": "chromedriver", "edge": "msedgedriver"}
    name = names.get(browser)
    if not name:
        return None
    path = os.path.join(bundle_dir, "drivers", name)
    return path if os.path.isfile(path) else None


def _run_with_timeout(fn: Callable[[], object], timeout_seconds: int, message: str):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            raise RuntimeError(message) from exc


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _first_existing(paths: list[str]) -> str | None:
    for path in paths:
        expanded = _expand(path)
        if os.path.isfile(expanded):
            return expanded
    return None


def _browser_candidates() -> list[tuple[str, str | None]]:
    if sys.platform == "win32":
        chrome = _first_existing(
            [
                r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
            ]
        )
        edge = _first_existing(
            [
                r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            ]
        )
        candidates: list[tuple[str, str | None]] = []
        if chrome:
            candidates.append(("chrome", chrome))
        if edge:
            candidates.append(("edge", edge))
        if not candidates:
            candidates.append(("edge", None))
        return candidates

    chrome = _first_existing(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
        ]
    )
    return [("chrome", chrome)] if chrome else [("chrome", None)]


def _persistent_browser_profile() -> str:
    path = data_dir() / "browser_profiles" / PERSISTENT_PROFILE_NAME
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _make_browser_profile(prefix: str, *, persistent: bool = False) -> str:
    if persistent:
        return _persistent_browser_profile()
    root = data_dir() / "browser_profiles"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=root)


def _should_cleanup_profile(profile_dir: str | None) -> bool:
    if not profile_dir:
        return False
    return os.path.normpath(profile_dir) != os.path.normpath(_persistent_browser_profile())


def _cleanup_browser_profile(profile_dir: str | None) -> None:
    if _should_cleanup_profile(profile_dir) and os.path.isdir(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)


def _apply_common_browser_options(
    options,
    *,
    headless: bool,
    offscreen: bool,
    visible: bool,
    profile_dir: str | None = None,
) -> None:
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        options.add_argument("--headless=new")
    elif visible:
        options.add_argument("--start-maximized")
        options.add_argument("--new-window")
        if sys.platform == "win32":
            options.add_argument("--disable-gpu")
    if offscreen:
        options.add_argument("--window-position=-2400,-2400")
        options.add_argument("--window-size=1280,900")
    elif not headless and not visible:
        options.add_argument("--window-size=1280,900")


def _stealth(driver) -> None:
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": (
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        },
    )


def _format_browser_error(browser: str, exc: Exception) -> str:
    name = "Chrome" if browser == "chrome" else "Edge"
    detail = str(exc).strip() or exc.__class__.__name__
    if "cannot find chrome binary" in detail.lower():
        return f"找不到 {name} 可执行文件，请确认浏览器已正确安装。"
    if "session not created" in detail.lower() or "version" in detail.lower():
        return f"{name} 与驱动版本不匹配：{detail}。请更新 Chrome 到最新版后重试。"
    if "user data directory" in detail.lower():
        return f"{name} 配置目录被占用：{detail}。请关闭所有 Chrome 窗口后重试。"
    if "启动浏览器超时" in detail:
        return detail
    return f"无法启动 {name}：{detail}"


def _browser_start_timeout_message() -> str:
    log_file = data_dir() / "app.log"
    return (
        f"启动浏览器超时（{BROWSER_START_TIMEOUT_SECONDS} 秒）。"
        "常见原因：网络无法下载 ChromeDriver（请检查代理/VPN 或暂时关闭 Clash 等代理）、"
        "杀毒软件拦截、或 Chrome 未正确安装。"
        f"详细日志：{log_file}"
    )


def _raise_browser_window(driver) -> None:
    try:
        driver.maximize_window()
    except WebDriverException:
        pass
    try:
        driver.execute_script("window.focus();")
    except WebDriverException:
        pass

    if sys.platform != "win32":
        return

    try:
        import ctypes

        user32 = ctypes.windll.user32
        chrome_windows: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(128)
                if user32.GetClassNameW(hwnd, buf, 128):
                    if buf.value == "Chrome_WidgetWin_1":
                        chrome_windows.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        if chrome_windows:
            target = chrome_windows[-1]
            user32.ShowWindow(target, 9)
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
    except Exception:
        pass


def _start_webdriver(
    browser: str,
    binary: str | None,
    *,
    headless: bool,
    offscreen: bool,
    visible: bool,
    profile_dir: str,
):
    bundled_driver = _bundled_driver_path(browser)
    if bundled_driver:
        logger.info("Using bundled %s driver: %s", browser, bundled_driver)
        os.environ["SE_OFFLINE"] = "true"
    else:
        os.environ.pop("SE_OFFLINE", None)
        logger.info("No bundled driver for %s; selenium-manager will resolve one", browser)

    if browser == "edge":
        options = webdriver.EdgeOptions()
        if binary:
            options.binary_location = binary
            logger.info("Edge binary: %s", binary)
        _apply_common_browser_options(
            options,
            headless=headless,
            offscreen=offscreen,
            visible=visible,
            profile_dir=profile_dir,
        )
        service = EdgeService(bundled_driver) if bundled_driver else None
        return webdriver.Edge(service=service, options=options)

    options = webdriver.ChromeOptions()
    if binary:
        options.binary_location = binary
        logger.info("Chrome binary: %s", binary)
    _apply_common_browser_options(
        options,
        headless=headless,
        offscreen=offscreen,
        visible=visible,
        profile_dir=profile_dir,
    )
    service = ChromeService(bundled_driver) if bundled_driver else None
    return webdriver.Chrome(service=service, options=options)


def _create_browser_driver(
    headless: bool = False,
    offscreen: bool = False,
    visible: bool = False,
    profile_prefix: str = "session-",
    persistent_profile: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[object, str | None]:
    _configure_selenium_for_frozen()
    errors: list[str] = []
    profile_dir = _make_browser_profile(profile_prefix, persistent=persistent_profile)
    timeout_message = _browser_start_timeout_message()

    for browser, binary in _browser_candidates():
        browser_name = "Chrome" if browser == "chrome" else "Edge"
        if on_progress:
            on_progress(f"正在启动 {browser_name}，请稍候…")
        logger.info(
            "Starting %s (binary=%s, headless=%s, visible=%s)",
            browser,
            binary,
            headless,
            visible,
        )
        try:
            driver = _run_with_timeout(
                lambda browser=browser, binary=binary: _start_webdriver(
                    browser,
                    binary,
                    headless=headless,
                    offscreen=offscreen,
                    visible=visible,
                    profile_dir=profile_dir,
                ),
                BROWSER_START_TIMEOUT_SECONDS,
                timeout_message,
            )
            _stealth(driver)
            if visible and not headless and not offscreen:
                _raise_browser_window(driver)
            logger.info("%s started successfully", browser_name)
            return driver, profile_dir
        except Exception as exc:
            logger.exception("Failed to start %s", browser_name)
            errors.append(_format_browser_error(browser, exc))

    _cleanup_browser_profile(profile_dir)
    log_file = data_dir() / "app.log"
    raise RuntimeError(
        "无法启动浏览器。\n"
        + "\n".join(errors)
        + f"\n详细日志：{log_file}"
    )


def _inject_cookies(driver, cookies: list[dict]) -> None:
    driver.get(REBRICKABLE_BASE)
    for cookie in cookies:
        payload = {
            key: cookie[key]
            for key in ("name", "value", "path", "domain", "secure", "expiry")
            if key in cookie
        }
        if cookie.get("httpOnly"):
            payload["httpOnly"] = True
        same_site = cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            payload["sameSite"] = same_site
        try:
            driver.add_cookie(payload)
        except Exception:
            pass


def _wait_for_login(driver, wait_seconds: int) -> list[dict]:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        cookies = driver.get_cookies()
        if has_session_cookie(cookies):
            return cookies
        time.sleep(2)
    raise RuntimeError(
        f"在 {wait_seconds} 秒内未检测到登录成功，请确认已在浏览器中完成登录。"
    )


def login_with_browser(
    wait_seconds: int = 300,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    driver = None
    profile_dir = None
    try:
        driver, profile_dir = _create_browser_driver(
            headless=False,
            visible=True,
            profile_prefix="login-",
            persistent_profile=True,
            on_progress=on_progress,
        )
        if on_progress:
            on_progress("浏览器已打开，请在弹出的窗口中登录 Rebrickable")

        driver.get(LOGIN_URL)
        _raise_browser_window(driver)
        cookies = _wait_for_login(driver, wait_seconds)
        if not has_session_cookie(cookies):
            raise RuntimeError("未能获取登录 Cookie，请确认已在浏览器中完成登录。")
        save_cookies(cookies)
        return cookies
    finally:
        if driver is not None:
            driver.quit()
        _cleanup_browser_profile(profile_dir)


def _is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "challenges.cloudflare.com" in lowered
        or "just a moment" in lowered
        or "请稍候" in html
    )


class CloudflareBlockedError(RuntimeError):
    """Raised when the page is blocked by Cloudflare."""


def _ensure_session_in_browser(driver, cookies: list[dict]) -> None:
    driver.get(REBRICKABLE_BASE)
    if has_session_cookie(driver.get_cookies()):
        return
    _inject_cookies(driver, cookies)
    driver.get(REBRICKABLE_BASE)


def _wait_for_inventory_page(driver, wait_seconds: int = CLOUDFLARE_PAGE_WAIT_SECONDS) -> None:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if "login" in driver.current_url.lower():
            raise PermissionError("Cookie 已失效，请重新登录。")
        if _is_cloudflare_challenge(driver.page_source):
            logger.info("Cloudflare challenge detected, waiting for it to clear...")
            time.sleep(2)
            continue
        try:
            WebDriverWait(driver, min(5, max(1, deadline - time.time()))).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tr"))
            )
            return
        except TimeoutException:
            time.sleep(1)
    if _is_cloudflare_challenge(driver.page_source):
        raise CloudflareBlockedError("被 Cloudflare 拦截。")
    raise ValueError(
        "未能在网页中解析到表格，请确认 inventory 编号正确且已登录。"
    )


def _fetch_inventory_html_once(url: str, cookies: list[dict]) -> str:
    # Reuse the same persistent profile as login so cf_clearance survives across runs.
    driver, profile_dir = _create_browser_driver(
        headless=False,
        offscreen=True,
        profile_prefix="export-",
        persistent_profile=True,
    )
    try:
        _ensure_session_in_browser(driver, cookies)
        _wait_for_inventory_page(driver)
        driver.get(url)
        _wait_for_inventory_page(driver)
        return driver.page_source
    finally:
        driver.quit()
        _cleanup_browser_profile(profile_dir)


def fetch_inventory_html(url: str, cookies: list[dict]) -> str:
    last_error: Exception | None = None
    for attempt in range(1, CLOUDFLARE_RETRY_MAX + 1):
        try:
            return _fetch_inventory_html_once(url, cookies)
        except CloudflareBlockedError as exc:
            last_error = exc
            logger.warning(
                "Cloudflare blocked export (attempt %s/%s), closing browser and retrying",
                attempt,
                CLOUDFLARE_RETRY_MAX,
            )
            if attempt < CLOUDFLARE_RETRY_MAX:
                time.sleep(CLOUDFLARE_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"被 Cloudflare 拦截，已自动重试 {CLOUDFLARE_RETRY_MAX} 次仍未成功，请稍后再试。"
    ) from last_error


def download_excel(
    inventory_id: str,
    output_file: str = "Lego_Parts.xlsx",
    cookies: list[dict] | None = None,
) -> str:
    cookies = cookies or load_cookies()
    if not cookies:
        raise PermissionError("尚未登录，请先完成登录。")

    url = build_inventory_url(inventory_id)
    html_source = fetch_inventory_html(url, cookies)
    session = create_requests_session(cookies)

    soup = BeautifulSoup(html_source, "html.parser")
    table = soup.find("table")
    if not table:
        raise ValueError("未能在网页中解析到表格，请确认 inventory 编号正确且已登录。")

    rows = table.find_all("tr")

    wb = Workbook()
    ws = wb.active
    ws.title = "Lego Parts"

    headers_text = [th.text.strip() for th in rows[0].find_all(["th", "td"])]
    ws.append(headers_text)
    ws.column_dimensions["A"].width = 12

    for tr in rows[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue

        row_num = ws.max_row + 1
        row_data = [td.text.strip() for td in tds]
        row_data[0] = ""
        ws.append(row_data)
        ws.row_dimensions[row_num].height = 60

        img_tag = tds[0].find("img")
        img_url = None
        if img_tag:
            for attr in ["src", "data-src", "data-original"]:
                val = img_tag.get(attr, "")
                if val.startswith("http") or val.startswith("//"):
                    img_url = val if val.startswith("http") else "https:" + val
                    break

        if img_url:
            try:
                img_res = session.get(img_url, timeout=5)
                if img_res.status_code == 200:
                    img = OpenpyxlImage(BytesIO(img_res.content))
                    img.width, img.height = 70, 70
                    ws.add_image(img, f"A{row_num}")
            except Exception as exc:
                logger.warning("Failed to embed image %s: %s", img_url, exc)

    wb.save(output_file)
    return output_file
