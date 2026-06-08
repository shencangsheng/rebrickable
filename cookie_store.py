import json
import shutil

from paths import app_dir, data_dir

COOKIE_FILE = data_dir() / "cookies.json"
_LEGACY_COOKIE_FILE = app_dir() / "cookies.json"


def _migrate_legacy_cookies() -> None:
    if COOKIE_FILE.exists() or not _LEGACY_COOKIE_FILE.exists():
        return
    shutil.copy2(_LEGACY_COOKIE_FILE, COOKIE_FILE)


_migrate_legacy_cookies()


def save_cookies(cookies: list[dict]) -> None:
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def load_cookies() -> list[dict] | None:
    if not COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except (json.JSONDecodeError, OSError):
        return None


def clear_cookies() -> None:
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


def has_session_cookie(cookies: list[dict]) -> bool:
    return any(cookie.get("name") == "sessionid" for cookie in cookies)


def cookies_to_session(session, cookies: list[dict]) -> None:
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
