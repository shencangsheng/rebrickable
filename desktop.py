"""Desktop entry — starts Flask without debug mode."""

import threading
import webbrowser

from werkzeug.serving import make_server

from app import app

HOST = "127.0.0.1"
PORT = 5050


def run_server():
    server = make_server(HOST, PORT, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    webbrowser.open(f"http://{HOST}:{PORT}")
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
