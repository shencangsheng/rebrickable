import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.drawing.image import Image as OpenpyxlImage
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from cookie_store import (
    cookies_to_session,
    has_session_cookie,
    load_cookies,
    save_cookies,
)
from paths import data_dir

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


def _make_browser_profile(prefix: str) -> str:
    root = data_dir() / "browser_profiles"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=root)


def _cleanup_browser_profile(profile_dir: str | None) -> None:
    if profile_dir and os.path.isdir(profile_dir):
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
    return f"无法启动 {name}：{detail}"


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


def _create_browser_driver(
    headless: bool = False,
    offscreen: bool = False,
    visible: bool = False,
    profile_prefix: str = "session-",
) -> tuple[object, str | None]:
    _configure_selenium_for_frozen()
    errors: list[str] = []
    profile_dir = _make_browser_profile(profile_prefix)

    for browser, binary in _browser_candidates():
        try:
            if browser == "edge":
                options = webdriver.EdgeOptions()
                if binary:
                    options.binary_location = binary
                _apply_common_browser_options(
                    options,
                    headless=headless,
                    offscreen=offscreen,
                    visible=visible,
                    profile_dir=profile_dir,
                )
                driver = webdriver.Edge(options=options)
            else:
                options = webdriver.ChromeOptions()
                if binary:
                    options.binary_location = binary
                _apply_common_browser_options(
                    options,
                    headless=headless,
                    offscreen=offscreen,
                    visible=visible,
                    profile_dir=profile_dir,
                )
                driver = webdriver.Chrome(options=options)

            _stealth(driver)
            if visible and not headless and not offscreen:
                _raise_browser_window(driver)
            return driver, profile_dir
        except Exception as exc:
            errors.append(_format_browser_error(browser, exc))

    _cleanup_browser_profile(profile_dir)
    raise RuntimeError(
        "无法启动浏览器，请查看后台控制台窗口中的详细错误。\n" + "\n".join(errors)
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
    if on_progress:
        on_progress("正在启动 Chrome，请稍候…")

    driver = None
    profile_dir = None
    try:
        driver, profile_dir = _create_browser_driver(
            headless=False, visible=True, profile_prefix="login-"
        )
        if on_progress:
            on_progress("Chrome 已打开，请在弹出的窗口中登录 Rebrickable")

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


def fetch_inventory_html(url: str, cookies: list[dict]) -> str:
    # Cloudflare blocks headless Chrome; use an off-screen visible window instead.
    driver, profile_dir = _create_browser_driver(
        headless=False, offscreen=True, profile_prefix="export-"
    )
    try:
        _inject_cookies(driver, cookies)
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tr"))
            )
        except TimeoutException:
            if "login" in driver.current_url.lower():
                raise PermissionError("Cookie 已失效，请重新登录。")
            if _is_cloudflare_challenge(driver.page_source):
                raise RuntimeError(
                    "被 Cloudflare 拦截，请重新登录后再试。"
                )
            raise ValueError(
                "未能在网页中解析到表格，请确认 inventory 编号正确且已登录。"
            )
        return driver.page_source
    finally:
        driver.quit()
        _cleanup_browser_profile(profile_dir)


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
            except Exception:
                pass

    wb.save(output_file)
    return output_file
