"""Scholarship tracker — command line entry point.

    python cli.py status                     what would run, with what config
    python cli.py test-source scholars4dev   one source, verbose, no DB writes
    python cli.py run                        the full nightly cycle
    python cli.py query --tier must_apply     ask the DB
    python cli.py report                      regenerate data/out/*
    python cli.py dashboard                   preview the static dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_config, summarise
from db.models import STATUS_ACTIVE, Scholarship, SourceRun, init_engine
from matching.matcher import evaluate


def _logging(verbosity: int) -> None:
    logging.basicConfig(
        level={0: logging.INFO, 1: logging.DEBUG}.get(verbosity, logging.WARNING),
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("parsers").setLevel(logging.WARNING)


def cmd_status(args) -> int:
    settings = load_config(args.config)
    info = summarise(settings)
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        return 0
    print(f"config      : {info['config']}")
    print(f"database    : {info['db']}")
    p = info["profile"]
    print(f"profile     : {p['degree']}, GPA {p['gpa']}, IELTS {p['ielts']}, home {p['home_country']}")
    print(f"fields      : {', '.join(p['fields'])}")
    print(f"countries   : {', '.join(p['preferred_countries'])}")
    print(f"llm         : {info['llm']['model']} enabled={info['llm']['enabled']} key={'yes' if info['llm']['has_key'] else 'NO'} @ {info['llm']['base_url']}")
    print(f"notify      : channels={info['notify']['channels']} threshold={info['notify']['threshold']}")
    print(f"playwright  : {'installed' if info['playwright_available'] else 'not installed (optional)'}")
    print("sources:")
    for s in info["sources"]:
        print(f"  - {s['id']:22s} kind={s['kind']:9s} priority={s['priority']} enabled={s['enabled']}")
    return 0


def cmd_test_source(args) -> int:
    settings = load_config(args.config)
    init_engine(settings.db_url)
    from core.web import Web
    from scrapers import build_scraper

    src = next((s for s in settings.enabled_sources if s.get("id") == args.source), None)
    if src is None:
        print(f"unknown source '{args.source}'. available: {', '.join(s.get('id','?') for s in settings.sources)}")
        return 2
    web = Web(
        cache_dir=Path(settings.runtime.get("cache_dir", "data/cache")),
        user_agent=settings.runtime.get("user_agent", "Mozilla/5.0"),
        min_interval=float(src.get("min_interval", settings.runtime.get("min_interval_seconds", 2.0))),
        offline=args.offline,
    )
    scraper = build_scraper(src, web, settings.profile)
    res = scraper.scrape()
    print(f"[{args.source}] status={res.status} candidates={len(res.candidates)} errors={len(res.errors)}")
    for e in res.errors[:3]:
        print("   error:", e[:200])
    for cand in res.candidates[: args.limit]:
        from core.pipeline import candidate_to_fields

        fields, diag = candidate_to_fields(cand, settings)
        ex = diag["extraction"]
        row = Scholarship(**{k: v for k, v in fields.items() if k in {c.name for c in Scholarship.__table__.columns}})
        res_match = evaluate(row, settings.profile, weights=settings.scoring.get("weights"), thresholds=settings.scoring.get("tiers"))
        got = sum(1 for f in ("country", "degree_level", "funding_type", "amount_text", "deadline", "field") if getattr(ex, f))
        print(f"\n  • {ex.title[:96]}")
        print(f"    fields found {got}/6 · score {res_match.score:.0f} · tier {res_match.tier} · conf {res_match.confidence:.0%}")
        print(f"    country={ex.country} level={ex.degree_level} funding={ex.funding_type} amount={ex.amount_usd} deadline={ex.deadline}{' (rolling)' if ex.deadline_rolling else ''}")
        if res_match.gate.reasons:
            print("    ✗ gates:", "; ".join(res_match.gate.reasons))
        if res_match.gate.warnings:
            print("    ⚠ ", "; ".join(res_match.gate.warnings[:3]))
        if args.show_text:
            print("    text:", (cand.merged_text() or "")[:400].replace("\n", " ⏎ "))
    return 0


def cmd_run(args) -> int:
    from core.pipeline import run

    settings = load_config(args.config)
    summary = run(
        settings,
        sources=args.source or None,
        notify=not args.no_notify,
        offline=args.offline,
        force=args.force,
        limit=args.limit,
        export=not args.no_export,
    )
    if args.json:
        print(json.dumps(summary.as_dict(), indent=2, default=str))
    return 0


def cmd_report(args) -> int:
    settings = load_config(args.config)
    init_engine(settings.db_url)
    from db.session import get_session
    from reporting import export

    session = get_session()
    files = export(session, Path(args.out), limit=args.limit, statuses=tuple(args.statuses.split(",")) if args.statuses else None)
    for name, path in files.items():
        print(f"{name:5s} → {path}")
    return 0


def cmd_query(args) -> int:
    settings = load_config(args.config)
    init_engine(settings.db_url)
    from db.session import get_session

    session = get_session()
    q = session.query(Scholarship)
    if not args.all:
        q = q.filter(Scholarship.status == STATUS_ACTIVE)
    if args.tier:
        q = q.filter(Scholarship.tier == args.tier)
    if args.country:
        q = q.filter(Scholarship.country.ilike(f"%{args.country}%"))
    if args.degree:
        q = q.filter(Scholarship.degree_level.ilike(f"%{args.degree}%"))
    if args.funding:
        q = q.filter(Scholarship.funding_type == args.funding)
    if args.min_score:
        q = q.filter(Scholarship.match_score >= args.min_score)
    if args.q:
        like = f"%{args.q}%"
        q = q.filter(Scholarship.title.ilike(like) | Scholarship.field.ilike(like) | Scholarship.university.ilike(like))
    if args.soon:
        q = q.filter(Scholarship.deadline.isnot(None)).order_by(Scholarship.deadline.asc())
    else:
        q = q.order_by(Scholarship.match_score.desc())
    rows = q.limit(args.limit).all()
    if args.json:
        print(json.dumps([r.as_dict() for r in rows], indent=2, default=str))
        return 0
    if not rows:
        print("no rows")
        return 0
    for r in rows:
        dl = r.deadline.strftime("%Y-%m-%d") if r.deadline else ("rolling" if r.deadline_rolling else "—")
        money = f"${r.amount_usd:,.0f}" if r.amount_usd else (r.amount_text or "")[:22] or "—"
        print(
            f"{r.match_score:5.1f} {r.tier or '-':12s} {(r.days_left if r.days_left is not None else -999):5}d "
            f"{'F' if r.funding_type=='Full' else (r.funding_type or '?')[:4]:4s} {dl:10s} {money:>10s}  {(r.title or '')[:58]}"
        )
        if args.explain:
            try:
                bd = json.loads(r.score_breakdown or "{}")
                print(f"        breakdown: {bd.get('breakdown')}  conf={bd.get('confidence')}")
                if bd.get("warnings"):
                    print(f"        warnings : {bd['warnings']}")
                if bd.get("reasons"):
                    print(f"        reasons  : {bd['reasons']}")
            except json.JSONDecodeError:
                pass
    return 0


def cmd_dashboard(args) -> int:
    settings = load_config(args.config)
    out = Path(args.out)
    if not (out / "index.html").exists() or args.rebuild:
        init_engine(settings.db_url)
        from db.session import get_session
        from reporting import export

        export(get_session(), out)
    from reporting import serve

    serve(out, port=args.port, host=getattr(args, "host", "0.0.0.0"))
    return 0


def cmd_health(args) -> int:
    settings = load_config(args.config)
    init_engine(settings.db_url)
    from db.session import get_session

    session = get_session()
    rows = session.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(400).all()
    if not rows:
        print("no runs recorded yet")
        return 0
    runs: dict[str, list[SourceRun]] = {}
    for r in rows:
        runs.setdefault(r.run_id, []).append(r)
    latest_id = sorted(runs)[-1]
    print(f"latest run: {latest_id}")
    for r in runs[latest_id]:
        print(f"  {r.source:20s} {r.status:8s} found={r.items_found:<4} new={r.items_new:<3} changed={r.items_changed:<3} "
              f"detail={r.detail_fetches:<3} cache={r.cache_hits:<3} llm={r.llm_calls:<2} ${r.llm_cost_usd:.4f} {r.duration_ms}ms")
        if r.error:
            print(f"      ↳ {r.error[:200]}")
    if args.since:
        pat = re.compile(args.since)
        stale = [r.source for r in rows if r.status != "ok" and pat.search(r.source)]
        if stale:
            print("non-ok sources matching filter:", ", ".join(sorted(set(stale))))
    return 0


def cmd_notify_preview(args) -> int:
    settings = load_config(args.config)
    init_engine(settings.db_url)
    from core.pipeline import _notification_pool, _score_all
    from db.session import get_session
    from notifiers import build_digest

    session = get_session()
    scored = _score_all(session, settings)
    pool = _notification_pool(session, scored, force=True, settings=settings)
    digest = build_digest(pool, settings.profile, settings=settings, stats={"sources_ok": 0, "new": len(pool), "changed": 0, "llm_calls": 0, "llm_cost_usd": 0})
    if digest is None:
        print("nothing to send (no rows above threshold)")
        return 0
    if args.mark_sent:
        for row in digest.rows:
            row.scholarship.notified = True
            row.scholarship.is_new = False
        session.commit()
    if args.html:
        out = Path(settings.runtime.get("out_dir", "data/out"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "digest.html").write_text(digest.html(), encoding="utf-8")
        print(f"wrote {out/'digest.html'}")
    if args.plain:
        print(digest.plain())
    else:
        print(digest.markdown())
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scholarship-bot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=None, help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="show resolved config")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("test-source", help="run one scraper and show what was extracted (no writes)")
    sp.add_argument("source")
    sp.add_argument("--limit", type=int, default=4)
    sp.add_argument("--offline", action="store_true")
    sp.add_argument("--show-text", action="store_true")
    sp.set_defaults(fn=cmd_test_source)

    sp = sub.add_parser("run", help="full cycle: scrape → parse → score → notify → report")
    sp.add_argument("--source", action="append", help="restrict to a source id (repeatable)")
    sp.add_argument("--limit", type=int, default=None, help="max candidates per source")
    sp.add_argument("--offline", action="store_true", help="never hit the network (cache only)")
    sp.add_argument("--force", action="store_true", help="ignore dedupe/quiet-hours and send")
    sp.add_argument("--no-notify", action="store_true")
    sp.add_argument("--no-export", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("query", help="query the database")
    sp.add_argument("-q", "--q")
    sp.add_argument("--tier")
    sp.add_argument("--country")
    sp.add_argument("--degree")
    sp.add_argument("--funding")
    sp.add_argument("--min-score", type=float, default=0)
    sp.add_argument("--soon", action="store_true", help="sort by deadline")
    sp.add_argument("--all", action="store_true", help="include expired/stale rows")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--explain", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_query)

    sp = sub.add_parser("report", help="write dashboard/CSV/markdown exports")
    sp.add_argument("--out", default="data/out")
    sp.add_argument("--limit", type=int, default=400)
    sp.add_argument("--statuses", default="active")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("dashboard", help="serve the static dashboard")
    sp.add_argument("--out", default="data/out")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--host", default="0.0.0.0", help="0.0.0.0 works behind a proxy/tunnel")
    sp.add_argument("--rebuild", action="store_true")
    sp.set_defaults(fn=cmd_dashboard)

    sp = sub.add_parser("health", help="per-source reliability from the last run")
    sp.add_argument("--since", help="regex filter on source id")
    sp.set_defaults(fn=cmd_health)

    sp = sub.add_parser("notify-preview", help="render the digest that would be sent")
    sp.add_argument("--plain", action="store_true")
    sp.add_argument("--html", action="store_true")
    sp.add_argument("--mark-sent", action="store_true")
    sp.set_defaults(fn=cmd_notify_preview)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _logging(args.verbose)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
