import os

from flask import Flask, jsonify, render_template, request, send_file

from cookie_store import save_cookies
from logging_config import setup_logging
from meta import APP_AUTHOR, APP_NAME
from paths import exports_dir, is_frozen, templates_dir
from scraper import download_excel_from_html, safe_file_stem

setup_logging()

app = Flask(__name__, template_folder=str(templates_dir()))


@app.context_processor
def inject_app_meta():
    return {"app_author": APP_AUTHOR, "app_name": APP_NAME}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({"ok": True, "message": "ready"})


@app.route("/api/export", methods=["POST"])
def export():
    data = request.get_json(silent=True) or {}
    html = str(data.get("html") or "")
    cookies = data.get("cookies")
    file_stem = safe_file_stem(str(data.get("file_stem") or "parts"))

    if not html.strip():
        return jsonify({"ok": False, "message": "页面内容为空，请刷新后再试"}), 400

    if cookies:
        if not isinstance(cookies, list):
            return jsonify({"ok": False, "message": "Cookie 格式无效"}), 400
        save_cookies(cookies)

    output_file = exports_dir() / f"{file_stem}.xlsx"

    try:
        download_excel_from_html(html, str(output_file), cookies if isinstance(cookies, list) else None)
    except PermissionError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 401
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"导出失败: {exc}"}), 500

    return send_file(
        output_file,
        as_attachment=True,
        download_name=f"{file_stem}.xlsx",
    )


if __name__ == "__main__":
    debug = not is_frozen() and os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5050, debug=debug, use_reloader=False)
