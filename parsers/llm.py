"""LLM assist layer — used only to fill gaps the regex pass could not cover.

Differences from the blueprint's `parse_scholarship`:
  * one request covers a *batch* of items (5x fewer HTTP round-trips)
  * results are cached in SQLite keyed by content hash → re-runs cost $0
  * a per-cycle token/USD budget with graceful degradation to regex-only
  * works with any OpenAI-compatible endpoint (OpenAI, Groq, Together,
    Ollama, LM Studio, OpenRouter) so you can run it for free locally
  * output is validated by the same Pydantic schema as the regex pass, and the
    LLM is explicitly forbidden from overwriting values we read verbatim
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .schema import ScholarshipRecord

log = logging.getLogger("llm")
SCHEMA_VERSION = "2026-09-v1"

# USD per 1M tokens (input, output). Unknown models cost 0 (local).
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "grok-3-mini": (0.30, 0.50),
    "claude-3-5-haiku": (0.80, 4.00),
}

SYSTEM_PROMPT = """You extract structured data from scholarship web pages. You are precise and conservative.

Rules
1. Use ONLY information present in the page text. Never invent, infer, or extrapolate.
2. If a value is not stated, return null for it. An absent field is better than a wrong one.
3. `deadline` must be YYYY-MM-DD. If the page gives a month only, use the last day of that month and set `deadline_estimated` true. If applications are rolling/ongoing, set deadline null and `deadline_rolling` true.
4. `degree_level`: one of Bachelor, Master, PhD, Postdoc, Diploma, Any. Use "Any" when open to all levels.
5. `funding_type`: "Full" only if tuition AND living costs are covered (or it says fully funded). "Tuition-only" if only fees/waiver. Else "Partial" or "Unknown".
6. `amount_usd`: annual value in US dollars (monthly stipend x 10 academic months). Convert other currencies approximately. null if no figure is stated.
7. `coverage`: array from this list only, including only what is explicitly mentioned: tuition, stipend, airfare, health_insurance, visa_fee, accommodation, books, language_course, research_budget.
8. `eligible_countries`: array of country names or a group name (Commonwealth, EU, developing countries, worldwide) IF the page lists them; [] if the page names no restriction.
9. `excluded_countries`: countries the page says are NOT eligible (e.g. "not open to US citizens").
10. `requires_gre`, `requires_work_experience`: true only if explicitly stated.
11. `field`: short list of study fields (e.g. "Machine Learning, Data Science"), or "Any".
12. `min_gpa` is on the scale given in the page; also return `gpa_scale` ("4.0" or "5.0" etc).
13. `notes`: one line max — anything a human should know (e.g. "requires 2 yrs work after graduation").

Return strictly valid JSON: {"results": [{"idx": <int>, ...fields...}]}. One object per input item, matching idx."""


@dataclass
class Usage:
    calls: int = 0
    items: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    failures: int = 0
    budget_exhausted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "items": self.items,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 5),
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "budget_exhausted": self.budget_exhausted,
        }


class ParseCache:
    """SQLite cache: content_hash -> parsed JSON."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS parse_cache (key TEXT PRIMARY KEY, model TEXT, "
            "payload TEXT NOT NULL, created_at TEXT, hits INTEGER DEFAULT 0)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(content_hash: str, model: str) -> str:
        return f"{SCHEMA_VERSION}:{model}:{content_hash}"

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM parse_cache WHERE key=?", (key,)).fetchone()
            if row:
                self._conn.execute("UPDATE parse_cache SET hits = hits + 1 WHERE key=?", (key,))
                self._conn.commit()
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return None
        return None

    def put(self, key: str, model: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO parse_cache(key,model,payload,created_at,hits) VALUES(?,?,?,?,1)",
                (key, model, json.dumps(payload, default=str), datetime.utcnow().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def size(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM parse_cache").fetchone()[0])


class LLMClient:
    """Minimal OpenAI-compatible chat client (no SDK dependency)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        timeout: float = 90.0,
        max_items_per_call: int = 5,
        max_item_chars: int = 2600,
        max_output_tokens: int = 2600,
        budget_usd: float = 0.12,
        retries: int = 2,
        enabled: bool = True,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_items = max_items_per_call
        self.max_item_chars = max_item_chars
        self.max_output_tokens = max_output_tokens
        self.budget_usd = budget_usd
        self.retries = retries
        self.enabled = bool(enabled and api_key)
        self.usage = Usage()

    # --------------------------------------------------------------- public
    @staticmethod
    def needs_llm(record: ScholarshipRecord) -> tuple[bool, list[str]]:
        """Triage: only spend tokens when important fields are missing."""
        missing: list[str] = []
        if not record.deadline and not record.deadline_rolling:
            missing.append("deadline")
        if not record.funding_type or record.funding_type == "Unknown":
            missing.append("funding_type")
        if not record.degree_level:
            missing.append("degree_level")
        if record.amount_usd is None and record.funding_type != "Full":
            missing.append("amount_usd")
        if not record.country:
            missing.append("country")
        return (len(missing) >= 2), missing

    def parse_batch(self, items: list[tuple[str, str, str]], cache: ParseCache) -> dict[str, dict[str, Any]]:
        """items: list of (id, title, text). Returns id -> parsed dict."""
        out: dict[str, dict[str, Any]] = {}
        todo: list[tuple[str, str, str, str]] = []  # id, title, text, cache_key
        for item_id, title, text in items:
            ch = hashlib_sha(text)
            key = ParseCache.make_key(ch, self.model)
            hit = cache.get(key)
            if hit is not None:
                self.usage.cache_hits += 1
                if hit:
                    merged = dict(hit)
                    merged["title"] = merged.get("title") or title
                    out[item_id] = merged
                else:
                    out[item_id] = {}  # cached "nothing found"
                continue
            todo.append((item_id, title, text, key))

        for chunk in _chunks(todo, self.max_items):
            if self.usage.cost_usd >= self.budget_usd:
                self.usage.budget_exhausted = True
                log.warning("LLM budget reached ($%.3f) — remaining items stay regex-only", self.usage.cost_usd)
                break
            results = self._call([(c[0], c[1], c[2]) for c in chunk])
            payloads: dict[str, dict] = {}
            for row in results:
                item_id = str(row.pop("idx", "")) or chunk[0][0]
                payloads[item_id] = row
            for item_id, title, text, key in chunk:
                payload = payloads.get(item_id)
                if payload is None:
                    self.usage.failures += 1
                    continue
                payload.setdefault("title", title)
                try:
                    rec = ScholarshipRecord.model_validate(_clean_payload(payload))
                    payload = rec.model_dump(mode="json")
                except Exception as exc:
                    log.debug("llm payload rejected: %s", str(exc)[:200])
                    payload = {k: v for k, v in payload.items() if k in ScholarshipRecord.model_fields}
                    try:
                        payload = ScholarshipRecord.model_validate(_clean_payload(payload)).model_dump(mode="json")
                    except Exception:
                        payload = {}
                out[item_id] = payload
                cache.put(key, self.model, payload)
        return out

    # --------------------------------------------------------------- private
    def _call(self, batch: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        payload_rows = []
        for item_id, title, text in batch:
            payload_rows.append(
                {
                    "idx": item_id,
                    "title": title[:200],
                    "page_text": re.sub(r"\s+", " ", text)[: self.max_item_chars],
                }
            )
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"items": payload_rows}, ensure_ascii=False)},
            ],
        }
        # response_format is not supported by every compatible server → degrade
        for extra in ({"response_format": {"type": "json_object"}}, {}):
            req_body = {**body, **extra}
            for attempt in range(self.retries + 1):
                try:
                    raw = self._post(req_body)
                    data = self._extract_json(raw)
                    if data is not None:
                        self._account_usage(raw, req_body)
                        rows = data.get("results") if isinstance(data, dict) else data
                        if isinstance(rows, list):
                            return [r for r in rows if isinstance(r, dict)]
                    raise ValueError("no json in response")
                except RateLimited as exc:
                    wait = min(60.0, float(exc.retry_after or 5 * (attempt + 1)))
                    log.info("rate limited, sleeping %.0fs", wait)
                    time.sleep(wait)
                except Exception as exc:  # noqa: BLE001
                    if extra and attempt == self.retries:
                        continue  # retry without response_format
                    log.warning("llm call failed (batch of %d): %s", len(batch), str(exc)[:180])
                    self.usage.failures += 1
                    time.sleep(1.5 * (attempt + 1))
                    break
        return []

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            if exc.code in (429,):
                raise RateLimited(retry_after=_retry_after(exc)) from exc
            raise RuntimeError(f"http {exc.code}: {detail}") from exc

    def _account_usage(self, raw: dict, body: dict) -> None:
        u = raw.get("usage") or {}
        tin = int(u.get("prompt_tokens") or _approx_tokens(json.dumps(body)))
        tout = int(u.get("completion_tokens") or 0)
        self.usage.calls += 1
        self.usage.items += len((body.get("messages") or [{}])[-1].get("content", "").split('"idx"')) - 1
        self.usage.tokens_in += tin
        self.usage.tokens_out += tout
        pin, pout = PRICING.get(self.model, (0.0, 0.0))
        self.usage.cost_usd += tin / 1_000_000 * pin + tout / 1_000_000 * pout

    @staticmethod
    def _extract_json(raw: dict) -> Any:
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        if isinstance(content, list):  # some servers return parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        content = (content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-z]*\s*|\s*```$", "", content)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}|\[.*\]", content, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
        return None


class RateLimited(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("429 rate limited")
        self.retry_after = retry_after


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    val = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in payload.items():
        if isinstance(value, str):
            value = value.strip()
            if value.lower() in {"null", "none", "n/a", "na", "unknown", "-", "not specified", "not mentioned"}:
                value = None
        if isinstance(value, (list, tuple)):
            value = [v for v in value if v not in (None, "", "null")]
        out[key] = value
    return out


def _chunks(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), max(1, size)):
        yield seq[i : i + size]


def hashlib_sha(text: str) -> str:
    import hashlib

    cleaned = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8", "ignore")).hexdigest()[:32]


def disabled_client() -> LLMClient:
    c = LLMClient(api_key="", base_url="", model="disabled", enabled=False)
    c.enabled = False
    return c


def from_config(settings: Any, cache_path: Path, *, offline: bool = False) -> LLMClient:
    llm = settings.llm
    client = LLMClient(
        api_key=llm.get("api_key") or "",
        base_url=llm.get("base_url") or "https://api.openai.com/v1",
        model=llm.get("model") or "gpt-4o-mini",
        temperature=float(llm.get("temperature", 0.0)),
        timeout=float(llm.get("timeout", 90.0)),
        max_items_per_call=int(llm.get("batch_size", 5)),
        max_item_chars=int(llm.get("max_item_chars", 2600)),
        max_output_tokens=int(llm.get("max_output_tokens", 2600)),
        budget_usd=float(llm.get("budget_usd", 0.12)),
        retries=int(llm.get("retries", 2)),
        enabled=bool(llm.get("enabled", True)) and not offline,
    )
    client.cache = ParseCache(cache_path)  # type: ignore[attr-defined]
    return client


__all__ = ["SCHEMA_VERSION", "LLMClient", "ParseCache", "Usage", "date", "disabled_client", "from_config"]
