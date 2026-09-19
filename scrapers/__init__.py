"""Scraper framework.

Every source is declared in config.yaml with CSS selectors (or an API mapping),
so when a site redesigns, you edit YAML — not Python.  Scrapers return
*candidate* dicts (title/url/raw text/page text/optional pre-parsed fields);
parsing, validation and storage all happen in the pipeline so a scraper can
never write half-baked rows.

Also new vs. the blueprint: detail pages are only fetched when the listing text
is not enough (`fetch_detail` + `min_listing_score`), which is what turns a
500-page crawl into 40 pages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from db.models import normalize_url as normalize_url_static
from parsers.fields import clean_text

log = logging.getLogger("scraper")


@dataclass
class Candidate:
    """One potential scholarship coming out of a scraper.

    `skip` means the scraper recognised the page as already stored and
    deliberately did no work for it — the pipeline counts it as unchanged.
    """

    source: str
    title: str
    url: str
    page_text: str | None = None
    listing_text: str | None = None
    priority: int = 1
    pre: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None
    fetched: bool = False
    skip: bool = False

    def merged_text(self) -> str:
        return "\n".join(x for x in (self.listing_text, self.page_text) if x)

    def key(self) -> str:
        return hashlib.sha256(f"{self.source}|{self.url}".encode()).hexdigest()[:24]


class ScrapeResult:
    def __init__(self, source_id: str):
        self.source_id = source_id
        self.candidates: list[Candidate] = []
        self.items_skipped = 0
        self.errors: list[str] = []
        self.detail_fetches = 0
        self.cache_hits = 0
        self.status = "ok"

    def add(self, cand: Candidate) -> None:
        self.candidates.append(cand)

    @property
    def ok(self) -> bool:
        return not self.errors or bool(self.candidates)


class BaseScraper:
    kind = "base"

    def __init__(self, source: dict[str, Any], web: Any, profile: Any | None = None):
        self.source = source
        self.web = web
        self.profile = profile
        self.id: str = source.get("id") or self.__class__.__name__
        self.priority: int = int(source.get("priority", 1))
        self.detail_fetch_count = 0

    # ------------------------------------------------------------- helpers
    def soup(self, html: str) -> BeautifulSoup:
        for parser in ("lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:  # pragma: no cover
                continue
        return BeautifulSoup(html, "html.parser")

    def fetch(self, url: str, *, render: bool = False) -> tuple[str | None, str | None, int]:
        res = self.web.fetch(url, source=self.id, dynamic=render, render=render)
        if res.from_cache:
            self._cache_hits = getattr(self, "_cache_hits", 0) + 1
        if not res.ok:
            return None, res.error, res.status
        return res.text, None, res.status

    def clean(self, node: Any) -> str:
        return clean_text(str(node))

    def scrape(self) -> ScrapeResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # --------------------------------------------------- candidate finishing
    def finish(self, result: ScrapeResult, candidates: Iterable[Candidate]) -> ScrapeResult:
        self._flush_touches()
        seen = set()
        for cand in candidates:
            if not cand.title or not cand.url or cand.key() in seen:
                continue
            if cand.skip:
                seen.add(cand.key())
                result.add(cand)
                result.items_skipped += 1
                continue
            seen.add(cand.key())
            cand.priority = self.priority
            result.add(cand)
        if not result.candidates and result.status == "ok":
            result.status = "empty" if not result.errors else "blocked"
        return result


# --------------------------------------------------------------------------- #
# kind: listing  (CSS selectors over an HTML list page)
# --------------------------------------------------------------------------- #
    _touched: set[str] = set()

    def _flush_touches(self) -> None:
        """Record that this source (re)published the rows we skipped by hash.

        Without this, a programme that is *always* skipped never learns its other
        publishers, and `seen_in_sources` stays a single id for every row — which
        both loses the "seen in 3 places" signal and re-fetches the same detail
        page once per category listing it appears in.
        """
        if not self._touched:
            return
        try:
            from db.models import Scholarship, get_session, merge_sources
            from sqlalchemy import select

            session = get_session()
        except Exception:
            return
        urls = sorted(self._touched)
        try:
            for row in session.scalars(select(Scholarship).where(Scholarship.normalized_url.in_(urls))):
                merged = merge_sources(row.seen_in_sources or row.source, self.id)
                if merged != row.seen_in_sources:
                    row.seen_in_sources = merged
                row.times_seen = (row.times_seen or 0) + 1
            session.commit()
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never fail a scrape
            log.debug("touch merge skipped: %s", exc)
            try:
                session.rollback()
            except Exception:
                pass
        self._touched.clear()


class ListingScraper(BaseScraper):
    kind = "listing"
    DEFAULT_EXCLUDE = ("Share", "Print", "Bookmark", "Facebook", "Twitter", "WhatsApp", "LinkedIn", "Email")

    def scrape(self) -> ScrapeResult:
        res = ScrapeResult(self.id)
        self.detail_fetch_count = 0
        self.known = self._load_known()
        self._touched: set[str] = set()
        cfg = self.source
        urls = [cfg["url"]]
        for page in range(2, int(cfg.get("pages", 1)) + 1):
            tmpl = cfg.get("page_url", "{url}page/{page}/")
            try:
                urls.append(tmpl.format(url=cfg["url"], page=page))
            except Exception:  # pragma: no cover
                pass
        out: list[Candidate] = []
        for url in urls:
            html, err, _status = self.fetch(url, render=bool(cfg.get("render")))
            if html is None:
                res.errors.append(f"{url}: {err}")
                continue
            try:
                out.extend(self.parse_listing(html, url, cfg))
                res.detail_fetches = self.detail_fetch_count
            except Exception as exc:  # noqa: BLE001 - a bad selector must not kill the run
                res.errors.append(f"parse error on {url}: {type(exc).__name__}: {exc}")
        return self.finish(res, out)

    def _load_known(self) -> dict[str, tuple]:
        """normalized_url → stored content_hash, for URLs this source published.

        Checked *before* any detail-page fetch: if we already store the exact page
        bytes, there is nothing new to learn, so a re-run costs a listing fetch and
        nothing else. Also indexed by the alternate URLs recorded during
        cross-source dedupe, so a programme seen via another category is skipped too.
        """
        try:
            from db.models import Scholarship, get_session

            session = get_session()
        except Exception:  # first ever run, or CLI preview mode with no DB
            return {}
        import json as _json

        known: dict[str, tuple] = {}
        for url, chash, seen, extras, alt in session.query(
            Scholarship.normalized_url, Scholarship.content_hash, Scholarship.seen_in_sources, Scholarship.extras, Scholarship.url
        ).yield_per(500):
            publishers = set((seen or "").split(",")) | {self.id}
            if self.id not in publishers:
                continue
            if chash:
                known[url] = chash
                if alt:
                    known[alt] = chash
            for extra in (_json.loads(extras) if isinstance(extras, str) and extras else (extras or {})).get("alt_urls", []):
                if chash:
                    known[extra] = chash
        return known

    def parse_listing(self, html: str, page_url: str, cfg: dict) -> list[Candidate]:
        soup = self.soup(html)
        sel = cfg.get("item_selector", ".post")
        nodes = soup.select(sel)
        limit = int(cfg.get("limit", 40))
        out: list[Candidate] = []
        detail_fetch = 0
        max_detail = int(cfg.get("max_detail_fetches", 60))
        for node in nodes[: limit * 3]:
            a = node.select_one(cfg.get("link_selector", "a[href]")) or (node if node.name == "a" else None)
            if not a or not a.get("href"):
                continue
            title = (a.get_text(" ", strip=True) or "").strip()
            title_sel = cfg.get("title_selector")
            if title_sel:
                tnode = node.select_one(title_sel)
                if tnode:
                    title = tnode.get_text(" ", strip=True).strip()
            title = clean_title(title)
            if len(title) < 12 or any(word in title for word in self.DEFAULT_EXCLUDE):
                continue
            url = a["href"].strip()
            if url.startswith("/"):
                base = re.match(r"https?://[^/]+", page_url)
                url = (base.group(0) if base else "") + url
            listing_text = self.clean(node)
            cand = Candidate(source=self.id, title=title[:400], url=url, listing_text=listing_text)
            known = getattr(self, "known", None) or {}
            hit = known.get(normalize_url_static(url))
            if hit and cfg.get("skip_known", True):
                # we have already stored this exact page: no detail fetch, no parse
                cand.skip = True
                cand.content_hash = hit
                self._touched.add(normalize_url_static(url))
                out.append(cand)
                continue
            # Cheap pre-parse of the listing row. If it already carries the facts that
            # only live on the detail page (funding / amount / coverage), we skip that
            # fetch — which is what turns a 500-page crawl into ~40 pages.
            from parsers.fields import extract_all

            quick = extract_all(listing_text, title=title)
            cand.pre = {k: v for k, v in quick.as_dict().items() if v not in (None, "", [], False) and k != "title"}
            want = cfg.get("detail_when_missing", ["funding_type", "amount_usd", "coverage"])
            missing = [f for f in want if not cand.pre.get(f)]
            if cfg.get("fetch_detail", True) and len(missing) > int(cfg.get("min_listing_score", 1)) and detail_fetch < max_detail:
                html2, _err2, _ = self.fetch(url)
                detail_fetch += 1
                self.detail_fetch_count += 1
                if html2:
                    art = None
                    for sel in cfg.get("detail_selectors", ["article", ".post", "main", "body"]):
                        try:
                            art = self.soup(html2).select_one(sel)
                        except Exception:  # bad selector in config → ignore it
                            art = None
                        if art:
                            break
                    cand.page_text = self.clean(art or html2)[:16000]
                    # hash the *content we keep*, so cookies/banners/timestamps
                    # elsewhere on the page cannot make every row look "changed"
                    cand.content_hash = hashlib.sha256(cand.page_text.encode("utf-8", "ignore")).hexdigest()[:32]
                    cand.fetched = True
            out.append(cand)
        return out


# --------------------------------------------------------------------------- #
# kind: table  (aggregator pages that publish one row per scholarship)
# --------------------------------------------------------------------------- #
class TableScraper(BaseScraper):
    kind = "table"

    def scrape(self) -> ScrapeResult:
        res = ScrapeResult(self.id)
        cfg = self.source
        html, err, _ = self.fetch(cfg["url"], render=bool(cfg.get("render")))
        if html is None:
            res.errors.append(err or "no html")
            return self.finish(res, [])
        soup = self.soup(html)
        rows = soup.select(cfg.get("row_selector", "table tbody tr")) or soup.select("tr")
        cols = cfg.get("columns", {})
        out: list[Candidate] = []
        for tr in rows:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            idx_map = {k: _int(v) for k, v in cols.items()}

            def get(name: str, _cells=cells, _idx=idx_map) -> str | None:
                i = _idx.get(name)
                if i is None or i >= len(_cells):
                    return None
                return _cells[i].get_text(" ", strip=True) or None

            title = get("title")
            link_node = None
            if idx_map.get("title") is not None and idx_map["title"] < len(cells):
                link_node = cells[idx_map["title"]].select_one("a[href]")
            url = (link_node.get("href") if link_node is not None else None) or (get("url") or "")
            url = (url or "").strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url and not url.startswith(("http://", "https://")) and not url.startswith("/"):
                from urllib.parse import urljoin

                url = urljoin(cfg["url"], url)
            elif url.startswith("/"):
                base = re.match(r"https?://[^/]+", cfg["url"])
                url = (base.group(0) if base else "") + url
            if not title or len(title) < 8 or not url.startswith("http"):
                continue
            pre: dict[str, Any] = {}
            if (dl := get("deadline")):
                from parsers.fields import parse_deadline

                parsed = parse_deadline(dl)
                if parsed["deadline"]:
                    pre["deadline"] = parsed["deadline"]
                    pre["deadline_text"] = dl
            if (val := get("value")):
                from parsers.fields import parse_amounts

                amounts = parse_amounts(val)
                pre.update({k: v for k, v in amounts.items() if v})
                if re.search(r"full", val, re.IGNORECASE):
                    pre["funding_type"] = "Full"
            if (ctry := get("country")):
                pre["country"] = ctry
            if (level := get("level")):
                from parsers.labels import degree_from_label

                pre["degree_level"] = degree_from_label(level) or level[:60]
            out.append(
                Candidate(
                    source=self.id,
                    title=re.sub(r"\s+", " ", title)[:400],
                    url=url,
                    listing_text="\n".join(x for x in (title, get("deadline"), get("value"), get("country"), get("level")) if x),
                    pre=pre,
                )
            )
        return self.finish(res, out[: int(cfg.get("limit", 120))])


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# kind: rss / atom  (the most robust option — prefer it when a site offers it)
# --------------------------------------------------------------------------- #
class RSSScraper(BaseScraper):
    kind = "rss"

    def scrape(self) -> ScrapeResult:
        res = ScrapeResult(self.id)
        cfg = self.source
        xml, err, _ = self.fetch(cfg["url"])
        if xml is None:
            res.errors.append(err or "no feed")
            return self.finish(res, [])
        try:
            root = ET.fromstring(xml[: 3_000_000])
        except ET.ParseError as exc:
            res.errors.append(f"feed parse error: {exc}")
            return self.finish(res, [])
        out: list[Candidate] = []
        for item in root.iter():
            tag = _local(item.tag)
            if tag not in {"item", "entry"}:
                continue
            fields: dict[str, str] = {}
            for child in item:
                ctag = _local(child.tag)
                if ctag in {"title", "link", "description", "content", "published", "date", "categories", "category", "summary"}:
                    if ctag == "link" and child.get("href") and not (child.text or "").strip():
                        fields["link"] = child.get("href")
                    else:
                        fields[ctag] = clean_text(child.text or "", max_chars=4000)
            title = (fields.get("title") or "").strip()
            link = (fields.get("link") or "").strip()
            if not title or not link:
                continue
            out.append(
                Candidate(
                    source=self.id,
                    title=clean_title(title),
                    url=link,
                    listing_text="\n".join(v for v in fields.values() if v)[:6000],
                )
            )
        return self.finish(res, out[: int(cfg.get("limit", 40))])


# only pipes/bullets are treated as site-name separators — a colon or dash may
# be part of a legitimate title ("Nursing - Advanced Practice Scholarship")
TRAILING_SITE_RE = re.compile(r"\s*[|·•]\s*(?:the\s+)?[A-Z][^|·•]{0,40}$")


def clean_title(title: str) -> str:
    """Strip navigation noise and a trailing "| Site Name" suffix."""
    t = re.sub(r"\s+", " ", title or "").strip()
    for _ in range(2):
        stripped = TRAILING_SITE_RE.sub("", t)
        if stripped == t:
            break
        t = stripped
    return t.strip(" -|·•")[:400]


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


# --------------------------------------------------------------------------- #
# kind: json_api  (open data endpoints: EACEA projects, UNESCO, OPA… )
# --------------------------------------------------------------------------- #
class JSONAPIScraper(BaseScraper):
    kind = "json_api"

    def scrape(self) -> ScrapeResult:
        res = ScrapeResult(self.id)
        cfg = self.source
        url = cfg["url"]
        method = cfg.get("method", "GET").upper()
        payload = cfg.get("payload")
        raw: Any = None
        collected: list[Candidate] = []
        for page in range(int(cfg.get("pages", 1))):
            page_url = url.format(page=page, offset=page * int(cfg.get("page_size", 100)), size=cfg.get("page_size", 100))
            try:
                if method == "POST":
                    body = dict(payload or {})
                    if "{page}" in json.dumps(body) or "{offset}" in json.dumps(body):
                        body = json.loads(json.dumps(body).replace("{page}", str(page)).replace("{offset}", str(page * int(cfg.get("page_size", 100)))))
                    raw = self.web.post_json(page_url, body, headers={"Accept": "application/json"}, source=self.id)
                else:
                    raw = self.web.get_json(page_url, source=self.id)
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"{page_url}: {exc}")
                raw = None
            if not raw:
                break
            try:
                collected.extend(self._rows(raw, cfg))
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"row mapping failed: {exc}")
            if not cfg.get("paginate"):
                break
        return self.finish(res, collected)

    def _rows(self, raw: Any, cfg: dict) -> list[Candidate]:
        path = cfg.get("path", "$")
        rows = _jget(raw, path) or []
        if isinstance(rows, dict):
            rows = [rows]
        out: list[Candidate] = []
        mapping = cfg.get("fields", {})
        for row in rows:
            if not isinstance(row, dict):
                continue
            vals = {key: _jget(row, expr) for key, expr in mapping.items()}
            title = _first(vals.get("title"))
            url = _first(vals.get("url"))
            if not title or not url:
                continue
            pre: dict[str, Any] = {}
            if vals.get("deadline"):
                from parsers.fields import parse_deadline

                parsed = parse_deadline(str(vals["deadline"]))
                pre["deadline"] = parsed["deadline"]
                pre["deadline_text"] = str(vals["deadline"])[:120]
            for k in ("funding_type", "degree_level", "field", "provider", "university"):
                if _first(vals.get(k)):
                    pre[k] = _first(vals[k])
            for k in ("min_gpa", "min_ielts", "amount_usd", "max_age"):
                v = _first(vals.get(k))
                if isinstance(v, (int, float)):
                    pre[k] = float(v)
            ctext = " | ".join(str(_first(vals.get(k))) for k in ("summary", "description", "country") if _first(vals.get(k)))
            cand = Candidate(source=self.id, title=str(title)[:400], url=str(url), listing_text=ctext[:6000], pre=pre)
            if vals.get("country_list"):
                cl = vals["country_list"]
                if isinstance(cl, list):
                    cand.pre["eligible_countries"] = [str(c) for c in cl if c][:60]
            out.append(cand)
        return out[: int(cfg.get("limit", 200))]


def _jget(obj: Any, dotted: str) -> Any:
    """Support 'a.b.0.c' style paths and bare '$'."""
    if not dotted or dotted == "$":
        return obj
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        return None
    return cur


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


# --------------------------------------------------------------------------- #
# kind: seed  (curated YAML/JSON of high-value programmes + optional live page)
# --------------------------------------------------------------------------- #
class SeedScraper(BaseScraper):
    """Curated records for flagship programmes whose sites are bot-blocked from
    datacenter IPs (DAAD, Chevening, Fulbright, CSC…).  Being explicit beats
    silently scraping 0 items; each seed carries an `update_url` the bot checks
    for the deadline and a `review_before` date that nags you to refresh it.
    """

    kind = "seed"

    def scrape(self) -> ScrapeResult:
        res = ScrapeResult(self.id)
        cfg = self.source
        path = Path(cfg["file"])
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        if not path.exists():
            res.errors.append(f"seed file missing: {path}")
            return self.finish(res, [])
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            text = path.read_text(encoding="utf-8")
            try:
                import yaml

                data = yaml.safe_load(text)
            except Exception:
                res.errors.append(f"seed unreadable: {exc}")
                return self.finish(res, [])
        out: list[Candidate] = []
        now = datetime.utcnow()
        for entry in data.get("scholarships", []):
            review = entry.get("review_before")
            if review:
                try:
                    if datetime.fromisoformat(review) < now:
                        entry.setdefault("notes", "")
                        entry["notes"] = (str(entry.get("notes") or "") + f" [stale seed: verify {review}]").strip()
                except ValueError:
                    pass
            pre = {k: v for k, v in entry.items() if k not in {"title", "url", "update_url", "page_text"}}
            dl = pre.get("deadline")
            if isinstance(dl, str):
                pre["deadline"] = datetime.fromisoformat(dl)
            cand = Candidate(
                source=self.id,
                title=entry["title"],
                url=entry["url"],
                listing_text=entry.get("text") or " ".join(f"{k}: {v}" for k, v in entry.items() if isinstance(v, (str, int, float))),
                pre=pre,
            )
            if entry.get("update_url"):
                html, _err, _ = self.fetch(entry["update_url"])
                if html:
                    from parsers.fields import extract_all

                    live = extract_all(self.clean(html)[:8000], title=entry["title"])
                    for f in ("deadline", "deadline_text", "amount_text", "min_ielts"):
                        v = getattr(live, f)
                        if v and (f != "deadline" or (cand.pre.get("deadline") and v > cand.pre["deadline"])):
                            cand.pre[f] = v
                    cand.page_text = self.clean(html)[:8000]
                    cand.fetched = True
            out.append(cand)
        return self.finish(res, out)


SCRAPERS: dict[str, type[BaseScraper]] = {
    ListingScraper.kind: ListingScraper,
    TableScraper.kind: TableScraper,
    RSSScraper.kind: RSSScraper,
    JSONAPIScraper.kind: JSONAPIScraper,
    SeedScraper.kind: SeedScraper,
}


def build_scraper(source: dict[str, Any], web: Any, profile: Any | None = None) -> BaseScraper:
    kind = source.get("kind", "listing")
    try:
        cls = SCRAPERS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown scraper kind '{kind}' for source {source.get('id')}") from exc
    return cls(source, web, profile)
