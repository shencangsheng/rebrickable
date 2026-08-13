"""CLI entry — for the desktop app, run: npm start in electron/"""

from scraper import build_inventory_url

TARGET_INVENTORY_ID = "340344"


if __name__ == "__main__":
    print("请使用 Electron 桌面应用: cd electron && npm start")
    print(f"示例 URL: {build_inventory_url(TARGET_INVENTORY_ID)}")
