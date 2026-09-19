"""The run: scrape → cheap parse → (LLM only for gaps) → validate → store →
score → notify → publish report.

The blueprint's `run_cycle` re-parsed every page with the LLM on every run,
notified on `notified=False` (so the very first row in the DB could email you
forever), and never noticed that a source had gone quiet.  This version is
idempotent, cost-aware and observable.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from db.models import (
    STATUS_ACTIVE,
    Scholarship,
    SourceRun,
    init_engine,
    mark_expired,
    normalize_url,
    record_hash,
    upsert_scholarship,
    utcnow,
)
from matching.matcher import evaluate
from notifiers import build_digest, deliver
from parsers.fields import RawExtraction, extract_all
from parsers.llm import LLMClient, from_config
from parsers.schema import ScholarshipRecord
from reporting import export as export_report
from scrapers import ScrapeResult, build_scraper

from .config import Settings
from .web import Web

log = logging.getLogger("pipeline")
_COLUMN_NAMES = {c.name for c in Scholarship.__table__.columns}


@dataclass
class RunSummary:
    run_id: str
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    source_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    duplicates: int = 0
    rejected: int = 0
    detail_fetches: int = 0
    cache_hits: int = 0
    llm_items: int = 0
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    llm_tokens: int = 0
    notified: dict[str, Any] = field(default_factory=dict)
    reports: dict[str, str] = field(default_factory=dict)
    lifecycle: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        d["started_at"] = self.started_at.isoformat(timespec="seconds")
        d["finished_at"] = self.finished_at.isoformat(timespec="seconds") if self.finished_at else None
        return d


def run(settings: Settings, *, sources: list[str] | None = None, notify: bool = True, offline: bool = False,
        force: bool = False, limit: int | None = None, export: bool = True) -> RunSummary:
    init_engine(settings.db_url)
    from db.session import get_session

    web = Web(
        cache_dir=Path(settings.runtime.get("cache_dir", "data/cache")),
        user_agent=settings.runtime.get("user_agent", "Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0.0.0 Safari/537.36"),
        timeout=float(settings.runtime.get("timeout", 25)),
        min_interval=float(settings.runtime.get("min_interval_seconds", 2.0)),
        jitter=float(settings.runtime.get("jitter_seconds", 2.0)),
        retries=int(settings.runtime.get("retries", 2)),
        respect_robots=bool(settings.runtime.get("respect_robots", False)),
        offline=offline,
        allow_playwright=bool(settings.runtime.get("allow_playwright", True)),
        max_age_hours=float(settings.runtime.get("cache_fresh_hours", 0)),
    )
    llm = from_config(settings, Path(settings.runtime.get("cache_dir", "data/cache")) / "parse_cache.sqlite3", offline=offline)
    summary = RunSummary(run_id=utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    session = get_session()

    wanted = set(sources) if sources else None
    enabled = [s for s in settings.enabled_sources if not wanted or s.get("id") in wanted]
    if wanted:
        missing = wanted - {s.get("id") for s in enabled}
        if missing:
            log.warning("unknown/disabled sources requested: %s", ", ".join(sorted(missing)))

    candidates_by_source: dict[str, ScrapeResult] = {}
    for src_cfg in enabled:
        sid = src_cfg.get("id", "?")
        t0 = time.monotonic()
        run_row = SourceRun(run_id=summary.run_id, source=sid)
        try:
            scraper = build_scraper(src_cfg, web, settings.profile)
            res = scraper.scrape()
        except Exception as exc:
            log.exception("source %s crashed", sid)
            res = ScrapeResult(sid)
            res.errors.append(f"{type(exc).__name__}: {exc}")
        candidates_by_source[sid] = res
        cands = res.candidates[: limit] if limit else res.candidates
        stats = _process_candidates(session, cands, settings, llm, summary, src_cfg)
        run_row.items_found = len(res.candidates)
        run_row.items_new = stats["new"]
        run_row.items_changed = stats["changed"]
        run_row.items_skipped = stats["unchanged"]
        run_row.items_error = len(res.errors)
        run_row.detail_fetches = res.detail_fetches
        run_row.cache_hits = getattr(scraper, "_cache_hits", 0)
        run_row.llm_calls = stats["llm_calls"]
        run_row.llm_tokens_in = stats["tokens_in"]
        run_row.llm_tokens_out = stats["tokens_out"]
        run_row.llm_cost_usd = stats["cost"]
        run_row.duration_ms = int((time.monotonic() - t0) * 1000)
        run_row.status = "ok" if res.candidates and not res.errors else ("blocked" if res.errors and not res.candidates else ("empty" if not res.candidates else "ok"))
        run_row.error = "; ".join(res.errors)[:1500] or None
        summary.sources_failed.append(f"{sid} ({run_row.status})") if run_row.status != "ok" else summary.sources_ok.append(sid)
        summary.source_details[sid] = {"status": run_row.status, "found": run_row.items_found, "new": run_row.items_new, "changed": run_row.items_changed, "error": run_row.error}
        session.add(run_row)
        session.commit()
        log.info(
            "source %-18s %s  found=%-3d new=%-2d changed=%-2d skipped=%-3d llm=%d $%.3f  %dms",
            sid, run_row.status, run_row.items_found, run_row.items_new, run_row.items_changed,
            run_row.items_skipped, run_row.llm_calls, run_row.llm_cost_usd, run_row.duration_ms,
        )

    # ---- lifecycle ----
    summary.lifecycle = mark_expired(session, stale_days=int(settings.runtime.get("stale_days", 45)))
    # an "unchanged" row has already cleared its edge flags inside upsert; flush here
    session.commit()

    # ---- score everything active ----
    scored_rows = _score_all(session, settings)

    # ---- notify ----
    if notify:
        stats = {
            "sources_ok": len(summary.sources_ok),
            "sources_failed": summary.sources_failed,
            "new": summary.new,
            "changed": summary.changed,
            "llm_calls": summary.llm_calls,
            "llm_cost_usd": summary.llm_cost_usd,
        }
        pool = _notification_pool(session, scored_rows, force=force, settings=settings)
        digest = build_digest(pool, settings.profile, settings=settings, stats=stats,
                              max_rows=int(settings.notify.get("max_rows", 12)))
        summary.notified = deliver(digest, settings, session, force=force)
        if digest is not None:
            for row in digest.rows:
                row.scholarship.notified = True
                row.scholarship.notify_count = (row.scholarship.notify_count or 0) + 1
            session.commit()
            if settings.notify.get("channels") and "file" in settings.notify["channels"]:
                (Path(settings.runtime.get("out_dir", "data/out")) / "last_digest.md").write_text(digest.markdown(), encoding="utf-8")
    session.close()

    # ---- publish ----
    if export:
        out_dir = Path(settings.runtime.get("out_dir", "data/out"))
        session2 = get_session()
        files = export_report(session2, out_dir, limit=int(settings.runtime.get("report_limit", 400)))
        summary.reports = {k: str(v) for k, v in files.items()}
        session2.close()

    # clear one-shot change flags after the cycle so the next run starts clean
    session3 = get_session()
    for row in session3.query(Scholarship).filter(Scholarship.changed.is_(True)).yield_per(500):
        row.changed = False
        row.change_type = None
    session3.commit()
    session3.close()

    summary.finished_at = utcnow()
    summary.llm_calls = llm.usage.calls
    summary.llm_cost_usd = round(llm.usage.cost_usd, 5)
    summary.llm_tokens = llm.usage.tokens_in + llm.usage.tokens_out
    llm_items = int(getattr(llm, "_batched_items", 0) or 0)
    summary.llm_items = llm_items or llm.usage.items
    _log_summary(summary, llm)
    return summary


# --------------------------------------------------------------------------- #
# per-source processing
# --------------------------------------------------------------------------- #
def _process_candidates(session, cands, settings: Settings, llm: LLMClient, summary: RunSummary, src_cfg: dict) -> dict[str, Any]:
    stats = {"new": 0, "changed": 0, "unchanged": 0, "duplicates": 0, "rejected": 0, "llm_calls": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0, "llm_items": 0}
    pending_llm: list[tuple[str, str, str]] = []
    staged: dict[str, dict[str, Any]] = {}
    skip_if_unchanged = bool(settings.runtime.get("skip_unchanged", True))

    for cand in cands:
        if getattr(cand, "skip", False):
            stats["unchanged"] += 1
            summary.unchanged += 1
            summary.candidates += 1
            continue
        merged_text = cand.merged_text()
        if not merged_text or len(merged_text) < 40:
            stats["rejected"] += 1
            continue
        norm = normalize_url(cand.url)
        existing = session.query(Scholarship).filter(Scholarship.normalized_url == norm).first()

        pre = {k: v for k, v in (cand.pre or {}).items() if k != "title" and v not in (None, "", [], False)}
        ex = extract_all(merged_text, title=cand.title, home_country=settings.profile.home_country)
        # explicit per-source metadata (listing row / seed file / API) beats prose guesses
        for key, value in pre.items():
            if hasattr(ex, key) and not key.startswith("_"):
                setattr(ex, key, value)
        summary.candidates += 1
        record = _to_record(ex, cand, pre=pre)
        needs, missing = _needs_llm(record, settings)
        record = _postprocess(record, settings)

        # The skip test is on the *parsed facts*, not the page bytes: a site can
        # rewrite its footer or expire a cache entry without any programme
        # changing. This is what makes a re-run (or an offline re-run) a no-op.
        facts_hash = record_hash(record.db_fields(source=cand.source, url=cand.url))
        if existing is not None and skip_if_unchanged and existing.raw_hash == facts_hash:
            existing.times_seen = (existing.times_seen or 0) + 1
            existing.last_seen_at = utcnow()
            if cand.content_hash:
                existing.content_hash = cand.content_hash
            # record *which* source republished it, otherwise a programme that is
            # always skipped never learns its other publishers and every source
            # keeps re-fetching its detail page
            from db.models import merge_sources

            existing.seen_in_sources = merge_sources(existing.seen_in_sources, cand.source)
            stats["unchanged"] += 1
            summary.unchanged += 1
            session.commit()
            continue

        if needs and llm.enabled:
            pending_llm.append((cand.key(), cand.title, merged_text))
            staged[cand.key()] = {"candidate": cand, "record": record, "missing": missing, "facts_hash": facts_hash}
        else:
            staged[cand.key()] = {"candidate": cand, "record": record, "missing": missing, "facts_hash": facts_hash}

    if pending_llm and llm.enabled:
        try:
            results = llm.parse_batch(pending_llm, llm.cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm batch failed, continuing regex-only: %s", exc)
            results = {}
        stats["llm_calls"] = llm.usage.calls
        stats["cost"] = llm.usage.cost_usd
        stats["tokens_in"] = llm.usage.tokens_in
        stats["tokens_out"] = llm.usage.tokens_out
        stats["llm_items"] = len(pending_llm)
        for key, payload in (results or {}).items():
            entry = staged.get(key)
            if not entry or not payload:
                continue
            try:
                llm_rec = ScholarshipRecord.model_validate({**payload, "title": entry["record"].title})
            except Exception:  # noqa: BLE001
                continue
            merged = llm_rec.merged_over(entry["record"])
            merged.notes = ((merged.notes or "") + f" llm_filled={','.join(entry.get('missing', []))}").strip()
            entry["record"] = merged
            entry["llm_used"] = True

    for key, entry in staged.items():
        cand: Any = entry["candidate"]
        record: ScholarshipRecord = entry["record"]
        record = _postprocess(record, settings)
        if not record.title or len(record.title) < 6:
            stats["rejected"] += 1
            continue
        if record.deadline and record.deadline < utcnow().date() and not record.deadline_rolling:
            summary.candidates += 1
            # keep expired rows in the DB but do not treat them as new
            _store(session, record, cand, entry, settings, is_new_override=False)
            stats["unchanged"] += 1
            continue
        try:
            _row, outcome = _store(session, record, cand, entry, settings)
        except ValueError as exc:  # e.g. missing url/title — never kill the run
            log.debug("rejected %s: %s", cand.url, exc)
            stats["rejected"] += 1
            continue
        summary.candidates += 1
        if outcome == "new":
            stats["new"] += 1
            summary.new += 1
        elif outcome == "changed":
            stats["changed"] += 1
            summary.changed += 1
        elif outcome == "duplicate":
            stats["duplicates"] += 1
            summary.duplicates += 1
        else:
            stats["unchanged"] += 1
            summary.unchanged += 1
    summary.detail_fetches += sum(1 for e in staged.values() if e["candidate"].fetched)
    summary.llm_items += stats["llm_items"]
    session.commit()
    return stats


def _to_record(ex: RawExtraction, cand: Any, *, pre: dict[str, Any] | None = None) -> ScholarshipRecord:
    """RawExtraction (+ any explicit per-source metadata) → validated record.

    Everything is funnelled through Pydantic, so a scraper that hands us a date
    string, a weird degree label or an over-long title is normalised instead of
    crashing the run or poisoning the DB.
    """
    pre = pre if pre is not None else (cand.pre or {})
    payload: dict[str, Any] = {}
    for key, value in ex.as_dict().items():
        if key in ScholarshipRecord.model_fields:
            payload[key] = value
    for key in ("eligible_countries", "excluded_countries", "notes", "last_updated"):
        if pre.get(key):
            payload[key] = pre[key]
    payload["title"] = cand.title or payload.get("title")
    for key, value in pre.items():
        if key in ScholarshipRecord.model_fields and key != "title":
            payload[key] = value
    payload.setdefault("title", cand.title or "untitled")
    try:
        return ScholarshipRecord.model_validate(payload)
    except Exception as exc:
        # Drop the single field that failed validation, keep the rest of the row.
        log.debug("record repair for %s: %s", cand.url, str(exc)[:160])
        for field_name in sorted(payload):
            probe = dict(payload)
            probe.pop(field_name, None)
            try:
                ScholarshipRecord.model_validate(probe)
            except Exception:  # noqa: BLE001 - not this field's fault
                continue
            payload = probe
            return ScholarshipRecord.model_validate(payload)
        raise


def candidate_to_fields(cand: Any, settings: Settings, *, use_llm: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scrape-level candidate → (DB field dict, diagnostics).

    Shared by `run()` and `cli.py test-source`, so what you preview in the
    terminal is exactly what would be stored — no second implementation to drift.
    """
    pre = {k: v for k, v in (cand.pre or {}).items() if k != "title" and v not in (None, "", [], False)}
    ex = extract_all(cand.merged_text(), title=cand.title, home_country=settings.profile.home_country)
    for key, value in pre.items():
        if hasattr(ex, key) and not key.startswith("_"):
            setattr(ex, key, value)
    record = _to_record(ex, cand, pre=pre)
    needs, missing = _needs_llm(record, settings)
    record = _postprocess(record, settings)
    fields = _store_fields(record, cand, settings, {"missing": missing, "llm_used": False})
    fields.pop("last_updated", None)
    if getattr(record, "last_updated", None):
        fields["extras"] = {**(fields.get("extras") or {}), "last_updated": record.last_updated.isoformat()}
    return fields, {"record": record, "missing": missing, "llm_needed": bool(needs and use_llm), "extraction": ex}


def _store_fields(record: ScholarshipRecord, cand: Any, settings: Settings, entry: dict) -> dict[str, Any]:
    from db.models import text_hash as _th

    fields = record.db_fields(
        source=cand.source,
        url=cand.url,
        description=(cand.page_text or cand.listing_text or "")[:60000],
        content_hash=cand.content_hash or _th(cand.page_text or cand.listing_text or "") or "",
        parse_method="label+regex" if not entry.get("missing") else ("label+regex+llm" if entry.get("llm_used") else "label+regex+gaps"),
        llm_used=bool(entry.get("llm_used")),
        llm_model=(settings.llm.get("model") if entry.get("llm_used") else None),
        extras={"missing_at_scrape": list(entry.get("missing") or [])},
    )
    fields.pop("notes", None)
    fields = {k: v for k, v in fields.items() if k in _COLUMN_NAMES}
    if record.notes:
        fields["extras"] = {**(fields.get("extras") or {}), "notes": record.notes}
    fields.setdefault("raw_hash", entry.get("facts_hash") or record_hash(fields))
    return fields


def _needs_llm(record: ScholarshipRecord, settings: Settings) -> tuple[bool, list[str]]:
    """Triage gate: tokens are the only part of this pipeline with a price tag."""
    if not settings.llm.get("enabled") or not settings.llm.get("api_key"):
        return False, []
    if not settings.llm.get("fill_gaps_only", True):
        return True, ["all"]
    return LLMClient.needs_llm(record)


def _postprocess(record: ScholarshipRecord, settings: Settings) -> ScholarshipRecord:
    """Normalise, and handle the single most dangerous failure mode of aggregator
    data: a stale *past* deadline on a programme that actually reopens yearly."""
    from matching.countries import canon

    today = utcnow().date()
    recurring = bool(record.deadline_rolling) or (record.round_label or "").lower().startswith("annual") or bool(
        re.search(r"\bannual|every year|each year|yearly", record.deadline_text or "", re.IGNORECASE)
    )
    if record.deadline and record.deadline < today:
        if recurring:
            rolled = record.deadline
            for _ in range(4):
                try:
                    rolled = rolled.replace(year=rolled.year + 1)
                except ValueError:  # Feb 29
                    rolled = rolled.replace(year=rolled.year + 1, day=28)
                if rolled > today:
                    break
            record.deadline = rolled
            record.deadline_estimated = True
            record.notes = ((record.notes or "") + f" listed deadline had passed; rolled to {rolled.isoformat()} (recurring cycle) — verify").strip()
        else:
            record.notes = ((record.notes or "") + " deadline shown as past — verify before acting").strip()
    elif not record.deadline and record.deadline_rolling:
        record.notes = ((record.notes or "") + " rolling admissions").strip()

    if record.funding_type == "Unknown" and not record.amount_usd:
        record.funding_type = None
    record.country = canon(record.country) or record.country
    if record.eligible_countries:
        cleaned = []
        for item in record.eligible_countries:
            text = str(item).strip()
            if len(text) < 3 or text.lower().startswith(("international student", "students of", "any ")):
                continue
            cleaned.append(text)
        record.eligible_countries = cleaned[:60]
    if record.application_fee_usd == 0:
        record.notes = ((record.notes or "") + " no application fee").strip()
    return record


def _store(session, record: ScholarshipRecord, cand: Any, entry: dict, settings: Settings, *, is_new_override: bool | None = None) -> tuple[Any, str]:
    from db.models import text_hash as _th

    fields = record.db_fields(
        source=cand.source,
        url=cand.url,
        description=(cand.page_text or cand.listing_text or "")[:60000],
        content_hash=cand.content_hash or _th(cand.page_text or cand.listing_text or "") or "",
        parse_method="label+regex" if not entry.get("missing") else ("label+regex+llm" if entry.get("llm_used") else "label+regex+gaps"),
        llm_used=bool(entry.get("llm_used")),
        llm_model=(settings.llm.get("model") if entry.get("missing") and settings.llm.get("api_key") else None),
        extras={"missing_at_scrape": entry.get("missing", [])},
    )
    fields.pop("notes", None)
    fields = {k: v for k, v in fields.items() if k in _COLUMN_NAMES}
    if record.notes:
        fields["extras"] = {**(fields.get("extras") or {}), "notes": record.notes}
    row, outcome = upsert_scholarship(session, **fields)
    if is_new_override is False and outcome == "new":
        row.is_new = False
    return row, outcome


# --------------------------------------------------------------------------- #
# scoring / notification pool
# --------------------------------------------------------------------------- #
def _score_all(session, settings: Settings) -> list[Scholarship]:
    weights = settings.scoring.get("weights")
    thresholds = settings.scoring.get("tiers")
    rows = session.query(Scholarship).filter(Scholarship.status == STATUS_ACTIVE).all()
    for row in rows:
        res = evaluate(row, settings.profile, weights=weights, thresholds=thresholds)
        row.match_score = res.score
        row.tier = res.tier
        row.confidence = res.confidence
        row.gate_pass = res.gate.passed
        row.gate_reasons = json.dumps({"reasons": res.gate.reasons, "warnings": res.gate.warnings}, ensure_ascii=False)
        row.score_breakdown = res.as_json()
        row.quality_flags = _quality_flags(row, res)
    session.commit()
    return rows


def _quality_flags(row: ScholarshipRecord | Scholarship, res) -> str:
    flags = []
    if row.deadline_estimated:
        flags.append("deadline_estimated")
    if not row.amount_text:
        flags.append("no_amount")
    if not row.funding_type:
        flags.append("no_funding_type")
    if not row.eligible_countries or row.eligible_countries in ("[]", ""):
        flags.append("eligibility_unlisted")
    if res.confidence < 0.55:
        flags.append("low_confidence")
    return ",".join(flags)[:480]


def _notification_pool(session, scored_rows: list[Scholarship], *, force: bool, settings: Settings) -> list[Scholarship]:
    min_score = float(settings.notify.get("min_score", 45))
    per_source = int(settings.notify.get("max_per_source", 4))
    per_tier = settings.notify.get("tier_quota") or {}
    pool: list[Scholarship] = []
    counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    rows = [r for r in scored_rows if r.gate_pass and r.match_score >= min_score]
    rows.sort(key=lambda r: (-r.match_score, r.deadline or datetime.max))
    for row in rows:
        if not force and not (row.is_new or row.changed or row.notified is False):
            continue
        counts[row.source] = counts.get(row.source, 0) + 1
        if counts[row.source] > per_source:
            continue
        quota = int(per_tier.get(row.tier or "", 99))
        tier_counts[row.tier] = tier_counts.get(row.tier, 0) + 1
        if tier_counts[row.tier] > quota:
            continue
        pool.append(row)
    return pool


def _log_summary(summary: RunSummary, llm: LLMClient) -> None:
    dur = (summary.finished_at - summary.started_at).total_seconds() if summary.finished_at else 0
    log.info(
        "run %s done in %.1fs · candidates=%d new=%d changed=%d dup=%d rejected=%d · llm calls=%d items=%d tokens=%d cost=$%.4f · sources ok=%d fail=%d",
        summary.run_id, dur, summary.candidates, summary.new, summary.changed, summary.duplicates, summary.rejected,
        llm.usage.calls, summary.llm_items, llm.usage.tokens_in + llm.usage.tokens_out, llm.usage.cost_usd,
        len(summary.sources_ok), len(summary.sources_failed),
    )
    if llm.usage.cache_hits:
        log.info("llm cache hits: %d (saved ≈$%.3f)", llm.usage.cache_hits, llm.usage.cache_hits * 0.0004)
    if llm.usage.budget_exhausted:
        log.warning("LLM budget exhausted — remaining items were parsed with regex only")
