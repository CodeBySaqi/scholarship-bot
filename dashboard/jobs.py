"""One-at-a-time background jobs with a scrollback log.

The dashboard's "Run now" button must not block the HTTP thread (a full scrape
is minutes), and it must not let two scrapes overlap into the same SQLite file —
the pipeline is polite about rate limits and caches, and two runs would fight
over both. So: one job at a time, a bounded ring of log lines, and every job
calls the same functions `cli.py` calls.
"""

from __future__ import annotations

import io
import threading
import traceback
from collections import deque
from contextlib import redirect_stdout
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Callable
from typing import Any

LOG_LINES = 400


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class Job:
    id: str
    kind: str
    label: str
    fn: Callable[[Callable[[str], None], dict[str, Any]], Any] = None  # type: ignore[assignment]
    status: str = "queued"          # queued | running | ok | error
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    result: Any = None
    error: str | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    done: threading.Event = field(default_factory=threading.Event)

    # ---- for the UI ----
    def log(self, message: str) -> None:
        """Append to the ring buffer, newest at the end. Silent after `LOG_LINES`."""
        for line in str(message).splitlines() or [""]:
            self.lines.append(line[:500])

    def tail(self, after: int = 0) -> dict[str, Any]:
        start = max(0, min(int(after or 0), len(self.lines)))
        chunk = list(self.lines)[start:]
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lines": chunk,
            "next": len(self.lines),
            "total": len(self.lines),
            "result": self.result,
            "error": self.error,
        }

    def wait(self, timeout: float | None = None) -> bool:
        """Mostly for tests: block until the job finishes."""
        return self.done.wait(timeout)


class Busy(RuntimeError):
    """A job is already running — the UI turns this into a disabled button."""


class Runner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._seq = 0

    def submit(self, kind: str, label: str, fn: Callable[[Job], None]) -> Job:
        with self._lock:
            if self._current is not None and self._current.status == "running":
                raise Busy(f"{self._current.label} is still running (started {self._current.started_at})")
            self._seq += 1
            job = Job(id=f"{kind}-{self._seq}", kind=kind, label=label, fn=fn)  # type: ignore[arg-type]
            self._jobs[job.id] = job
            self._order.append(job.id)
            for stale in self._order[:-12]:
                if self._jobs[stale].status != "running":
                    self._jobs.pop(stale, None)
            self._order = self._order[-12:]
            self._current = job
        thread = threading.Thread(target=self._drive, args=(job,), name=job.id, daemon=True)
        thread.start()
        return job

    def _drive(self, job: Job) -> None:
        job.status = "running"
        job.log(f"{job.label} started")
        try:
            job.result = job.fn(job)
            job.status = "ok"
        except Exception as exc:  # noqa: BLE001 - the log is the report
            job.status = "error"
            job.error = f"{exc.__class__.__name__}: {exc}"
            for line in traceback.format_exc().strip().splitlines()[-14:]:
                job.log(f"    {line}")
        finally:
            job.finished_at = _now()
            job.log(f"finished: {job.status}" + (f" — {job.error}" if job.error else ""))
            job.done.set()
            with self._lock:
                if self._current is job:
                    self._current = None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    @property
    def current(self) -> Job | None:
        return self._current

    def recent(self) -> list[dict[str, Any]]:
        """Everything about each job, newest first. `tail(0)` = all lines: `tail(n)`
        *skips* n lines, which is for polling, not for a first read."""
        return [self._jobs[i].tail(0) for i in reversed(self._order) if i in self._jobs]


def _log_method(job: Job) -> Callable[[str], None]:
    """A `log("text")` callable bound to one job, for engine code to write into."""
    return job.log


def job_stream(job: Job, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run engine code with its stdout captured into the job log.

    The pipeline prints its progress; a web page should not need a
    `logging` handler to show it, so the prints are teed into the log buffer.
    """
    log = _log_method(job)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            result = fn(*args, **kwargs)
    finally:
        for line in buffer.getvalue().splitlines():
            log(line)
    return result


# ----------------------------------------------------------------- the jobs
def start_run(runner: Runner, settings_provider: Callable[[], Any], *, offline: bool = False,
              force: bool = False, notify: bool = True, export: bool = True,
              sources: list[str] | None = None, limit: int | None = None) -> Job:
    from core.pipeline import run

    label = "Offline rebuild" if offline else "Scrape now"
    if force:
        label += " (force)"

    def body(job: Job) -> dict[str, Any]:
        settings = settings_provider()
        log = _log_method(job)
        log(f"config: {getattr(settings, 'path', '?')} · db: {settings.db_url}")
        chosen = sources or [s.get("id", "?") for s in settings.enabled_sources]
        log(f"sources: {', '.join(chosen) if chosen else '(none enabled)'}")
        summary = job_stream(job, run, settings, sources=sources, notify=notify, offline=offline,
                             force=force, limit=limit, export=export)
        data = summary.as_dict() if hasattr(summary, "as_dict") else dict(summary.__dict__)
        log(f"candidates={data.get('candidates')} new={data.get('new')} changed={data.get('changed')} "
            f"dups={data.get('duplicates')} rejected={data.get('rejected')}")
        log(f"llm: calls={data.get('llm_calls')} tokens={data.get('llm_tokens')} cost=${data.get('llm_cost_usd') or 0:.4f}")
        ok, fail = data.get("sources_ok") or [], data.get("sources_failed") or []
        log(f"sources ok={len(ok)} fail={len(fail)}" + (f" — {', '.join(fail)}" if fail else ""))
        if data.get("notified"):
            log(f"notified: {data['notified']}")
        if not notify:
            log("notifications: skipped (--no-notify)")
        return data

    return runner.submit("run", label, body)


def start_test_source(runner: Runner, settings_provider: Callable[[], Any], source_id: str,
                      *, limit: int = 3, offline: bool = False) -> Job:
    def body(job: Job) -> dict[str, Any]:
        from core.config import load_config  # noqa: F401  (kept for parity with cli)

        settings = settings_provider()
        log = _log_method(job)
        src = next((s for s in settings.sources if s.get("id") == source_id), None)
        if src is None:
            raise KeyError(f"unknown source {source_id!r}")
        if not src.get("enabled", True):
            log(f"note: '{source_id}' is disabled — testing it anyway (that is how you check before switching it on)")
        from core.web import Web
        from core.pipeline import candidate_to_fields
        from matching.matcher import evaluate
        from db.models import Scholarship
        from scrapers import build_scraper

        web = Web(
            cache_dir=Path(src.get("cache_dir") or settings.runtime.get("cache_dir", "data/cache")),
            user_agent=settings.runtime.get("user_agent", "Mozilla/5.0"),
            min_interval=float(src.get("min_interval", settings.runtime.get("min_interval_seconds", 2.0))),
            offline=offline,
        )
        scraper = build_scraper(src, web, settings.profile)
        res = job_stream(job, scraper.scrape)
        log(f"status={res.status} candidates={len(res.candidates)} errors={len(res.errors)}")
        for err in res.errors[:3]:
            log(f"error: {str(err)[:200]}")
        preview = []
        for cand in res.candidates[:limit]:
            fields, diag = candidate_to_fields(cand, settings)
            ex = diag["extraction"]
            row = Scholarship(**{k: v for k, v in fields.items() if k in {c.name for c in Scholarship.__table__.columns}})
            match = evaluate(row, settings.profile, weights=settings.scoring.get("weights"),
                             thresholds=settings.scoring.get("tiers"))
            log(f"• {str(ex.title)[:90]} — score {match.score:.0f} · {match.tier} · deadline {ex.deadline or 'rolling'}")
            preview.append({"title": ex.title, "score": match.score, "tier": match.tier,
                            "country": ex.country, "deadline": str(ex.deadline) if ex.deadline else None,
                            "url": getattr(ex, "url", "")})
        return {"source": source_id, "status": res.status, "found": len(res.candidates),
                "errors": [str(e)[:200] for e in res.errors[:5]], "preview": preview}

    return runner.submit("test-source", f"Test {source_id}", body)


def start_notify(runner: Runner, settings_provider: Callable[[], Any], *, force: bool = True,
                 chat_id: str | None = None, channel: str | None = None) -> Job:
    def body(job: Job) -> dict[str, Any]:
        from core.pipeline import _notification_pool, _score_all  # same pool the run uses
        from notifiers import build_digest, deliver
        from db.models import init_engine, get_session

        settings = settings_provider()
        log = _log_method(job)
        if channel:
            settings.notify["channels"] = [channel]
            log(f"channel override: {channel}")
        if chat_id:
            settings.notify["telegram_chat_id"] = chat_id
        init_engine(settings.db_url)
        session = get_session()
        rows = job_stream(job, _score_all, session, settings)
        pool = _notification_pool(session, rows, force=force, settings=settings)
        log(f"digest: {len(pool)} row(s) above threshold "
            f"{settings.notify.get('min_score')} (channels: {', '.join(settings.notify.get('channels') or [])})")
        if not pool:
            log("nothing to send — no candidates passed the gates. Run a scrape first.")
            return {"sent": 0, "pool": 0, "channels": settings.notify.get("channels") or []}
        digest = build_digest(pool, settings.profile, settings=settings)
        result = deliver(digest, settings, session, force=force)
        # NB: not `for channel in …` — that would shadow the `channel` argument
        # above and make `if channel:` read an unbound local.
        for name, detail in (result.get("sent") or {}).items():
            log(f"✓ {name}: {detail}")
        for name, detail in (result.get("skipped") or {}).items():
            log(f"— {name}: {detail}")
        if not result.get("sent"):
            log("nothing left the box: no channel succeeded (dry_run? quiet hours? missing token/chat id?)")
        return {"result": result, "pool": len(pool), "preview": digest.plain()[:1500],
                "channels": settings.notify.get("channels") or []}

    return runner.submit("notify", "Send digest now", body)


def start_health(runner: Runner, settings_provider: Callable[[], Any]) -> Job:
    def body(job: Job) -> dict[str, Any]:
        from db.models import SourceRun, init_engine, get_session

        settings = settings_provider()
        log = _log_method(job)
        init_engine(settings.db_url)
        session = get_session()
        runs: dict[str, list[Any]] = {}
        for row in session.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(400).all():
            runs.setdefault(row.run_id, []).append(row)
        if not runs:
            log("no runs recorded yet — press Run now")
            return {"runs": 0, "sources": []}
        latest = max(runs)
        log(f"latest run: {latest}")
        out = []
        for row in runs[latest]:
            log(f"{row.source:20s} {row.status:8s} found={row.items_found:<4} new={row.items_new:<3} "
                f"changed={row.items_changed:<3} detail={row.detail_fetches:<3} llm={row.llm_calls:<2} "
                f"${row.llm_cost_usd:.4f} {row.duration_ms}ms")
            if row.error:
                log(f"    ↳ {row.error[:200]}")
            out.append({"source": row.source, "status": row.status, "found": row.items_found,
                        "new": row.items_new, "changed": row.items_changed, "detail_fetches": row.detail_fetches,
                        "cache_hits": row.cache_hits, "llm_calls": row.llm_calls,
                        "cost_usd": row.llm_cost_usd, "duration_ms": row.duration_ms,
                        "error": (row.error or "")[:300], "started_at": row.started_at.strftime("%Y-%m-%d %H:%M")})
        bad = [r["source"] for r in out if r["status"] != "ok"]
        log(f"{len(out) - len(bad)}/{len(out)} sources healthy" + (f" · needs attention: {', '.join(bad)}" if bad else ""))
        return {"runs": len(runs), "run_id": latest, "sources": out, "unhealthy": bad}

    return runner.submit("health", "Check source health", body)
