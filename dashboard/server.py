"""Stdlib HTTP adapter for the dashboard.

Why not FastAPI/Flask: the engine runs on a free VPS and a GitHub Action, and
`requirements.txt` is the install surface for both. `http.server` plus one
hand-written auth check is ~120 lines and has no transitive dependencies, which
also means no CVE triage on a box that only I can reach.

The preview proxy in this sandbox reaches the app through a port-forwarded
hostname, so the page must use *relative* URLs and the server must bind
`0.0.0.0`. Both are enforced here.
"""

from __future__ import annotations

import hmac
import secrets
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api import Ctx, dispatch
from .ui import LOGIN_HTML, PAGE

TOKEN_NAME = "radar_token"
COOKIE_ATTRS = "Path=/; SameSite=Lax"


def token_file(root: Path) -> Path:
    return Path(root) / "data" / "dashboard.token"


def load_or_create_token(root: Path) -> str:
    """A stable local token, 0600, never printed and never committed.

    A random token per boot would break the page after every restart; a fixed
    password in the repo would be worse. This is the middle: written once to
    `data/` (gitignored), reusable, and rotated by deleting the file.
    """
    path = token_file(root)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 16:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


class Dashboard(BaseHTTPRequestHandler):
    server_version = "ScholarRadar/1.0"
    protocol_version = "HTTP/1.1"
    ctx: Ctx
    token: str = ""
    require_token: bool = True
    allow_embed: bool = True

    # ---------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False) or ("40" in str(args[1]) if len(args) > 1 else False):
            sys.stderr.write(f"[dashboard] {self.address_string()} {fmt % args}\n")

    def _authorized(self, parsed) -> bool:
        if not self.require_token:
            return True
        header = self.headers.get("X-Radar-Token") or ""
        if not header:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                header = auth[7:].strip()
        if not header:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
                item = cookie.get(TOKEN_NAME)
                header = item.value if item else ""
            except Exception:  # pragma: no cover - malformed cookie
                header = ""
        if not header and parsed.query:
            header = (parse_qs(parsed.query).get("token") or [""])[0]
        return bool(header) and hmac.compare_digest(str(header), self.token)

    def _reply(self, status: int, headers: dict[str, str], body: bytes, *, html_page: bool = False) -> None:
        if html_page and self.allow_embed:
            headers = {**headers, "Content-Security-Policy": "frame-ancestors *"}
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        if status in {200, 404} and not headers.get("Cache-Control"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET", b"")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET", b"")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4_000_000:
            self.close_connection = True  # don't leave an unread body on a kept-alive socket
            self._reply(413, {"Content-Type": "text/plain; charset=utf-8"}, b"body too large")
            return
        self._handle("POST", self.rfile.read(length) if length else b"")

    def _handle(self, method: str, body: bytes) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/favicon.ico", "/robots.txt"}:
            self._reply(204, {"Content-Type": "text/plain"}, b"")
            return
        if path == "/healthz":
            self._reply(200, {"Content-Type": "text/plain"}, b"ok\n")
            return
        if not self._authorized(parsed):
            if path.startswith("/api/"):
                self._reply(401, {"Content-Type": "application/json"},
                            b'{"error":"unauthorized","hint":"send the dashboard token as X-Radar-Token"}')
                return
            self._reply(401, {"Content-Type": "text/html; charset=utf-8"}, LOGIN_HTML.encode("utf-8"),
                        html_page=True)
            return
        if path in {"/", "/index.html", "/dashboard"}:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            # Hand the query-string token to a cookie so the page's later
            # fetch() calls authenticate without the token in every URL.
            if self.require_token and not self._has_cookie():
                headers["Set-Cookie"] = f"{TOKEN_NAME}={self.token}; {COOKIE_ATTRS}"
            self._reply(200, headers, PAGE.encode("utf-8"), html_page=True)
            return
        if path.startswith("/api/"):
            status_code, headers, payload = dispatch(self.ctx, method, path, parsed.query, body)
            self._reply(status_code, headers, payload)
            return
        self._reply(404, {"Content-Type": "text/html; charset=utf-8"},
                    b"<html><body style='font:16px system-ui;background:#101418;color:#e6e6e6'>"
                    b"<p>Not a page here. <a style='color:#7fd1ff' href='/'>Back to the dashboard</a></p>",
                    html_page=True)

    def _has_cookie(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
            return bool(cookie.get(TOKEN_NAME))
        except Exception:  # pragma: no cover
            return False


def serve(ctx: Ctx, *, host: str = "0.0.0.0", port: int = 8765, insecure: bool = False,
          token: str | None = None, verbose: bool = False) -> int:
    """Block and serve. `insecure` skips the token — for a sandbox preview only."""
    root = Path(ctx.root)
    if insecure:
        print("[dashboard] token check DISABLED (--insecure). Anyone who can reach this port "
              "can edit config and trigger runs.", file=sys.stderr)
    resolved_token = token or ("" if insecure else load_or_create_token(root))
    # Class attributes, not a closure: `partial` objects reject attributes, and
    # the handler class is instantiated per connection by the stdlib server.
    Dashboard.ctx = ctx
    Dashboard.token = resolved_token
    Dashboard.require_token = not insecure

    httpd = ThreadingHTTPServer((host, port), Dashboard)
    httpd.daemon_threads = True
    httpd.verbose = verbose  # type: ignore[attr-defined]
    shown = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    print(f"Scholar Radar dashboard → http://{shown}:{port}/")
    if resolved_token:
        print(f"  token (from {token_file(root)}): {resolved_token}")
        print(f"  direct link: http://{shown}:{port}/?token={resolved_token}")
    print("  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[dashboard] stopped")
    finally:
        httpd.server_close()
    return 0
