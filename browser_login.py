"""Capture .ROBLOSECURITY by opening Roblox sign-in in a real browser.

Launches Chrome (or Edge as fallback) with a throwaway --user-data-dir and a
remote-debugging port, pointed at roblox.com/login. We poll the DevTools
protocol for the .ROBLOSECURITY cookie; the user signs in normally (captchas
work because it's a real browser). A fresh temp profile each time means every
"Add Account" starts logged out and the browser is fully closed afterwards.
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

import websocket

LOGIN_URL = "https://www.roblox.com/login"


def find_browser() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        shutil.which("chrome"),
        shutil.which("msedge"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class BrowserLogin:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.profile: str | None = None

    def start(self) -> None:
        exe = find_browser()
        if not exe:
            raise RuntimeError("No Chrome or Edge installation found")
        self.port = _free_port()
        self.profile = tempfile.mkdtemp(prefix="rbxlogin-")
        self.proc = subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                LOGIN_URL,
            ]
        )

    # ---------- CDP ----------

    def _json(self, path: str):
        return json.load(
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)
        )

    def _page_ws_url(self) -> str | None:
        try:
            targets = self._json("/json/list")
        except Exception:
            return None
        for t in targets:
            if t.get("type") == "page" and "roblox.com" in t.get("url", ""):
                return t.get("webSocketDebuggerUrl")
        for t in targets:
            if t.get("type") == "page":
                return t.get("webSocketDebuggerUrl")
        return None

    def query_cookie(self) -> str | None:
        ws_url = self._page_ws_url()
        if not ws_url:
            return None
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        try:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Network.getCookies",
                        "params": {"urls": ["https://www.roblox.com"]},
                    }
                )
            )
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    for c in msg.get("result", {}).get("cookies", []):
                        if c.get("name") == ".ROBLOSECURITY":
                            return c.get("value")
                    return None
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def wait_for_cookie(self, timeout: float = 300, interval: float = 1.5, cancelled=None) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancelled and cancelled():
                return None
            if self.proc and self.proc.poll() is not None:
                return None  # user closed the browser
            try:
                cookie = self.query_cookie()
                if cookie:
                    return cookie
            except Exception:
                pass
            time.sleep(interval)
        return None

    def wait_for_login(self, validate, timeout: float = 300, interval: float = 1.5, cancelled=None):
        """Poll until the captured .ROBLOSECURITY validates to a real account.

        Roblox also sets the cookie on logged-out sessions (validated to null
        user fields) — those are skipped and polling continues; the cookie is
        replaced once the user actually signs in. `validate` must return a
        truthy user dict or None.
        Returns (user, cookie) or None on cancel/close/timeout.
        """
        deadline = time.time() + timeout
        last_cookie = None
        while time.time() < deadline:
            if cancelled and cancelled():
                return None
            if self.proc and self.proc.poll() is not None:
                return None
            try:
                cookie = self.query_cookie()
            except Exception:
                cookie = None
            if cookie and cookie != last_cookie:
                last_cookie = cookie
                user = validate(cookie)
                if user:
                    return user, cookie
            time.sleep(interval)
        return None

    def close(self) -> None:
        try:
            ver = self._json("/json/version")
            ws = websocket.create_connection(
                ver["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
            )
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.close()
            if self.proc:
                self.proc.wait(timeout=5)
        except Exception:
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)
