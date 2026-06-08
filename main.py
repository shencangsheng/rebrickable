"""CLI entry — for web UI, run: python app.py"""

from scraper import build_inventory_url, download_excel, login_with_browser

TARGET_INVENTORY_ID = "340344"


if __name__ == "__main__":
    print("请使用 Web 界面: python app.py")
    print(f"示例 URL: {build_inventory_url(TARGET_INVENTORY_ID)}")
