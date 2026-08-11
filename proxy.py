#!/usr/bin/env python3
"""Local proxy + static file server for the door-access dashboard.

Inception's REST API sends no CORS headers, so a browser page can't call
it directly. This script serves door-dashboard.html on your machine and
forwards any /api/* request to the real controller server-to-server
(where CORS doesn't apply), adding CORS headers on the way back.

It logs in with your Inception username/password (prompted here in the
terminal, never stored in the browser) to get a session, attaches that
session to every forwarded request, and automatically re-logs-in if the
session expires (Inception expires sessions after 10 minutes idle).

First run (interactive):
    python3 proxy.py http://<inception-controller-ip>
You'll be prompted for your Inception username/password, and offered
the option to save them to a local config file (chmod 600, outside the
git repo by default) so future runs — e.g. from a systemd service on
boot — don't need a terminal to prompt into:
    python3 proxy.py

Then open:
    http://localhost:8787/door-dashboard.html

In the dashboard's Settings, set "Controller base URL" to
http://localhost:8787 (not the Inception IP) — no token/password is
ever entered in the dashboard itself.
"""
import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8787
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.door-dashboard-config.json")
UNVERIFIED_SSL = ssl._create_unverified_context()
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

session = {"id": None}
creds = {"username": None, "password": None}


def login(target):
    body = json.dumps({"Username": creds["username"], "Password": creds["password"]}).encode()
    req = urllib.request.Request(
        target + "/api/v1/authentication/login",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15, context=UNVERIFIED_SSL) as resp:
        data = json.loads(resp.read())
    session_id = data.get("UserID")
    if not session_id or session_id == EMPTY_GUID:
        raise RuntimeError("Login failed — check the username/password entered when the proxy started.")
    session["id"] = session_id
    return session_id


def make_handler(target):
    class ProxyHandler(SimpleHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _forward_once(self, method, path, body):
            if not session["id"]:
                login(target)
            req = urllib.request.Request(target + path, data=body, method=method)
            req.add_header("Cookie", "LoginSessId=" + session["id"])
            if body:
                req.add_header("Content-Type", "application/json")
            return urllib.request.urlopen(req, timeout=15, context=UNVERIFIED_SSL)

        def _proxy(self, method):
            body = None
            length = self.headers.get("Content-Length")
            if length:
                body = self.rfile.read(int(length))
            try:
                try:
                    resp = self._forward_once(method, self.path, body)
                except urllib.error.HTTPError as e:
                    if e.code == 401:
                        login(target)
                        resp = self._forward_once(method, self.path, body)
                    else:
                        raise
                with resp:
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


def load_config(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_config(path, target, username, password):
    with open(path, "w") as f:
        json.dump({"target": target, "username": username, "password": password}, f)
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser(description="Local proxy + static server for the door-access dashboard.")
    parser.add_argument("target", nargs="?", help="http://<inception-ip> (only needed the first run, or when no config is saved)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to saved credentials (default: {DEFAULT_CONFIG_PATH})")
    args = parser.parse_args()

    saved = load_config(args.config)
    if saved:
        target = saved["target"].rstrip("/")
        creds["username"] = saved["username"]
        creds["password"] = saved["password"]
        print(f"Loaded saved config from {args.config}")
    else:
        if not args.target:
            print(f"No saved config at {args.config} — run once with the controller URL:")
            print("    python3 proxy.py http://<inception-controller-ip>")
            sys.exit(1)
        target = args.target.rstrip("/")
        creds["username"] = input("Inception username: ")
        creds["password"] = getpass.getpass("Inception password: ")

    print("Logging in to " + target + " ...")
    login(target)
    print("Login OK.")

    if not saved and sys.stdin.isatty():
        answer = input(f"Save these to {args.config} for automatic startup next time (e.g. from systemd)? [y/N] ").strip().lower()
        if answer == "y":
            save_config(args.config, target, creds["username"], creds["password"])
            print(f"Saved to {args.config} (permissions restricted to your user).")

    handler = partial(make_handler(target), directory=SCRIPT_DIR)
    print(f"Proxying /api/* to {target}")
    print(f"Serving files from {SCRIPT_DIR}")
    print(f"Open http://localhost:{PORT}/door-dashboard.html")
    print(f"Set the dashboard's 'Controller base URL' setting to http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
