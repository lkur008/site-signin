#!/usr/bin/env python3
"""Local proxy + static file server for the door-access dashboard.

Inception's REST API doesn't send CORS headers, so a browser page can't
call it directly. This script serves door-dashboard.html on your machine
and forwards any /api/* request to the real controller server-to-server,
where CORS doesn't apply, adding the headers back on the way out.

Usage:
    python3 proxy.py http://<inception-controller-ip>

Then open:
    http://localhost:8787/door-dashboard.html

In the dashboard's Settings, set "Controller base URL" to
http://localhost:8787 (not the Inception IP) — everything else about the
dashboard stays the same.
"""
import os
import ssl
import sys
import urllib.error
import urllib.request
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8787
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UNVERIFIED_SSL = ssl._create_unverified_context()


def make_handler(target):
    class ProxyHandler(SimpleHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _proxy(self, method):
            url = target + self.path
            body = None
            length = self.headers.get("Content-Length")
            if length:
                body = self.rfile.read(int(length))
            req = urllib.request.Request(url, data=body, method=method)
            for h in ("Authorization", "Content-Type", "Cookie"):
                if h in self.headers:
                    req.add_header(h, self.headers[h])
            try:
                with urllib.request.urlopen(req, timeout=15, context=UNVERIFIED_SSL) as resp:
                    self.send_response(resp.status)
                    self._cors()
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    self.wfile.write(resp.read())
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self._cors()
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self.send_response(502)
                self._cors()
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(("Proxy error reaching " + target + ": " + str(e)).encode())

        def do_GET(self):
            if self.path.startswith("/api/"):
                self._proxy("GET")
            else:
                super().do_GET()

        def do_POST(self):
            self._proxy("POST")

        def do_DELETE(self):
            self._proxy("DELETE")

    return ProxyHandler


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 proxy.py http://<inception-controller-ip>")
        sys.exit(1)
    target = sys.argv[1].rstrip("/")
    handler = partial(make_handler(target), directory=SCRIPT_DIR)
    print(f"Proxying /api/* to {target}")
    print(f"Serving files from {SCRIPT_DIR}")
    print(f"Open http://localhost:{PORT}/door-dashboard.html")
    print(f"Set the dashboard's 'Controller base URL' setting to http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
