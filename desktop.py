"""Desktop entry — starts Flask without debug mode."""

from werkzeug.serving import make_server

from app import app

HOST = "127.0.0.1"
PORT = 5050


if __name__ == "__main__":
    server = make_server(HOST, PORT, app, threaded=True)
    server.serve_forever()
