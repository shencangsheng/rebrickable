import os
import threading

from flask import Flask, jsonify, render_template, request, send_file

from cookie_store import clear_cookies, load_cookies
from meta import APP_AUTHOR, APP_NAME
from paths import exports_dir, is_frozen, templates_dir
from scraper import (
    build_inventory_url,
    download_excel,
    is_logged_in,
    login_with_browser,
)

app = Flask(__name__, template_folder=str(templates_dir()))

login_lock = threading.Lock()
login_state = {"running": False, "message": ""}


@app.context_processor
def inject_app_meta():
    return {"app_author": APP_AUTHOR, "app_name": APP_NAME}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    cookies = load_cookies()
    if not cookies:
        return jsonify({"logged_in": False, "message": "尚未登录"})

    logged_in = is_logged_in(cookies)
    return jsonify(
        {
            "logged_in": logged_in,
            "message": "已登录" if logged_in else "Cookie 已失效，请重新登录",
            "cookie_count": len(cookies),
        }
    )


@app.route("/api/login", methods=["POST"])
def login():
    if login_state["running"]:
        return jsonify({"ok": False, "message": "登录流程正在进行中，请稍候"}), 409

    def run_login():
        login_state["running"] = True
        login_state["message"] = "正在启动浏览器，请稍候…"

        def on_progress(message: str) -> None:
            login_state["message"] = message

        try:
            login_with_browser(wait_seconds=300, on_progress=on_progress)
            login_state["message"] = "登录成功，Cookie 已保存"
        except Exception as exc:
            login_state["message"] = str(exc)
        finally:
            login_state["running"] = False

    thread = threading.Thread(target=run_login, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "正在打开浏览器，请完成登录"})


@app.route("/api/login/progress")
def login_progress():
    return jsonify(
        {
            "running": login_state["running"],
            "message": login_state["message"],
        }
    )


@app.route("/api/cookies", methods=["DELETE"])
def delete_cookies():
    clear_cookies()
    return jsonify({"ok": True, "message": "Cookie 已清除"})


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    inventory_id = str(data.get("inventory_id", "")).strip()
    if not inventory_id or not inventory_id.isdigit():
        return jsonify({"ok": False, "message": "请输入有效的 inventory 编号（纯数字）"}), 400

    cookies = load_cookies()
    if not cookies:
        return jsonify({"ok": False, "message": "尚未登录，请先登录"}), 401

    if not is_logged_in(cookies):
        return jsonify({"ok": False, "message": "Cookie 已失效，请重新登录"}), 401

    output_file = exports_dir() / f"inventory_{inventory_id}.xlsx"

    try:
        download_excel(inventory_id, str(output_file), cookies)
    except PermissionError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 401
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"导出失败: {exc}"}), 500

    return jsonify(
        {
            "ok": True,
            "message": "导出成功",
            "download_url": f"/api/download/{inventory_id}",
            "preview_url": build_inventory_url(inventory_id),
        }
    )


@app.route("/api/download/<inventory_id>")
def download(inventory_id: str):
    if not inventory_id.isdigit():
        return jsonify({"ok": False, "message": "无效的编号"}), 400

    output_file = exports_dir() / f"inventory_{inventory_id}.xlsx"
    if not output_file.exists():
        return jsonify({"ok": False, "message": "文件不存在，请先导出"}), 404

    return send_file(
        output_file,
        as_attachment=True,
        download_name=f"inventory_{inventory_id}.xlsx",
    )


if __name__ == "__main__":
    debug = not is_frozen() and os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5050, debug=debug, use_reloader=False)
