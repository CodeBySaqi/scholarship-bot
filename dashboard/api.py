"""The dashboard's HTTP surface: routing and JSON, no HTML, no sockets.

`dispatch(ctx, method, path, query, body)` is pure Python — it takes parsed
request parts and returns `(status, headers, body)`. `server.py` is a thin
adapter over it, so every route here is testable without opening a port (and
without a web framework, which this project deliberately doesn't depend on).
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from . import query as Q
from . import settings_io as S
from . import telegram as TG
from .jobs import Runner, start_health, start_notify, start_run, start_test_source

HEADERS_JSON = {"Content-Type": "application/json; charset=utf-8"}
EXPORT_FILES = ("index.html", "data.json", "scholarships.csv", "summary.md", "last_digest.md")
ENGINE = "1.0"


@dataclass
class Ctx:
    """Everything a request can touch. Built once, in `server.serve`."""

    root: Path
    config_path: Path
    env_path: Path
    runner: Runner = field(default_factory=Runner)
    allow_writes: bool = True
    _settings: Any = None
    telegram_state: dict[str, Any] = field(default_factory=dict)

    def settings(self):
        if self._settings is None:
            self._settings = S.reload_settings(self.config_path)
        return self._settings

    def refresh(self):
        self._settings = S.reload_settings(self.config_path)
        return self._settings

    def session(self):
        from db.models import get_session, init_engine

        settings = self.settings()
        init_engine(settings.db_url)
        return get_session()


Response = tuple[int, dict[str, str], bytes]


def json_response(payload: Any, status: int = 200) -> Response:
    return status, HEADERS_JSON, json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")


def text_response(text: str, status: int = 400, content_type: str = "text/plain; charset=utf-8") -> Response:
    return status, {"Content-Type": content_type}, text.encode("utf-8")


def _params(query: dict[str, list[str]]) -> dict[str, str]:
    return {k: v[0] for k, v in query.items() if v}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _flag(params: dict[str, str], key: str) -> bool:
    return str(params.get(key, "")).lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------- routes
def status(ctx: Ctx) -> Response:
    settings = ctx.settings()
    from core.config import summarise
    from db.models import SourceRun

    info = summarise(settings)
    session = ctx.session()
    try:
        counts = Q.archive_stats(session)
        runs = session.query(SourceRun).order_by(SourceRun.id.desc()).limit(120).all()
        last_by_source: dict[str, dict[str, Any]] = {}
        for row in runs:
            last_by_source.setdefault(row.source, {
                "status": row.status, "found": row.items_found, "new": row.items_new,
                "changed": row.items_changed, "detail_fetches": row.detail_fetches,
                "llm_calls": row.llm_calls, "cost_usd": row.llm_cost_usd,
                "duration_ms": row.duration_ms, "started_at": row.started_at.strftime("%Y-%m-%d %H:%M"),
                "error": (row.error or "")[:300], "consecutive_failures": row.consecutive_failures,
            })
    finally:
        session.close()

    job = ctx.runner.current
    active = job.tail(0) if job else None
    sources = []
    for src in settings.sources:
        sid = src.get("id", "?")
        sources.append({
            "id": sid, "kind": src.get("kind"), "enabled": bool(src.get("enabled", True)),
            "priority": src.get("priority", 50), "url": src.get("url", ""),
            "pages": src.get("pages"), "limit": src.get("limit"),
            "fetch_detail": bool(src.get("fetch_detail", False)), "note": src.get("note", ""),
            "last": last_by_source.get(sid),
        })
    notify = settings.notify
    return json_response({
        "engine": ENGINE,
        "config": str(ctx.config_path),
        "db": info["db"],
        "out_dir": str(Path(settings.runtime.get("out_dir", "data/out"))),
        "writable": ctx.allow_writes,
        "profile": info["profile"],
        "llm": info["llm"],
        "notify": {
            "channels": notify.get("channels") or [], "min_score": notify.get("min_score"),
            "dry_run": bool(notify.get("dry_run", False)), "max_rows": notify.get("max_rows"),
            "quiet_hours": notify.get("quiet_hours"), "always_summary": bool(notify.get("always_summary")),
            "email_ready": bool(notify.get("smtp_host") and notify.get("to_email")),
            "webhook_ready": bool(notify.get("webhook_url")),
        },
        "telegram": ctx.telegram_state or {"configured": bool(notify.get("telegram_bot_token")),
                                           "verified": False},
        "counts": counts,
        "sources": sources,
        "secrets": S.secret_status(ctx.env_path, settings),
        "job": active,
        "playwright": info.get("playwright_available", False),
        "gates": {
            "min_award_usd": settings.profile.min_award_usd,
            "needs_full_funding": settings.profile.needs_full_funding,
            "max_application_fee_usd": settings.profile.max_application_fee_usd,
            "apply_window_days": settings.profile.apply_window_days,
        },
    })


def settings_view(ctx: Ctx) -> Response:
    settings = ctx.settings()
    return json_response({
        "groups": S.snapshot(settings),
        "specs": S.editable_sections(),
        "sources": [dict(s) for s in settings.sources],
        "source_fields": S.SOURCE_FIELDS,
        "secrets": S.secret_status(ctx.env_path, settings),
        "secret_specs": S.SECRETS,
        "config_path": str(ctx.config_path),
        "env_path": str(ctx.env_path),
        "env_exists": ctx.env_path.exists(),
    })


def save_settings(ctx: Ctx, body: dict[str, Any]) -> Response:
    if not ctx.allow_writes:
        return json_response({"error": "this server was started read-only (--read-only)"}, 403)
    patches: list[tuple[list[str], Any]] = []
    for group, values in (body.get("groups") or {}).items():
        if not isinstance(values, dict):
            continue
        try:
            patches.extend(S.build_patches(group, values))
        except (KeyError, ValueError, TypeError) as exc:
            return json_response({"error": f"{group}: {exc}"}, 422)
    source_edits = []
    for edit in body.get("source_edits") or []:
        try:
            spec = next(s for s in S.SOURCE_FIELDS if s["key"] == edit.get("key"))
        except StopIteration:
            return json_response({"error": f"source field {edit.get('key')!r} is not editable"}, 422)
        try:
            value = S.coerce(spec, edit.get("value"))
        except (ValueError, TypeError) as exc:
            return json_response({"error": f"source {edit.get('id')}.{edit.get('key')}: {exc}"}, 422)
        source_edits.append({"id": edit.get("id"), "key": edit["key"], "value": value})
    secrets = {str(k).upper(): v for k, v in (body.get("secrets") or {}).items()}
    for key in secrets:
        if not re.fullmatch(r"[A-Z0-9_]{2,60}", key):
            return json_response({"error": f"not a valid env key: {key}"}, 422)
    try:
        result = S.write_settings(
            ctx.config_path, config_patches=patches, source_edits=source_edits,
            source_add=body.get("source_add"), source_delete=body.get("source_delete"),
            env_path=ctx.env_path, env_values=secrets,
        )
    except S.ConfigRejected as exc:
        return json_response({"error": str(exc), "rolled_back": True}, 422)
    except (ValueError, KeyError) as exc:
        return json_response({"error": str(exc), "rolled_back": True}, 422)
    ctx.refresh()
    result["groups"] = S.snapshot(ctx.settings())
    result["sources"] = [dict(s) for s in ctx.settings().sources]
    return json_response(result)


def scholarship_list(ctx: Ctx, params: dict[str, str]) -> Response:
    session = ctx.session()
    try:
        days_max = _int(params.get("days"), 45) if params.get("status") == "expiring" else None
        payload = Q.list_scholarships(
            session,
            search=params.get("q", ""),
            country=params.get("country", ""),
            degree=params.get("degree", ""),
            tier=params.get("tier", ""),
            source=params.get("source", ""),
            funding=params.get("funding", ""),
            status=params.get("status", "active"),
            gate=params.get("gate", ""),
            days_max=days_max,
            starred_only=_flag(params, "starred"),
            include_hidden=_flag(params, "hidden"),
            sort=params.get("sort", "score"),
            limit=_int(params.get("limit"), 200),
            offset=_int(params.get("offset"), 0),
        )
    finally:
        session.close()
    return json_response(payload)


def scholarship_detail(ctx: Ctx, key: str) -> Response:
    session = ctx.session()
    try:
        row = Q.get_scholarship(session, key)
    finally:
        session.close()
    if row is None:
        return json_response({"error": f"no row matching {key!r}"}, 404)
    return json_response(row)


def scholarship_action(ctx: Ctx, key: str, body: dict[str, Any]) -> Response:
    if not ctx.allow_writes:
        return json_response({"error": "read-only server"}, 403)
    session = ctx.session()
    try:
        try:
            result = Q.set_flags(session, key,
                                 starred=body.get("star") if "star" in body else None,
                                 hidden=body.get("hide") if "hide" in body else None,
                                 notes=body.get("notes") if "notes" in body else None)
        except (KeyError, ValueError) as exc:
            return json_response({"error": str(exc)}, 404)
    finally:
        session.close()
    return json_response(result)


def jobs_view(ctx: Ctx, params: dict[str, str]) -> Response:
    return json_response({"recent": ctx.runner.recent(),
                          "running": ctx.runner.current.tail(0) if ctx.runner.current else None})


def job_view(ctx: Ctx, job_id: str, params: dict[str, str]) -> Response:
    job = ctx.runner.get(job_id)
    if job is None:
        return json_response({"error": f"no job {job_id!r} (only the last 12 are kept)"}, 404)
    return json_response(job.tail(_int(params.get("after"), 0)))


def start_job(ctx: Ctx, body: dict[str, Any]) -> Response:
    """Queue one of the four actions the page offers, and return its id at once.

    Everything here is a job rather than a direct call because each one can take
    minutes; the page polls /api/jobs/{id} for the log instead of holding a socket.
    """
    if not ctx.allow_writes:
        return json_response({"error": "read-only server"}, 403)
    from .jobs import Busy

    kind = body.get("kind", "run")
    settings = ctx.settings()
    try:
        if kind == "run":
            job = start_run(ctx.runner, ctx.refresh,
                            offline=bool(body.get("offline")), force=bool(body.get("force")),
                            notify=not body.get("no_notify"), export=not body.get("no_export"),
                            sources=body.get("sources") or None,
                            limit=_int(body.get("limit"), 0) or None)
        elif kind == "test-source":
            source_id = str(body.get("source") or "").strip()
            known = [s.get("id") for s in settings.sources]
            if source_id not in known:
                return json_response({"error": f"unknown source {source_id!r}; known: {', '.join(map(str, known))}"}, 422)
            job = start_test_source(ctx.runner, ctx.settings, source_id,
                                    limit=_int(body.get("limit"), 3), offline=bool(body.get("offline")))
        elif kind == "notify":
            job = start_notify(ctx.runner, ctx.settings, force=bool(body.get("force", True)),
                              chat_id=body.get("chat_id"), channel=body.get("channel"))
        elif kind == "health":
            job = start_health(ctx.runner, ctx.settings)
        else:
            return json_response({"error": f"unknown job kind {kind!r}; try run, test-source, notify, health"}, 422)
    except Busy as exc:
        return json_response({"error": str(exc), "busy": True}, 409)
    return json_response(job.tail(0), 202)


def telegram_view(ctx: Ctx) -> Response:
    notify = ctx.settings().notify
    return json_response({
        "configured": bool(notify.get("telegram_bot_token")),
        "chat_id": notify.get("telegram_chat_id") or None,
        "channel_on": "telegram" in (notify.get("channels") or []),
        "dry_run": bool(notify.get("dry_run", False)),
        **(ctx.telegram_state or {}),
    })


def telegram_action(ctx: Ctx, body: dict[str, Any]) -> Response:
    """verify | pair | bind | test | digest | unbind — connect a bot without editing files by hand."""
    from .jobs import Busy

    settings = ctx.settings()
    action = body.get("action", "verify")
    raw_token = str(body.get("token") or "").strip()
    if raw_token:
        token = TG.token_from_url(raw_token)
        if not token:
            return json_response({"error": "That doesn't look like a bot token. @BotFather issues "
                                           "`digits:letters`; paste the whole line."}, 422)
    else:
        token = str(settings.notify.get("telegram_bot_token") or "")
    if not token:
        return json_response({"error": "No bot token yet — paste the one @BotFather gave you, then Verify."}, 422)

    wanted_chat = TG.chat_id_from_link(str(body.get("chat_id") or "")) or str(
        settings.notify.get("telegram_chat_id") or "").strip()

    def save(**values: Any) -> None:
        if not ctx.allow_writes or not values:
            return
        S.write_settings(ctx.config_path, env_path=ctx.env_path, env_values=values)
        ctx.refresh()

    try:
        if action == "verify":
            me = TG.get_me(settings, token=token)
            ctx.telegram_state = {"verified": True, "bot": me, "chat_id": wanted_chat or None, "checked_at": _now()}
            save(**({"TELEGRAM_BOT_TOKEN": token} if raw_token else {}))
            return json_response({"ok": True, "bot": me, "chat_id": wanted_chat or None,
                                  "note": "Token accepted. Next: pair — press START in Telegram, then Pair."})
        if action == "pair":
            if raw_token:
                save(TELEGRAM_BOT_TOKEN=token)
                settings = ctx.settings()
            result = TG.pair(settings, token=token)
            if not result.get("ok"):
                return json_response({"ok": False, "error": result.get("error")}, 502)
            ctx.telegram_state.update({"verified": True, "chat_id": result.get("chat_id"),
                                       "chats": result.get("chats") or [], "checked_at": _now()})
            if result.get("chat_id"):
                result["note"] = ("Click Use this chat to save it. "
                                  f"({len(result['chats'])} chat(s) have messaged this bot.)")
            return json_response(result)
        if action == "bind":
            chat = TG.chat_id_from_link(str(body.get("chat_id") or "")) or wanted_chat
            if not chat:
                return json_response({"error": "No chat to bind. Press START in the bot's Telegram chat, "
                                               "click Pair, then pick it."}, 422)
            if not ctx.allow_writes:
                return json_response({"error": "read-only server"}, 403)
            save(TELEGRAM_BOT_TOKEN=token, TELEGRAM_CHAT_ID=chat)
            if body.get("also_channel") and "telegram" not in (ctx.settings().notify.get("channels") or []):
                # pairing is useless if the digest never routes to the channel, and
                # asking the user to tick a checkbox three tabs away invites exactly that
                S.write_settings(ctx.config_path,
                                 config_patches=[(["notify", "channels"],
                                                  list(ctx.settings().notify.get("channels") or []) + ["telegram"])])
                ctx.refresh()
            ctx.telegram_state.update({"verified": True, "chat_id": chat, "bound_at": _now()})
            return json_response({"ok": True, "chat_id": chat,
                                  "note": "Saved to .env — the next run notifies this chat."})
        if action == "test":
            text = str(body.get("text") or "").strip() or (
                "Scholar Radar: connection test.\nIf you can read this, notifications will arrive here.")
            res = TG.send(ctx.settings(), wanted_chat or None, text, token=token)
            return json_response({"ok": True, **res,
                                  "note": f"Sent {res['messages']} message(s) to {res['chat_id']}."})
        if action == "digest":
            try:
                job = start_notify(ctx.runner, ctx.settings, force=True,
                                   chat_id=wanted_chat or None, channel="telegram")
            except Busy as exc:
                return json_response({"error": str(exc), "busy": True}, 409)
            return json_response({"ok": True, "job": job.id, "label": job.label}, 202)
        if action == "unbind":
            if not ctx.allow_writes:
                return json_response({"error": "read-only server"}, 403)
            save(TELEGRAM_CHAT_ID="")
            ctx.telegram_state.update({"chat_id": None})
            return json_response({"ok": True, "note": "Chat id cleared from .env. The token is kept."})
        if action == "forget":
            if not ctx.allow_writes:
                return json_response({"error": "read-only server"}, 403)
            save(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
            ctx.telegram_state = {"verified": False, "chat_id": None}
            return json_response({"ok": True, "note": "Token and chat id removed from .env."})
        return json_response({"error": f"unknown telegram action {action!r}"}, 422)
    except TG.TelegramError as exc:
        # Telegram's own description is the useful part ("Unauthorized", "chat not found")
        return json_response({"ok": False, "error": str(exc), "action": action}, 502)
    except S.ConfigRejected as exc:
        return json_response({"ok": False, "error": f"saved nothing: {exc}"}, 422)
    except Exception as exc:  # noqa: BLE001
        return json_response({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, 500)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def export_listing(ctx: Ctx) -> Response:
    """What `reporting.export` has produced, with an honest "not yet generated".

    The page links these rather than rendering its own table, so the CSV the user
    downloads is byte-identical to the one the GitHub Pages deploy publishes.
    """
    out_dir = Path(ctx.settings().runtime.get("out_dir", "data/out"))
    files = []
    for name in EXPORT_FILES:
        path = out_dir / name
        exists = path.exists()
        files.append({
            "name": name, "exists": exists,
            "bytes": path.stat().st_size if exists else 0,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if exists else None,
        })
    return json_response({"dir": str(out_dir), "files": files, "names": list(EXPORT_FILES)})


def export_file(ctx: Ctx, name: str) -> Response:
    if name not in EXPORT_FILES:
        return json_response({"error": f"only these can be downloaded: {', '.join(EXPORT_FILES)}"}, 404)
    path = Path(ctx.settings().runtime.get("out_dir", "data/out")) / name
    if not path.exists():
        return text_response("not generated yet — run a scrape, exports are written at the end of a run.", 404)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    headers = {"Content-Type": f"{ctype}; charset=utf-8"}
    if not name.endswith((".html", ".md", ".json", ".csv")):
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return 200, headers, path.read_bytes()


def source_help(ctx: Ctx) -> Response:
    """Which scraper kinds exist and which keys a source may set — read from the
    registry, so the page cannot advertise a kind the engine will reject."""
    from scrapers import SCRAPERS

    kinds = {}
    for kind, cls in SCRAPERS.items():
        doc = (cls.__doc__ or "").strip().splitlines()
        kinds[kind] = {"label": doc[0] if doc else kind,
                       "required": sorted(getattr(cls, "required_keys", ()) or ())}
    return json_response({"kinds": kinds, "fields": S.SOURCE_FIELDS,
                          "template": {"id": "my_source", "kind": "listing", "url": "https://…",
                                        "enabled": True, "priority": 40, "pages": 1,
                                        "item": "article.post", "title_selector": "h2 a",
                                        "link_selector": "a", "fetch_detail": True,
                                        "max_detail_fetches": 8, "limit": 25,
                                        "note": "added from the dashboard"}})


# ------------------------------------------------------------------ dispatcher
ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Response], str]] = [
    ("GET", re.compile(r"^/api/status$"), lambda ctx, p, q, b: status(ctx), "status"),
    ("GET", re.compile(r"^/api/settings$"), lambda ctx, p, q, b: settings_view(ctx), "settings"),
    ("POST", re.compile(r"^/api/settings$"), lambda ctx, p, q, b: save_settings(ctx, b), "save settings"),
    ("GET", re.compile(r"^/api/scholarships$"), lambda ctx, p, q, b: scholarship_list(ctx, q), "list"),
    ("GET", re.compile(r"^/api/sources/help$"), lambda ctx, p, q, b: source_help(ctx), "source kinds"),
    ("GET", re.compile(r"^/api/raw-config$"), lambda ctx, p, q, b: json_response(
        {"path": str(ctx.config_path), "text": ctx.config_path.read_text(encoding="utf-8")}), "raw config"),
    ("GET", re.compile(r"^/api/exports$"), lambda ctx, p, q, b: export_listing(ctx), "exports"),
    ("GET", re.compile(r"^/api/export/(?P<name>[^/]+)$"), lambda ctx, p, q, b: export_file(ctx, p["name"]), "export"),
    ("GET", re.compile(r"^/api/telegram$"), lambda ctx, p, q, b: telegram_view(ctx), "telegram"),
    ("POST", re.compile(r"^/api/telegram$"), lambda ctx, p, q, b: telegram_action(ctx, b), "telegram"),
    ("GET", re.compile(r"^/api/jobs$"), lambda ctx, p, q, b: jobs_view(ctx, q), "jobs"),
    ("POST", re.compile(r"^/api/jobs$"), lambda ctx, p, q, b: start_job(ctx, b), "start job"),
    ("GET", re.compile(r"^/api/jobs/(?P<id>[^/]+)$"), lambda ctx, p, q, b: job_view(ctx, p["id"], q), "job"),
    ("GET", re.compile(r"^/api/scholarships/(?P<key>[^/]+)$"), lambda ctx, p, q, b: scholarship_detail(ctx, p["key"]), "detail"),
    ("POST", re.compile(r"^/api/scholarships/(?P<key>[^/]+)$"), lambda ctx, p, q, b: scholarship_action(ctx, p["key"], b), "flag"),
]


def dispatch(ctx: Ctx, method: str, path: str, query: str = "", body: bytes | str | None = None) -> Response:
    method = (method or "GET").upper()
    params = _params(parse_qs(query or "", keep_blank_values=True))
    payload: dict[str, Any] = {}
    if body:
        raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return json_response({"error": "body is not JSON"}, 400)
        if not isinstance(parsed, dict):
            return json_response({"error": "body must be a JSON object"}, 400)
        payload = parsed
    try:
        for verb, pattern, handler, _label in ROUTES:
            if verb != method:
                continue
            match = pattern.match(path)
            if match:
                return handler(ctx, match.groupdict(), params, payload)
        if path.startswith("/api/"):
            verbs = {v for v, pat, _h, _l in ROUTES if pat.match(path)}
            if verbs:
                return json_response({"error": f"{method} not allowed here; try {', '.join(sorted(verbs))}"}, 405)
            return json_response({"error": f"no route for {method} {path}"}, 404)
        if method not in {"GET", "HEAD"}:
            return json_response({"error": "the page is served by GET; use /api/* for writes"}, 405)
        return json_response({"error": f"no route for {path}"}, 404)
    except Exception as exc:  # noqa: BLE001 - a 500 with the reason beats a hung socket
        return json_response({"error": f"{exc.__class__.__name__}: {exc}", "path": path}, 500)
