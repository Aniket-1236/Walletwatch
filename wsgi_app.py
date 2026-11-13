"""WSGI entry point for PythonAnywhere (or any WSGI host: gunicorn wsgi_app:application).

It serves the same pages and API as server.py by reusing its request handler, but reads the live
data from the worker (worker.py) through the shared database."""
import io
import os
import sys
from email.message import Message
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

_app = server.App(remote=True)


class _Server:
    app = _app


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    qs = environ.get("QUERY_STRING", "")
    if urlparse(path).path == "/api/stream":          # live push needs a long-lived connection; the page polls instead
        body = b"retry: 600000\n\n"                      # an empty stream that the browser will not retry for 10 minutes
        start_response("200 OK", [("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    body = environ["wsgi.input"].read(min(length, 20_000)) if length else b""

    h = server.Handler.__new__(server.Handler)          # reuse every route without opening a socket
    msg = Message()
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            msg[k[5:].replace("_", "-").title()] = v
    if environ.get("CONTENT_TYPE"):
        msg["Content-Type"] = environ["CONTENT_TYPE"]
    msg["Content-Length"] = str(len(body))
    h.headers, h.rfile, h.wfile = msg, io.BytesIO(body), io.BytesIO()
    h.server, h.client_address = _Server, (environ.get("REMOTE_ADDR", ""), 0)
    h._https = environ.get("wsgi.url_scheme") == "https"      # real scheme from the server, not a header anyone can fake
    h.command, h.path = method, path + ("?" + qs if qs else "")
    h.request_version, h.requestline, h.close_connection = "HTTP/1.1", f"{method} {h.path} HTTP/1.1", True
    handler = getattr(h, "do_" + method, None)
    if handler is None:
        start_response("405 Method Not Allowed", [("Content-Length", "0")])
        return [b""]
    handler()

    raw = h.wfile.getvalue()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 1)[1] if " " in lines[0] else "500 Internal Server Error"
    headers = [tuple(l.split(": ", 1)) for l in lines[1:] if ": " in l and l.split(": ", 1)[0].lower() not in ("server", "date", "connection")]
    start_response(status, headers)
    return [payload]
