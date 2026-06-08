import time
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.drawing.image import Image as OpenpyxlImage
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from cookie_store import (
    cookies_to_session,
    has_session_cookie,
    load_cookies,
    save_cookies,
)

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


def _create_chrome_driver(
    headless: bool = False, offscreen: bool = False
) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")
    if offscreen:
        options.add_argument("--window-position=-2400,-2400")
    options.add_argument("--window-size=1280,900")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=options
    )
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def _inject_cookies(driver: webdriver.Chrome, cookies: list[dict]) -> None:
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


def _wait_for_login(driver: webdriver.Chrome, wait_seconds: int) -> list[dict]:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        cookies = driver.get_cookies()
        if has_session_cookie(cookies):
            return cookies
        time.sleep(2)
    raise RuntimeError(
        f"在 {wait_seconds} 秒内未检测到登录成功，请确认已在浏览器中完成登录。"
    )


def login_with_browser(wait_seconds: int = 300) -> list[dict]:
    driver = _create_chrome_driver(headless=False)

    try:
        driver.get(LOGIN_URL)
        cookies = _wait_for_login(driver, wait_seconds)
        if not has_session_cookie(cookies):
            raise RuntimeError("未能获取登录 Cookie，请确认已在浏览器中完成登录。")
        save_cookies(cookies)
        return cookies
    finally:
        driver.quit()


def _is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "challenges.cloudflare.com" in lowered
        or "just a moment" in lowered
        or "请稍候" in html
    )


def fetch_inventory_html(url: str, cookies: list[dict]) -> str:
    # Cloudflare blocks headless Chrome; use an off-screen visible window instead.
    driver = _create_chrome_driver(headless=False, offscreen=True)
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
