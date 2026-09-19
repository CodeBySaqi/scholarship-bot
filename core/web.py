"""Polite, resilient HTTP layer.

Improvements over the blueprint's `fetch_static`/`fetch_dynamic`:
  * on-disk conditional-GET cache (ETag / Last-Modified) → ~0 bytes for unchanged pages
  * per-domain minimum interval + jitter + full-jitter backoff on 429/5xx
  * optional robots.txt check (cached) so the bot never gets your IP banned
  * circuit breaker per source: a blocked/dead site is skipped for a cooldown
    instead of failing the nightly run 8 times
  * single retry with an alternate UA, then optional Playwright fallback
  * `offline` mode for CI/tests that reuses only cached HTML
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import requests

log = logging.getLogger("web")

_RETRY_AFTER_CODES = {408, 425, 429}
_SERVER_CODES = {500, 502, 503, 504, 520, 521, 522, 524}
_BLOCK_CODES = {401, 403, 406, 451}

_ALTERNATE_UAS = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
)


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    from_cache: bool = False
    not_modified: bool = False
    error: str | None = None
    dynamic: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "ignore")).hexdigest()[:32]


class HtmlCache:
    """Tiny SQLite-backed HTTP cache with conditional-GET metadata."""

    def __init__(self, path: Path, *, max_age_hours: float = 0.0):
        self.path = path
        self.max_age_hours = max_age_hours
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS http_cache (
                key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status INTEGER NOT NULL,
                etag TEXT,
                last_modified TEXT,
                body TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    def get(self, url: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT url,status,etag,last_modified,body,fetched_at FROM http_cache WHERE key=?",
                (self.key(url),),
            ).fetchone()
        if not row:
            return None
        return {
            "url": row[0],
            "status": row[1],
            "etag": row[2],
            "last_modified": row[3],
            "body": row[4],
            "fetched_at": datetime.fromisoformat(row[5]),
        }

    def put(self, url: str, status: int, body: str, etag: str | None, last_modified: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO http_cache(key,url,status,etag,last_modified,body,fetched_at,hits) "
                "VALUES(?,?,?,?,?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET status=excluded.status,etag=excluded.etag,"
                "last_modified=excluded.last_modified,body=excluded.body,fetched_at=excluded.fetched_at",
                (
                    self.key(url),
                    url,
                    status,
                    etag,
                    last_modified,
                    body,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            self._conn.commit()

    def is_fresh(self, fetched_at: datetime) -> bool:
        if self.max_age_hours <= 0:
            return False
        age = (datetime.now(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds() / 3600.0
        return age < self.max_age_hours


class DomainThrottle:
    """Per-domain politeness: never hit the same host more than once per N seconds."""

    def __init__(self, min_interval: float = 2.0, jitter: float = 2.0):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> float:
        host = urlparse(url).netloc.lower()
        with self._lock:
            last = self._last.get(host)
            now = time.monotonic()
            delay = 0.0
            if last is not None:
                delta = now - last
                need = self.min_interval + random.uniform(0, self.jitter)
                if delta < need:
                    delay = need - delta
            self._last[host] = now + delay
        if delay > 0:
            time.sleep(delay)
        return delay


class Robots:
    """robots.txt gate. Fail-open on network errors (a dead robots.txt should
    not silently stop the whole run), but always fail-open on 4xx/5xx text."""

    def __init__(self, enabled: bool = False, ua: str = "scholarship-bot"):
        self.enabled = enabled
        self.ua = ua
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        host = urlparse(url).netloc.lower()
        with self._lock:
            rp = self._cache.get(host, "missing")  # type: ignore[assignment]
        if rp == "missing":
            rp = self._load(host)
            with self._lock:
                self._cache[host] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:  # pragma: no cover
            return True

    def _load(self, host: str):
        try:
            with urlopen(f"https://{host}/robots.txt", timeout=8) as resp:
                text = resp.read().decode("utf-8", "ignore")
        except Exception:
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        return rp


class CircuitBreaker:
    """Track consecutive failures per source and cool off broken sources."""

    def __init__(self, max_failures: int = 3, cooldown_minutes: int = 180):
        self.max_failures = max_failures
        self.cooldown = cooldown_minutes * 60  # seconds
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open_until(self, key: str) -> float:
        with self._lock:
            st = self._state.get(key)
            if not st or not st.get("open_until"):
                return 0.0
            remaining = st["open_until"] - time.monotonic()
            return remaining if remaining > 0 else 0.0

    def record_failure(self, key: str) -> bool:
        with self._lock:
            st = self._state.setdefault(key, {"failures": 0, "open_until": 0.0})
            st["failures"] += 1
            just_opened = st["failures"] == self.max_failures
            if st["failures"] >= self.max_failures:
                backoff = min(self.cooldown * (2 ** (st["failures"] - self.max_failures)), 6 * 3600)
                st["open_until"] = time.monotonic() + backoff
            return just_opened

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state[key] = {"failures": 0, "open_until": 0.0}

    def failures(self, key: str) -> int:
        with self._lock:
            return self._state.get(key, {}).get("failures", 0)


class Web:
    """Facade used by every scraper."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        user_agent: str,
        timeout: float = 25.0,
        min_interval: float = 2.0,
        jitter: float = 2.0,
        retries: int = 2,
        respect_robots: bool = False,
        offline: bool = False,
        allow_playwright: bool = True,
        max_age_hours: float = 0.0,
        max_failures: int = 3,
        cooldown_minutes: int = 180,
    ):
        self.cache = HtmlCache(Path(cache_dir) / "http_cache.sqlite3", max_age_hours=max_age_hours)
        self.throttle = DomainThrottle(min_interval=min_interval, jitter=jitter)
        self.robots = Robots(enabled=respect_robots)
        self.breaker = CircuitBreaker(max_failures=max_failures, cooldown_minutes=cooldown_minutes)
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self.allow_playwright = allow_playwright
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

    # ---------------------------------------------------------------- public
    def fetch(self, url: str, *, source: str = "default", dynamic: bool = False, render: bool = False) -> FetchResult:
        if self.breaker.open_until(source) > 0 and not self.offline:
            return FetchResult(url, 599, "", error=f"circuit open for '{source}'", from_cache=True)
        if not self.robots.allowed(url):
            return FetchResult(url, 403, "", error="blocked by robots.txt")

        cached = self.cache.get(url)
        if self.offline:
            if cached:
                return FetchResult(url, cached["status"], cached["body"], from_cache=True, not_modified=True)
            return FetchResult(url, 0, "", error="offline and not cached")

        if cached and self.cache.is_fresh(cached["fetched_at"]) and not dynamic:
            return FetchResult(url, cached["status"], cached["body"], from_cache=True, not_modified=True)

        headers: dict[str, str] = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        attempt_errors: list[str] = []
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(30.0, (2**attempt) + random.uniform(0, 1.5)))  # full-ish jitter backoff
            try:
                self.throttle.wait(url)
                resp = self.session.get(url, timeout=self.timeout, headers=headers, allow_redirects=True)
            except requests.RequestException as exc:
                attempt_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if resp.status_code in _RETRY_AFTER_CODES or resp.status_code in _SERVER_CODES:
                attempt_errors.append(f"http {resp.status_code}")
                continue
            if resp.status_code in _BLOCK_CODES:
                attempt_errors.append(f"http {resp.status_code}")
                # one UA rotation before giving up
                if attempt == 0:
                    self.session.headers["User-Agent"] = random.choice(_ALTERNATE_UAS)
                    continue
                break
            if resp.status_code == 200:
                self.session.headers["User-Agent"] = user_agent_default()
                body = resp.text
                self.cache.put(
                    url,
                    200,
                    body,
                    resp.headers.get("ETag"),
                    resp.headers.get("Last-Modified"),
                )
                self.breaker.record_success(source)
                needs_render = render or self._looks_like_empty_shell(body)
                if needs_render and self.allow_playwright:
                    rendered = self._render(url)
                    if rendered:
                        self.cache.put(url, 200, rendered, None, None)
                        return FetchResult(url, 200, rendered, dynamic=True)
                return FetchResult(url, 200, body)
            break
        # exhausted retries → serve stale cache if we have it
        self.breaker.record_failure(source)
        if cached:
            log.warning("serving stale cache for %s (%s)", url, "; ".join(attempt_errors[-1:]))
            return FetchResult(url, cached["status"], cached["body"], from_cache=True, not_modified=True)
        return FetchResult(url, 0, "", error="; ".join(attempt_errors[-2:]) or "fetch failed")

    @staticmethod
    def _looks_like_empty_shell(html: str) -> bool:
        """Cheap heuristic: SPA shell / JS challenge page → needs a real browser."""
        if len(html) > 200000:
            return False
        low = html.lower()
        signals = ("__next/data", "ng-app", "data-reactroot", "cf-chl", "just a moment", "enable javascript")
        body_start = low.find("<body")
        body = low[body_start:] if body_start != -1 else low
        return any(sig in low for sig in signals) and len(body) < 4000

    def _render(self, url: str) -> str | None:
        try:
            from playwright.sync_api import sync_playwright  # optional dependency
        except Exception:
            log.info("playwright not installed; skipping JS render for %s", url)
            return None
        try:
            with sync_playwright() as p:  # pragma: no cover - needs browser binaries
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(user_agent=self.session.headers.get("User-Agent"))
                page.goto(url, timeout=45000, wait_until="networkidle")
                html = page.content()
                browser.close()
                return html
        except Exception as exc:  # pragma: no cover
            log.warning("playwright render failed for %s: %s", url, exc)
            return None

    def post_json(self, url: str, payload: dict, *, headers: dict | None = None, source: str = "default"):
        if self.breaker.open_until(source) > 0:
            return None
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout, headers=headers or {})
            self.breaker.record_success(source)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            self.breaker.record_failure(source)
            log.warning("post_json %s failed: %s", url, exc)
            return None

    def get_json(self, url: str, *, headers: dict | None = None, source: str = "default"):
        if self.offline:
            return None
        try:
            self.throttle.wait(url)
            resp = self.session.get(url, timeout=self.timeout, headers={"Accept": "application/json", **(headers or {})})
            resp.raise_for_status()
            self.breaker.record_success(source)
            return resp.json()
        except Exception as exc:
            self.breaker.record_failure(source)
            log.warning("get_json %s failed: %s", url, exc)
            return None


_UA_DEFAULT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def user_agent_default() -> str:
    return _UA_DEFAULT
