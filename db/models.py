"""SQLAlchemy 2.0 models.

Schema differences from the blueprint, and why:
  * `unique=True` on `url` is replaced by `normalized_url` + upsert logic.
    Trailing slashes / utm params used to create duplicate rows and, worse,
    a UNIQUE collision crashed the run.
  * content_hash + raw_hash → we skip re-parsing pages that did not change,
    which is the single biggest LLM-cost saving in the system.
  * `status` + `change_type` → "deadline moved" and "rolled to next intake"
    are actionable notifications; "we saw it again" is not.
  * canonical_key → cross-source dedupe (Chevening appears on 4 sites).
  * gate_reasons / score_breakdown / confidence → explainable matches.
  * llm_used / parse_confidence make prompt bugs visible immediately.
  * source_runs table → tells you WHICH source broke, not just "0 results".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, validates

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_OUTDATED = "outdated"
STATUS_STALE = "stale"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Scholarship(Base):
    __tablename__ = "scholarships"

    id: Mapped[int] = mapped_column(primary_key=True)

    # identity
    title: Mapped[str] = mapped_column(String(500))
    title_norm: Mapped[str] = mapped_column(String(500), index=True)
    canonical_key: Mapped[str] = mapped_column(String(80), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    normalized_url: Mapped[str] = mapped_column(String(1000), unique=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    seen_in_sources: Mapped[str] = mapped_column(String(300), default="")

    # programme facts
    provider: Mapped[str | None] = mapped_column(String(300))
    university: Mapped[str | None] = mapped_column(String(300))
    country: Mapped[str | None] = mapped_column(String(120))
    country_scope: Mapped[str | None] = mapped_column(String(40))  # any | worldwide | developed | list | specific
    eligible_countries: Mapped[str | None] = mapped_column(Text)
    excluded_countries: Mapped[str | None] = mapped_column(Text)
    degree_level: Mapped[str | None] = mapped_column(String(60))
    field: Mapped[str | None] = mapped_column(String(300))
    funding_type: Mapped[str | None] = mapped_column(String(40))  # Full | Partial | Tuition-only | Unknown
    coverage: Mapped[str | None] = mapped_column(Text)  # list of covered items
    amount_text: Mapped[str | None] = mapped_column(String(300))
    amount_usd: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(10))
    application_fee_usd: Mapped[float | None] = mapped_column(Float)
    intake: Mapped[str | None] = mapped_column(String(40))
    round_label: Mapped[str | None] = mapped_column(String(60))

    # dates
    open_date: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_text: Mapped[str | None] = mapped_column(String(200))
    deadline_rolling: Mapped[bool] = mapped_column(Boolean, default=False)
    deadline_estimated: Mapped[bool] = mapped_column(Boolean, default=False)

    # requirements (normalised so the matcher never parses prose)
    min_gpa: Mapped[float | None] = mapped_column(Float)
    gpa_scale: Mapped[str | None] = mapped_column(String(20))
    min_ielts: Mapped[float | None] = mapped_column(Float)
    min_toefl: Mapped[int | None] = mapped_column(Integer)
    requires_gre: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_work_experience: Mapped[bool] = mapped_column(Boolean, default=False)
    min_work_experience_years: Mapped[float | None] = mapped_column(Float)
    max_age: Mapped[int | None] = mapped_column(Integer)
    max_years_since_degree: Mapped[int | None] = mapped_column(Integer)
    requires_citizenship_of: Mapped[str | None] = mapped_column(String(300))
    age_limit_note: Mapped[str | None] = mapped_column(String(300))

    # text
    eligibility: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    # scoring / pipeline state
    match_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    tier: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    gate_pass: Mapped[bool] = mapped_column(Boolean, default=True)
    gate_reasons: Mapped[str | None] = mapped_column(Text)
    score_breakdown: Mapped[str | None] = mapped_column(Text)
    quality_flags: Mapped[str | None] = mapped_column(String(500))

    # provenance
    content_hash: Mapped[str | None] = mapped_column(String(40))   # hash of the fetched page (diagnostics)
    raw_hash: Mapped[str | None] = mapped_column(String(40))
    parse_method: Mapped[str | None] = mapped_column(String(30))
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[str | None] = mapped_column(String(80))
    extras: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_ACTIVE, index=True)
    # reader actions from the dashboard. Deliberately *not* `status`: the pipeline
    # resets status to active whenever it sees a row again, which would silently
    # un-hide anything you hid, and a control that stops working on the next run
    # is worse than no control.
    starred: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_new: Mapped[bool] = mapped_column(Boolean, default=True)
    changed: Mapped[bool] = mapped_column(Boolean, default=False)
    change_type: Mapped[str | None] = mapped_column(String(40))
    notified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_scholarships_status_deadline", "status", "deadline"),
        Index("ix_scholarships_tier_score", "tier", "match_score"),
    )

    # ------------------------------------------------------------- helpers
    @validates("eligible_countries", "excluded_countries", "coverage", "score_breakdown",
               "gate_reasons", "quality_flags")
    def _json_text_cols(self, key: str, value: Any) -> Any:
        """These columns are Text holding JSON.

        Handing one a real list/dict dies deep inside SQLite
        (`type 'list' is not supported`) on any write path that is not the
        pipeline, so encode on assignment. `extras` is a real JSON column and
        is deliberately excluded.
        """
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (set, tuple)):
            return json.dumps(list(value), ensure_ascii=False)
        return value

    @property
    def days_left(self) -> int | None:
        if not self.deadline:
            return None
        return (self.deadline - utcnow()).days

    @property
    def eligible_country_list(self) -> list[str]:
        return json.loads(self.eligible_countries or "[]")

    @property
    def coverage_list(self) -> list[str]:
        return json.loads(self.coverage or "[]")

    @property
    def gate_reason_list(self) -> list[str]:
        return json.loads(self.gate_reasons or "[]")

    def as_dict(self) -> dict[str, Any]:
        out = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for dt in ("deadline", "open_date", "first_seen_at", "last_seen_at", "updated_at"):
            if out.get(dt) is not None and hasattr(out[dt], "isoformat"):
                out[dt] = out[dt].isoformat(timespec="seconds")
        out["days_left"] = self.days_left
        return out


class SourceRun(Base):
    """Per-source, per-cycle telemetry. The blueprint's biggest blind spot."""

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_changed: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    items_error: Mapped[int] = mapped_column(Integer, default=0)
    detail_fetches: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="ok")  # ok | empty | blocked | error
    error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    channel: Mapped[str] = mapped_column(String(30))
    recipient: Mapped[str | None] = mapped_column(String(200))
    digest_key: Mapped[str | None] = mapped_column(String(80), index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="sent")  # sent | skipped | failed | dry-run
    detail: Mapped[str | None] = mapped_column(Text)


class OptOut(Base):
    """Self-service suppression: 'send me less like this' from the email footer."""

    __tablename__ = "opt_outs"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------- utilities
_TRACKED_FIELDS: tuple[str, ...] = tuple(
    c.name for c in Scholarship.__table__.columns
    if c.name
    not in {
        "id",
        "first_seen_at",
        "last_seen_at",
        "updated_at",
        "times_seen",
        "notified",
        "notify_count",
        "is_new",
        "changed",
        "change_type",
        "seen_in_sources",
    }
)

_TRACKED_SET = set(_TRACKED_FIELDS)
# `source` is provenance, not programme data: the same URL can sit in several
# categories of one site, and treating that as a change makes rows ping-pong
# between "changed" and "unchanged" on every run.
# cosmetic/provenance churn that must never count as "the programme changed"
# churn that must never count as "the programme changed": provenance hashes,
# parse bookkeeping, and every column the *scoring* pass owns (match_score, tier,
# confidence, gate_*, quality_flags). Scoring runs after storing, so comparing
# them here made rows look "changed" on every multi-source run.
NOISE_FIELDS = {
    "content_hash", "raw_hash", "parse_method", "quality_flags", "extras",
    "match_score", "tier", "confidence", "gate_pass", "gate_reasons", "score_breakdown",
}
NON_COMPARABLE = NOISE_FIELDS | {
    "source", "seen_in_sources", "normalized_url", "title_norm", "canonical_key",
    "first_seen_at", "last_seen_at", "updated_at", "times_seen", "notified", "notify_count",
}


_STRIP_PARAMS = re.compile(r"(utm_|ga_|ref|source|mc_|igshid|fbclid)", re.IGNORECASE)
_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9 ]+")


def normalize_url(url: str) -> str:
    """Lower-case host, drop fragment, drop tracking params, drop trailing slash."""
    if not url:
        return ""
    url = url.strip()
    url = re.sub(r"^http://", "https://", url, count=1)
    m = re.match(r"^(https?)://([^/]+)(/[^?#]*)?(?:\?([^#]*))?(?:#.*)?$", url)
    if not m:
        return url.lower().rstrip("/")
    _scheme, host, path, query = m.groups()
    host = host.lower()
    host = host.removeprefix("www.")
    path = path or "/"
    path = re.sub(r"index\.html?$", "", path)
    path = re.sub(r"/+$", "", path) or "/"
    kept = []
    if query:
        for part in query.split("&"):
            if not part or _STRIP_PARAMS.match(part.split("=")[0]):
                continue
            kept.append(part)
    q = "?".join(sorted(kept))
    return f"https://{host}{path}" + (f"?{q}" if q else "")


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = _WS.sub(" ", title.lower()).strip()
    t = t.replace("&amp;", "and").replace("’", "'").replace("‘", "'")
    t = re.sub(r"\b(scholarship|scholarships|award|awards|grant|grants|fellowship|fellowships|for|at|in|the|of|a|an)\b", " ", t)
    t = re.sub(r"\b(20\d\d)(?:\s*-\s*(20\d\d|2\d\d\d))?\b", " ", t)
    t = _NONWORD.sub(" ", t)
    return _WS.sub(" ", t).strip()


def canonical_key(title: str | None, deadline: datetime | None, url: str | None = None) -> str:
    """Dedupe identity across sources: same programme + same deadline → same row."""
    parts = [normalize_title(title)]
    if deadline:
        parts.append(deadline.strftime("%Y-%m-%d"))
    if not parts[0]:
        parts.append(normalize_url(url or "")[:120])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _safe_json(value: Any) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
        return loaded if isinstance(loaded, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def merge_sources(existing: str | None, new_source: str | None) -> str:
    """Union of comma-separated source ids (order-stable, no duplicates)."""
    out: list[str] = []
    for part in (existing or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    for part in (new_source or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return ",".join(sorted(out))


def record_hash(values: dict[str, Any]) -> str:
    """Hash of the *parsed facts*, ignoring the surrounding page.

    This is what decides "did anything I care about change?". Keying it on raw
    HTML instead would re-store every row on each cosmetic site edit, which is
    how scrapers end up crying wolf (and re-notifying) every single day.
    """
    import json as _json

    ignore = {"description", "content_hash", "raw_hash", "source", "seen_in_sources",
              "quality_flags", "parse_method", "extras", "normalized_url", "title_norm"}
    core = {k: values.get(k) for k in _TRACKED_FIELDS if k not in ignore}
    norm = _json.dumps(core, sort_keys=True, default=str)
    return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()[:32]


def text_hash(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _WS.sub(" ", re.sub(r"<[^>]+>", " ", text)).strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8", "ignore")).hexdigest()[:32] if cleaned else None


# ------------------------------------------------------------- persistence
_ENGINE = None
_SessionFactory: sessionmaker | None = None


def init_engine(db_url: str, *, echo: bool = False, force: bool = False):
    global _ENGINE, _SessionFactory
    if not force and _ENGINE is not None and _ENGINE.url.database == _engine_dbname(db_url):
        return _ENGINE
    if _ENGINE is not None and _engine_dbname(db_url) != _ENGINE.url.database:
        _ENGINE.dispose()
    engine = create_engine(db_url, echo=echo, future=True)
    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    _lightweight_migrate(engine)
    _ENGINE = engine
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def _engine_dbname(db_url: str) -> str | None:
    try:
        return str(create_engine(db_url).url.database)
    except Exception:  # pragma: no cover
        return None


def _lightweight_migrate(engine) -> None:
    """Idempotent ALTER TABLE for columns added after first release.

    Avoids the pain of 'drop the db and lose history' for a solo project
    (a full Alembic setup is documented in README for the production path).
    """
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if "scholarships" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("scholarships")}
        wanted = {c.name: c for c in Scholarship.__table__.columns}
        with engine.begin() as conn:
            for name, col in wanted.items():
                if name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                conn.execute(text(f"ALTER TABLE scholarships ADD COLUMN {name} {coltype}"))
                log_info = f"added column scholarships.{name}"
                print(f"[migrate] {log_info}")
    except Exception as exc:  # pragma: no cover
        print(f"[migrate] skipped: {exc}")


def get_session() -> Session:
    if _SessionFactory is None:
        raise RuntimeError("call init_engine() first")
    return _SessionFactory()


def _payload_from_fields(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k in _TRACKED_SET}


def upsert_scholarship(session: Session, **fields: Any) -> tuple[Scholarship, str]:
    """Insert-or-update by normalized_url.

    Returns (row, outcome) with outcome in {new, unchanged, changed, duplicate}.
    `changed` is decided by comparing tracked field values (source of truth);
    the content hash is only used to short-circuit an *expensive* re-parse.
    """
    norm = normalize_url(fields.get("url", ""))
    if not norm or not norm.startswith("https://") or not fields.get("title"):
        raise ValueError(f"missing/invalid url or title (url={fields.get('url')!r})")
    fields["normalized_url"] = norm
    fields["title_norm"] = normalize_title(fields.get("title"))
    fields.setdefault("seen_in_sources", fields.get("source", ""))
    if "raw_hash" not in fields or not fields["raw_hash"]:
        fields["raw_hash"] = record_hash(fields)
    fields.setdefault("source", "")
    if "canonical_key" not in fields:
        fields["canonical_key"] = canonical_key(fields.get("title"), fields.get("deadline"), norm)

    stmt = select(Scholarship).where(Scholarship.normalized_url == norm)
    row: Scholarship | None = session.execute(stmt).scalar_one_or_none()

    if row is None:
        dupe = session.execute(
            select(Scholarship)
            .where(Scholarship.canonical_key == fields["canonical_key"], Scholarship.normalized_url != norm)
            .order_by(Scholarship.first_seen_at.asc())
        ).scalars().first()
        if dupe is not None:
            sources = {s for s in (dupe.seen_in_sources or "").split(",") if s}
            sources.add(fields["source"])
            dupe.seen_in_sources = ",".join(sorted(s for s in sources if s))
            dupe.times_seen = (dupe.times_seen or 0) + 1
            dupe.last_seen_at = utcnow()
            import json as _json

            try:
                alt = _json.loads(dupe.extras or "{}").get("alt_urls", [])
            except Exception:
                alt = []
            if norm not in alt and norm != dupe.normalized_url:
                dupe.extras = _json.dumps({**_safe_json(dupe.extras), "alt_urls": (alt + [norm])[:8]})
            return dupe, "duplicate"

    if row is None:
        row = Scholarship(**fields)
        row.first_seen_at = utcnow()
        row.last_seen_at = utcnow()
        row.is_new = True
        row.changed = True
        row.change_type = "new"
        row.status = STATUS_ACTIVE
        row.seen_in_sources = merge_sources(row.seen_in_sources, fields.get("source"))
        session.add(row)
        return row, "new"

    payload = _payload_from_fields(**fields)
    changed = [key for key, value in payload.items() if getattr(row, key) != value and key not in NON_COMPARABLE]

    row.times_seen = (row.times_seen or 0) + 1
    row.last_seen_at = utcnow()
    merged = merge_sources(row.seen_in_sources, fields.get("source"))
    if merged != row.seen_in_sources:
        row.seen_in_sources = merged
    if not changed:
        # "changed" is a one-shot edge flag: it must not stay set and make the
        # next cycle believe the programme moved again.
        row.changed = False
        row.change_type = None
        return row, "unchanged"

    if "deadline" in changed:
        old, new = row.deadline, fields.get("deadline")
        if old and new and old != new:
            row.change_type = "deadline_moved"
        else:
            row.change_type = "deadline_added" if new else "deadline_removed"
    elif {"amount_text", "funding_type", "amount_usd"} & set(changed):
        row.change_type = "funding_changed"
    else:
        row.change_type = "content_changed"

    renotify = row.change_type in {"deadline_moved", "funding_changed"}
    if os.environ.get("SCHOLARSHIP_DEBUG_CHANGES"):
        import json as _json

        print("[diff]", row.normalized_url[-60:])
        for key in changed:
            old = getattr(row, key)
            new = fields.get(key)
            if isinstance(old, str) and len(str(old)) > 90:
                old = str(old)[:90] + f"…({len(str(old))})"
            if isinstance(new, str) and len(str(new)) > 90:
                new = str(new)[:90] + f"…({len(str(new))})"
            print(f"   {key}: {old!r} -> {new!r}")
    for key in changed:
        setattr(row, key, fields[key])
    row.changed = True
    row.is_new = row.is_new and False if not renotify else row.is_new
    if renotify:
        row.notified = False  # a moved deadline is worth a second email
    return row, "changed"


def mark_expired(session: Session, *, stale_days: int = 45) -> dict[str, int]:
    """Lifecycle sweep: expired / outdated / missing-in-action stale."""
    now = utcnow()
    counts = {"expired": 0, "outdated": 0, "stale": 0}
    q = session.query(Scholarship)
    for row in q.filter(Scholarship.status == STATUS_ACTIVE, Scholarship.deadline.isnot(None), Scholarship.deadline < now).yield_per(500):
        row.status = STATUS_EXPIRED
        counts["expired"] += 1
    for row in q.filter(Scholarship.status == STATUS_ACTIVE).yield_per(1000):
        if row.status != STATUS_ACTIVE:
            continue
        if row.deadline and (row.deadline - now).days < -1:
            continue
        if row.last_seen_at and (now - row.last_seen_at).days > stale_days:
            row.status = STATUS_STALE
            counts["stale"] += 1
        elif (now - (row.first_seen_at or now)).days > 400:
            row.status = STATUS_OUTDATED
            counts["outdated"] += 1
    session.commit()
    return counts


def active_queryset(session: Session):
    return session.query(Scholarship).filter(Scholarship.status.in_([STATUS_ACTIVE]))
