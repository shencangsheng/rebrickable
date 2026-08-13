import logging
import re
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
import PIL.Image  # noqa: F401 — required at runtime; ensures PyInstaller bundles Pillow
from openpyxl.drawing.image import Image as OpenpyxlImage

from cookie_store import cookies_to_session, load_cookies

logger = logging.getLogger(__name__)

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
                "Chrome/131.0.0.0 Safari/537.36"
            )
        }
    )
    if cookies:
        cookies_to_session(session, cookies)
    return session


def is_cloudflare_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "challenges.cloudflare.com" in lowered
        or "just a moment" in lowered
        or "请稍候" in html
        or "正在验证" in html
    )


def download_excel_from_html(
    html_source: str,
    output_file: str = "Lego_Parts.xlsx",
    cookies: list[dict] | None = None,
) -> str:
    if is_cloudflare_challenge(html_source):
        raise RuntimeError("页面仍在进行安全验证，请等验证结束后再导出。")

    cookies = cookies or load_cookies()
    session = create_requests_session(cookies)

    soup = BeautifulSoup(html_source, "html.parser")
    table = soup.find("table")
    if not table:
        title = soup.find("title")
        title_text = title.get_text(" ", strip=True).lower() if title else ""
        if "login" in title_text:
            raise PermissionError("尚未登录或登录已失效，请先在页面中登录。")
        raise ValueError(
            "未能在网页中解析到零件表格。请打开套装、MOC 或零件清单后再导出。"
        )

    rows = table.find_all("tr")
    if len(rows) < 2:
        raise ValueError("零件表格是空的，请确认当前页面包含零件清单。")

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


def safe_file_stem(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" ._")
    return cleaned[:80] or "parts"
