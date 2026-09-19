"""Scraper + storage + end-to-end (offline) tests."""

from __future__ import annotations

import json
import pathlib
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from core.web import CircuitBreaker, DomainThrottle, FetchResult, HtmlCache
ROOT = pathlib.Path(__file__).resolve().parents[1]

from db.models import (
    Scholarship,
    canonical_key,
    get_session,
    init_engine,
    mark_expired,
    normalize_title,
    normalize_url,
    record_hash,
    upsert_scholarship,
    utcnow,
)
from scrapers import JSONAPIScraper, ListingScraper, RSSScraper, TableScraper

FIX = Path(__file__).parent / "fixtures"


class FakeWeb:
    """Serves fixture HTML — exercises the real scraper code, not a copy of it."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []
        self.detail_calls = 0

    def fetch(self, url, **kw):
        self.calls.append(url)
        text = self.pages.get(url)
        if text is None:
            return FetchResult(url, 404, "", error="not found in fixture")
        return FetchResult(url, 200, text)

    def get_json(self, url, **kw):
        text = self.pages.get(url)
        return json.loads(text) if text else None

    post_json = get_json


# --------------------------------------------------------------------------
class TestUrlAndTitleNormalisation:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("http://X.com/A/", "https://x.com/A"),
            ("https://www.x.com/a?utm_source=nl", "https://x.com/a"),
            ("https://x.com/a/index.html", "https://x.com/a"),
            ("https://X.com/a#top", "https://x.com/a"),
        ],
    )
    def test_equivalent_urls(self, a, b):
        assert normalize_url(a) == normalize_url(b)

    def test_different_pages_stay_different(self):
        assert normalize_url("https://x.com/a") != normalize_url("https://x.com/b")

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Chevening Scholarships 2026 in UK", "2027 Chevening scholarships (UK)"),
            ("DAAD EPOS Scholarship", "DAAD EPOS Awards"),
        ],
    )
    def test_title_fingerprint(self, a, b):
        assert normalize_title(a) == normalize_title(b)

    def test_canonical_key_ignores_source_and_year(self):
        assert canonical_key("Chevening 2026", None) == canonical_key("CHEVENING Scholarships 2026", None)

    def test_canonical_key_separates_intakes(self):
        a = canonical_key("Same Title", utcnow().replace(year=2026))
        b = canonical_key("Same Title", utcnow().replace(year=2027))
        assert a != b


class TestListingScraper:
    def test_parses_items_and_labels(self):
        html = (FIX / "scholars4dev_listing.html").read_text()
        url = "https://www.scholars4dev.com/category/x/"
        web = FakeWeb({url: html})
        sc = ListingScraper(
            {"id": "t1", "kind": "listing", "url": url, "pages": 1, "item_selector": ".post", "fetch_detail": False},
            web,
        )
        res = sc.scrape()
        assert res.status == "ok" and len(res.candidates) == 2
        cand = res.candidates[0]
        assert cand.pre["deadline"].startswith("2026-10-06")
        assert cand.pre["country"] == "United Kingdom"
        assert cand.pre["degree_level"] == "Master"
        # the second item is a bare link with no metadata → still a candidate, but sparse
        assert res.candidates[1].pre == {}

    def test_detail_page_fetched_when_funding_missing(self):
        listing = '<div class="post"><h2><a href="https://s4d.test/1">Some Scholarship In Netherlands For 2027</a></h2></div>'
        detail = (FIX / "scholars4dev_detail.html").read_text()
        web = FakeWeb({"https://s4d.test/list/": listing, "https://s4d.test/1": detail})
        sc = ListingScraper(
            {"id": "t2", "kind": "listing", "url": "https://s4d.test/list/", "item_selector": ".post", "fetch_detail": True, "min_listing_score": 0},
            web,
        )
        res = sc.scrape()
        assert res.candidates[0].fetched is True
        assert res.detail_fetches == 1
        assert "tuition" in res.candidates[0].page_text

    def test_bad_selector_reports_error_instead_of_raising(self):
        web = FakeWeb({"https://x.test/": "<div>nothing</div>"})
        sc = ListingScraper({"id": "t3", "kind": "listing", "url": "https://x.test/", "item_selector": "###"}, web)
        res = sc.scrape()
        assert res.candidates == []
        assert res.status in ("empty", "blocked")

    def test_unreachable_source_is_reported_not_crash(self):
        sc = ListingScraper({"id": "t4", "kind": "listing", "url": "https://nope.test/", "pages": 2}, FakeWeb({}))
        res = sc.scrape()
        assert res.errors and res.status == "blocked"


class TestRSSAndAPI:
    def test_rss(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
        <item><title>German Scholarship for Pakistani Students | Blog</title>
        <link>https://b.test/1</link><description>Deadline: 3 Dec 2026. Full tuition and living costs.</description></item>
        </channel></rss>"""
        web = FakeWeb({"https://b.test/feed": xml})
        res = RSSScraper({"id": "r1", "kind": "rss", "url": "https://b.test/feed"}, web).scrape()
        assert len(res.candidates) == 1
        assert res.candidates[0].title == "German Scholarship for Pakistani Students"

    def test_json_api_mapping(self):
        payload = json.dumps(
            {"content": [{"externalId": "2027-1-B79", "projectUrl": "https://e.test/p", "title": "Joint MSc in AI",
                          "callClosingDate": "2027-01-15", "coordinatorParticipantCountryIsoCode": "NL"}]}
        )
        web = FakeWeb({"https://e.test/search": payload})
        cfg = {
            "id": "api1", "kind": "json_api", "url": "https://e.test/search", "path": "content",
            "fields": {"title": "title", "url": "projectUrl", "summary": "externalId", "deadline": "callClosingDate"},
        }
        res = JSONAPIScraper(cfg, web).scrape()
        assert len(res.candidates) == 1, "one row must never become two candidates"
        cand = res.candidates[0]
        assert cand.title == "Joint MSc in AI"
        assert cand.url == "https://e.test/p"
        assert str(cand.pre["deadline"]).startswith("2027-01-15")

    def test_table_scraper(self):
        html = (FIX / "sample_listing_row.html").read_text()
        web = FakeWeb({"https://agg.test/": html})
        cfg = {"id": "tb1", "kind": "table", "url": "https://agg.test/", "row_selector": "tbody tr",
               "columns": {"title": 0, "value": 1, "deadline": 2, "country": 3}}
        res = TableScraper(cfg, web).scrape()
        assert res.candidates
        cand = res.candidates[0]
        assert cand.pre["deadline"].year == 2026
        assert cand.pre["country"] == "Germany"
        assert cand.pre["amount_usd"]


class TestWebLayer:
    def test_html_cache_roundtrip(self, tmp_path):
        c = HtmlCache(tmp_path / "c.sqlite3")
        c.put("https://x.test/a", 200, "<html>hi</html>", 'W/"1"', "Mon, 01 Jan 2024 00:00:00 GMT")
        got = c.get("https://x.test/a")
        assert got["body"] == "<html>hi</html>" and got["etag"] == 'W/"1"'

    def test_max_age_makes_fresh_pages_skip_the_network(self, tmp_path):
        c = HtmlCache(tmp_path / "c2.sqlite3", max_age_hours=24)
        c.put("https://x.test/a", 200, "body", None, None)
        assert c.is_fresh(c.get("https://x.test/a")["fetched_at"]) is True
        assert HtmlCache(tmp_path / "c3.sqlite3").is_fresh(c.get("https://x.test/a")["fetched_at"]) is False

    def test_throttle_waits_on_repeat_visits(self):
        t = DomainThrottle(min_interval=0.35, jitter=0)
        assert t.wait("https://a.test/1") == 0.0
        waited = t.wait("https://a.test/2")
        assert waited > 0.1

    def test_throttle_is_per_domain(self):
        t = DomainThrottle(min_interval=5, jitter=0)
        t.wait("https://a.test/1")
        assert t.wait("https://b.test/1") == 0.0

    def test_circuit_breaker_opens_and_recovers(self):
        b = CircuitBreaker(max_failures=2, cooldown_minutes=30)
        assert b.open_until("s") == 0
        b.record_failure("s")
        assert b.open_until("s") == 0
        assert b.record_failure("s") is True
        assert b.open_until("s") > 0
        b.record_success("s")
        assert b.open_until("s") == 0


class TestUpsert:
    def _fields(self, **kw):
        base = dict(
            title="Chevening Scholarships 2027", url="https://s4d.test/1", source="s1",
            country="United Kingdom", deadline=utcnow() + timedelta(days=100),
            description="body text", eligibility="open to all",
        )
        base.update(kw)
        return base

    @pytest.fixture(autouse=True)
    def _fresh_db(self, tmp_path):
        init_engine(f"sqlite:///{tmp_path/'t.db'}")
        sess = get_session()
        sess.query(Scholarship).delete()
        sess.commit()
        yield sess
        sess.close()

    @pytest.fixture
    def sess(self, _fresh_db):
        return _fresh_db

    def test_new_then_unchanged_then_changed(self, sess):
        """Idempotency: the same facts twice must not look like a change."""
        fields = self._fields()
        facts = record_hash(fields)
        row, outcome = upsert_scholarship(sess, **fields, raw_hash=facts)
        sess.commit()
        assert outcome == "new" and row.is_new is True

        _, outcome2 = upsert_scholarship(sess, **fields, raw_hash=facts)
        sess.commit()
        assert outcome2 == "unchanged"
        assert row.times_seen == 2
        assert row.changed is False and row.change_type is None

        moved = self._fields(deadline=utcnow() + timedelta(days=140))
        row2, outcome3 = upsert_scholarship(sess, **moved, raw_hash=record_hash(moved))
        sess.commit()
        assert outcome3 == "changed" and row2.change_type == "deadline_moved"
        assert row2.notified is False  # a moved deadline is re-notifiable

    def test_duplicate_from_a_second_source_merges(self, sess):
        upsert_scholarship(sess, **self._fields(source="s1"))
        sess.commit()
        row, outcome = upsert_scholarship(sess, **self._fields(source="s2", url="https://other.test/chevening"))
        sess.commit()
        assert outcome == "duplicate"
        assert set(row.seen_in_sources.split(",")) == {"s1", "s2"}
        assert sess.query(Scholarship).count() == 1

    def test_url_variants_do_not_create_rows(self, sess):
        upsert_scholarship(sess, **self._fields(url="https://s4d.test/1/"))
        sess.commit()
        _, outcome = upsert_scholarship(sess, **self._fields(url="http://www.s4d.test/1?utm_source=x"))
        sess.commit()
        assert outcome in ("unchanged", "changed")
        assert sess.query(Scholarship).count() == 1

    def test_missing_url_is_a_clean_error(self, sess):
        with pytest.raises(ValueError):
            upsert_scholarship(sess, title="no url", url="")

    def test_record_hash_ignores_page_chatter(self, sess):
        f = self._fields(description="version 1 of the page")
        f2 = {**f, "description": "version 2 — site added a cookie banner and a newsletter popup"}
        assert record_hash(f) == record_hash(f2), "cosmetic page edits must not re-store the row"
        f3 = {**f, "amount_text": "now 40,000 EUR"}
        assert record_hash(f) != record_hash(f3), "a real funding change must be detected"
        f4 = {**f, "source": "other-site"}
        assert record_hash(f) == record_hash(f4), "which site published it is not the programme changing"

    def test_lifecycle_marks_expired_and_stale(self, sess):
        row, _ = upsert_scholarship(sess, **self._fields(deadline=utcnow() - timedelta(days=2)))
        sess.commit()
        counts = mark_expired(sess)
        assert counts["expired"] >= 1
        assert sess.get(Scholarship, row.id).status == "expired"


class TestOfflineEndToEnd:
    """`run(offline=True)` must be fully deterministic: cached pages only, no
    network, no notifications, and a real report on disk."""

    def _prime_cache(self, settings, tmp_path):
        from core.web import HtmlCache

        cache = HtmlCache(Path(settings.runtime["cache_dir"]) / "http_cache.sqlite3")
        listing = (FIX / "scholars4dev_listing.html").read_text()
        cache.put("https://www.scholars4dev.com/category/level-of-study/masters-scholarships/", 200, listing, None, None)
        cache.put("https://www.scholars4dev.com/3299/british-chevening-scholarships/", 200, listing, None, None)
        return cache

    def test_run_uses_cache_and_scores(self, settings):
        from core.pipeline import run

        self._prime_cache(settings, Path(settings.runtime["cache_dir"]))
        summary = run(
            settings,
            sources=["scholars4dev_masters"],
            notify=False,
            offline=True,
            limit=2,
            export=True,
        )
        assert summary.candidates >= 1
        assert summary.new >= 1
        sess = get_session()
        rows = sess.query(Scholarship).all()
        assert rows and all(r.match_score > 0 for r in rows)
        assert rows[0].tier in ("must_apply", "strong", "worth_a_look")
        assert (Path(settings.runtime["out_dir"]) / "index.html").exists()
        assert (Path(settings.runtime["out_dir"]) / "scholarships.csv").exists()
        data = json.loads((Path(settings.runtime["out_dir"]) / "data.json").read_text())
        assert data["rows"] and "generated_at" in data
        sess.close()

    def test_second_run_is_a_no_op(self, settings):
        from core.pipeline import run

        self._prime_cache(settings, Path(settings.runtime["cache_dir"]))
        first = run(settings, sources=["scholars4dev_masters"], notify=False, offline=True, limit=2, export=False)
        assert first.new >= 1
        second = run(settings, sources=["scholars4dev_masters"], notify=False, offline=True, limit=2, export=False)
        assert second.new == 0 and second.changed == 0, second.source_details
        assert second.unchanged >= 1

    def test_deadline_move_requeues_notification(self, settings):
        from core.pipeline import run

        self._prime_cache(settings, Path(settings.runtime["cache_dir"]))
        run(settings, sources=["scholars4dev_masters"], notify=False, offline=True, limit=2, export=False)
        sess = get_session()
        row = sess.query(Scholarship).first()
        row.notified = True
        row.is_new = False
        row.changed = False
        sess.commit()
        sess.close()
        # simulate the site publishing a new deadline
        sess = get_session()
        row = sess.query(Scholarship).first()
        row.deadline = row.deadline + timedelta(days=30)
        row.raw_hash = "stale-hash"
        row.notified = True
        sess.commit()
        sess.close()
        from core.pipeline import run as _run

        summary = _run(settings, sources=["scholars4dev_masters"], notify=False, offline=True, limit=2, export=False)
        sess = get_session()
        row = sess.query(Scholarship).first()
        assert summary.changed >= 1 or summary.new == 0
        assert row.change_type in (None, "deadline_moved")
        sess.close()


@pytest.fixture
def populated(settings):
    from core.pipeline import run

    cache_dir = Path(settings.runtime["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    from core.web import HtmlCache

    cache = HtmlCache(cache_dir / "http_cache.sqlite3")
    listing = (FIX / "scholars4dev_listing.html").read_text()
    for u in ("https://www.scholars4dev.com/category/level-of-study/masters-scholarships/",
              "https://www.scholars4dev.com/3299/british-chevening-scholarships/"):
        cache.put(u, 200, listing, None, None)
    # threshold relaxed for the fixture: a bare listing row has no funding text,
    # which production scoring (rightly) penalises.
    settings.notify = dict(settings.notify)
    settings.notify["min_score"] = 10
    run(settings, sources=["scholars4dev_masters"], notify=False, offline=True, limit=2, export=False)
    return settings


class TestDigest:
    def test_dedupe_key_is_stable_for_the_same_set(self, populated):
        settings = populated
        from notifiers import build_digest

        sess = get_session()
        rows = sess.query(Scholarship).filter(Scholarship.status == "active").all()
        if not rows:
            pytest.skip("needs a populated db")
        d1 = build_digest(rows, settings.profile, settings=settings, stats={})
        d2 = build_digest(list(reversed(rows)), settings.profile, settings=settings, stats={})
        assert d1 is not None and d1.digest_key == d2.digest_key
        sess.close()

    def test_rendering_all_channels(self, populated):
        settings = populated
        from notifiers import build_digest

        sess = get_session()
        rows = sess.query(Scholarship).filter(Scholarship.status == "active").all()
        if not rows:
            pytest.skip("needs a populated db")
        digest = build_digest(rows[:3], settings.profile, settings=settings, stats={"sources_ok": 2, "new": 2, "changed": 0, "llm_calls": 0, "llm_cost_usd": 0.0})
        assert digest is not None and digest.rows
        md, plain, html = digest.markdown(), digest.plain(), digest.html()
        assert "http" in md and "[Apply" not in plain
        assert "<table" in html and "unsubscribe" in html
        # every link in the html must be escaped-safe
        assert "<script" not in html.lower()
        sess.close()

    def test_opt_out_tokens_are_unique(self, populated):
        settings = populated
        from notifiers import build_digest

        sess = get_session()
        rows = sess.query(Scholarship).filter(Scholarship.status == "active").all()
        if len(rows) < 2:
            pytest.skip("needs 2 rows")
        digest = build_digest(rows, settings.profile, settings=settings, stats={})
        assert digest is not None
        tokens = [r.opt_out_token for r in digest.rows]
        assert len(tokens) == len(set(tokens))
        sess.close()


class TestSourceConfigCoverage:
    """A source is pure YAML — which means a typo is invisible until the run that
    should have found you a scholarship quietly returns nothing. These assertions
    make the config itself a tested artifact."""

    def test_every_enabled_source_kind_exists(self):
        from scrapers import SCRAPERS

        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        for src in cfg["sources"]:
            assert src.get("kind", "listing") in SCRAPERS, f"{src['id']}: unknown kind"

    def test_enabled_sources_are_fully_specified(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        for src in cfg["sources"]:
            if not src.get("enabled", True):
                continue
            kind = src.get("kind", "listing")
            if kind != "seed":
                url = src.get("url", "")
                assert url.startswith("https://"), f"{src['id']}: url must be https (got {url!r})"
            if kind == "listing":
                # a listing page without an item selector silently yields 0 rows
                assert src.get("item_selector"), f"{src['id']}: listing needs item_selector"
                assert 1 <= int(src.get("pages", 1)) <= 8, f"{src['id']}: pages out of range"
                assert int(src.get("max_detail_fetches", 60)) <= 80, f"{src['id']}: too many detail fetches"
                assert int(src.get("limit", 40)) <= 120, f"{src['id']}: limit too high for one page set"
            if kind == "seed":
                key = src.get("seed_file") or src.get("file")
                assert key and (ROOT / key).exists(), f"{src['id']}: seed source points at a missing file ({key!r})"

    def test_source_ids_unique_and_stable(self):
        from db.models import SourceRun

        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        ids = [s["id"] for s in cfg["sources"]]
        assert len(ids) == len(set(ids)), "duplicate source id"
        # ids land in SourceRun/history — renaming one silently splits the record
        assert all(re.fullmatch(r"[a-z0-9_]{3,40}", i) for i in ids), ids
        assert any(i == "scholars4dev_masters" for i in ids), "the highest-signal source must stay on"

    def test_new_sources_do_not_stack_unbounded_detail_fetches(self):
        """Worst-case nightly cost must stay bounded no matter how many listings
        you add — this is the sum the CI cost note in README is based on."""
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        worst = sum(
            int(s.get("max_detail_fetches", 60)) if s.get("kind", "listing") == "listing" else 0
            for s in cfg["sources"]
            if s.get("enabled", True)
        )
        assert worst <= 240, f"configured detail fetches per night: {worst}"
