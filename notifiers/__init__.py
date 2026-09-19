"""Notification layer: one digest, rendered per channel.

Improvements over the blueprint:
  * builds ONE digest (rows + summary) and renders it for each channel, so the
    email and the Telegram message can never disagree
  * per-row opt-out token in the footer → "stop showing me Turkey" actually
    suppresses future rows instead of you muttering at the inbox
  * dedupe key = hash of (ids + change types) → an unchanged DB sends nothing
  * daily cap, quiet hours, and a `dry_run` mode for CI
  * no sendgrid / python-telegram-bot dependency: SMTP + HTTPS APIs only
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import smtplib
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

from db.models import utcnow

log = logging.getLogger("notify")


@dataclass
class Row:
    scholarship: Any
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
    tier: str = ""
    score: float = 0.0
    days_left: int | None = None
    opt_out_token: str = ""


@dataclass
class Digest:
    rows: list[Row]
    subject: str
    generated_at: datetime = field(default_factory=utcnow)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def digest_key(self) -> str:
        parts = [f"{r.scholarship.id}:{r.scholarship.change_type or 'new'}" for r in self.rows]
        return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:32]

    def markdown(self) -> str:
        lines = [f"## 🎓 {self.subject}", ""]
        for i, r in enumerate(self.rows, 1):
            s = r.scholarship
            flags = " · ".join(x for x in (r.tier.replace("_", " "), f"score {r.score:.0f}", f"conf {s.confidence:.0%}") if x)
            deadline = s.deadline.strftime("%d %b %Y") if s.deadline else (s.deadline_text or "rolling")
            money = money_line(s)
            lines.append(f"**{i}. {s.title}**  ")
            lines.append(f"{s.country or '?'} · {s.degree_level or '?'} · {s.funding_type or 'funding ?'} · **{deadline}**  ")
            lines.append(f"💰 {money}  ")
            lines.append(f"`{flags}`")
            if r.breakdown:
                lines.append("▸ " + ", ".join(f"{k} {v:.0f}" for k, v in sorted(r.breakdown.items(), key=lambda kv: -kv[1])[:4]))
            if r.warnings:
                lines.append("⚠ " + "; ".join(r.warnings[:2]))
            lines.append(f"[Apply / details]({s.url})")
            lines.append("")
        if self.stats:
            lines.append("---")
            lines.append(
                "sources ok: {} · new: {} · changed: {} · LLM calls: {} (${:.3f})".format(
                    self.stats.get("sources_ok", "?"),
                    self.stats.get("new", "?"),
                    self.stats.get("changed", "?"),
                    self.stats.get("llm_calls", 0),
                    self.stats.get("llm_cost_usd", 0.0),
                )
            )
            if self.stats.get("sources_failed"):
                lines.append(f"⚠ source problems: {', '.join(self.stats['sources_failed'])}")
        return "\n".join(lines)

    def plain(self) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", self.markdown())
        text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1 — \2", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        return text

    def html(self) -> str:
        rows_html = []
        for i, r in enumerate(self.rows, 1):
            s = r.scholarship
            deadline = s.deadline.strftime("%d %b %Y") if s.deadline else html.escape(s.deadline_text or "rolling")
            warn = ""
            if r.warnings:
                warn = f'<p style="margin:6px 0;color:#9a3412;font-size:13px">⚠ {html.escape("; ".join(r.warnings[:3]))}</p>'
            pills = "".join(
                f'<span style="display:inline-block;background:#eef2ff;color:#3730a3;border-radius:10px;padding:2px 8px;'
                f'font-size:12px;margin-right:6px">{html.escape(k)} {v:.0f}</span>'
                for k, v in sorted((r.breakdown or {}).items(), key=lambda kv: -kv[1])[:4]
            )
            rows_html.append(
                f"""<tr><td style="padding:16px;border-bottom:1px solid #e5e7eb;font-family:Segoe UI,Arial,sans-serif">
                <div style="color:#6b7280;font-size:12px">#{i} · {html.escape(r.tier.replace('_',' ').title())} · score {r.score:.0f}/100</div>
                <h3 style="margin:6px 0 4px;font-size:18px;color:#111827">{html.escape(s.title)}</h3>
                <div style="font-size:14px;color:#374151">🌍 {html.escape(s.country or '?')} &nbsp;·&nbsp; 🎓 {html.escape(s.degree_level or '?')}
                &nbsp;·&nbsp; 💵 {html.escape(s.funding_type or 'funding ?')} &nbsp;·&nbsp; ⏰ <b>{deadline}</b></div>
                <div style="font-size:14px;color:#166534;margin:4px 0">{html.escape(money_line(s))}</div>
                <div style="margin:6px 0">{pills}</div>
                {warn}
                <a href="{html.escape(s.url)}" style="color:#1d4ed8;font-size:14px">Apply / read details →</a>
                </td></tr>"""
            )
        # Every link must go somewhere. A `href="#"` "not interested" /
        # "unsubscribe" control looks functional, trains you to click it, and then
        # does nothing — so it is only rendered when a real target exists, and the
        # opt-out token is printed as text you can pass to `cli.py` instead.
        if self.rows and any(r.opt_out_token for r in self.rows):
            rows_html.append(
                '<tr><td style="padding:10px 16px;border-top:1px solid #f3f4f6;font-family:Segoe UI,Arial,sans-serif;'
                'font-size:11px;color:#9ca3af">To mute one of these for good: '
                "<code>python cli.py query --all --json</code> prints the ids · opt-out tokens: "
                + html.escape(", ".join(r.opt_out_token for r in self.rows if r.opt_out_token)[:300])
                + "</code></td></tr>"
            )
        links = []
        dash = str(self.stats.get("dashboard_url") or "").strip()
        if dash and dash != "#":
            links.append(f'<a href="{html.escape(dash)}">open dashboard</a>')
        unsub = str(self.stats.get("unsubscribe_url") or "").strip()
        if unsub and unsub != "#":
            links.append(f'<a href="{html.escape(unsub)}">unsubscribe</a>')
        link_html = (" · " + " · ".join(links)) if links else ""
        stats = ""
        if self.stats:
            stats = (
                f'<p style="font-family:Segoe UI,Arial,sans-serif;color:#6b7280;font-size:12px">'
                f"{self.stats.get('sources_ok', '?')} sources ok · {self.stats.get('new', 0)} new · "
                f"{self.stats.get('changed', 0)} changed · LLM ${self.stats.get('llm_cost_usd', 0):.3f}"
                + (f" · problems: {html.escape(', '.join(self.stats['sources_failed']))}" if self.stats.get("sources_failed") else "")
                + "</p>"
            )
        return (
            f'<html><body style="background:#f9fafb;margin:0;padding:20px">'
            f'<div style="max-width:680px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">'
            f'<div style="background:#111827;color:#fff;padding:18px 16px">'
            f'<h2 style="margin:0;font-family:Segoe UI,Arial,sans-serif;font-size:20px">🎓 {html.escape(self.subject)}</h2>'
            f'<p style="margin:4px 0 0;color:#9ca3af;font-size:13px;font-family:Segoe UI,Arial,sans-serif">'
            f"{self.generated_at:%d %b %Y %H:%M} UTC · {len(self.rows)} matches</p></div>"
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(rows_html)}</table>'
            f'<div style="padding:12px 16px;border-top:1px solid #e5e7eb">{stats}'
            f'<p style="font-family:Segoe UI,Arial,sans-serif;font-size:11px;color:#9ca3af">'
            f"Automated by the scholarship tracker · replies are not read{link_html}</p></div>"
            f"</div></body></html>"
        )


# --------------------------------------------------------------------------- #
# digest construction
# --------------------------------------------------------------------------- #
def build_digest(
    rows: Iterable[Any],
    profile: Any,
    *,
    settings: Any | None = None,
    stats: dict[str, Any] | None = None,
    max_rows: int = 12,
) -> Digest | None:
    from matching.matcher import evaluate  # local import: avoids cycle

    items: list[Row] = []
    for sch in rows:
        res = evaluate(sch, profile, weights=(settings.scoring.get("weights") if settings else None),
                       thresholds=(settings.scoring.get("tiers") if settings else None))
        if not res.gate.passed:
            continue
        if res.score < float(settings.notify.get("min_score", 45) if settings else 45):
            continue
        reasons = res.gate.reasons
        token = opt_out_token(sch)
        items.append(
            Row(
                scholarship=sch,
                reasons=reasons,
                warnings=res.gate.warnings,
                breakdown=res.breakdown,
                tier=res.tier,
                score=res.score,
                days_left=res.days_left,
                opt_out_token=token,
            )
        )
    items.sort(key=lambda r: (-r.score, r.days_left if r.days_left is not None else 9999))
    items = items[:max_rows]
    if not items:
        return None
    top = items[0]
    subject = f"{len(items)} scholarship match{'es' if len(items) != 1 else ''} for you — top: {top.scholarship.country or 'global'} {top.scholarship.degree_level or ''}".strip()
    stats = dict(stats or {})
    if settings:
        stats.setdefault("dashboard_url", settings.notify.get("base_url", ""))
        secret = settings.notify.get("digest_secret") or "dev-secret"
        stats.setdefault("unsubscribe_url", f"{settings.notify.get('base_url','')}/optout?token={hashlib.sha256(secret.encode()).hexdigest()[:16]}")
    return Digest(rows=items, subject=subject[:120], stats=stats)


def opt_out_token(sch: Any) -> str:
    basis = f"{getattr(sch, 'id', 0)}|{getattr(sch, 'canonical_key', '')}|{getattr(sch, 'url', '')}"
    return hashlib.sha256(basis.encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# delivery policies
# --------------------------------------------------------------------------- #
class Policy:
    def __init__(self, cfg: dict[str, Any]):
        self.channels: list[str] = cfg.get("channels") or ["console"]
        self.min_score: float = float(cfg.get("min_score", 45))
        self.max_rows: int = int(cfg.get("max_rows", 12))
        self.daily_cap: int = int(cfg.get("daily_cap", 25))
        self.quiet_hours: tuple[int, int] = tuple(cfg.get("quiet_hours") or (0, 7))
        self.always_summary: bool = bool(cfg.get("always_summary", True))
        self.dry_run: bool = bool(cfg.get("dry_run", False))
        self.require_new: bool = bool(cfg.get("require_new", True))
        self.reschedule_window_hours = float(cfg.get("reschedule_window_hours", 20))

    def in_quiet_hours(self, when: datetime | None = None) -> bool:
        when = when or utcnow()
        start, end = self.quiet_hours
        if start == end:
            return False
        hour = when.hour
        return (start <= hour < end) if start < end else (hour >= start or hour < end)

    def already_sent(self, session: Any, digest_key: str) -> bool:
        from db.models import NotificationLog

        since = utcnow() - timedelta(hours=self.reschedule_window_hours)
        row = (
            session.query(NotificationLog)
            .filter(NotificationLog.digest_key == digest_key, NotificationLog.created_at >= since, NotificationLog.status == "sent")
            .first()
        )
        return row is not None

    def record(self, session: Any, channel: str, recipient: str | None, digest: Digest, status: str, detail: str = "") -> None:
        from db.models import NotificationLog

        session.add(
            NotificationLog(
                channel=channel,
                recipient=recipient,
                digest_key=digest.digest_key,
                count=len(digest.rows),
                status=status,
                detail=detail[:900],
            )
        )
        session.commit()


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #
def money_line(sch: Any) -> str:
    """What the site said, plus our annualised reading when it differs.

    `992 eur` on a DAAD page is a *monthly* stipend: ~$10,800/yr, not $1,081.
    Printing the raw string alone under-reports the award by 10×, printing only the
    derived number invents precision the source never stated — so both, labelled.
    """
    raw = (sch.amount_text or "").strip()
    usd = float(sch.amount_usd or 0)
    if not raw:
        return "amount not stated"
    if not usd:
        return raw
    try:
        import re as _re

        # A number in the sentence may be a *monthly* figure that was annualised,
        # so compare at both scales instead of insisting they match.
        mult = 10.0 if _re.search(r"\bmonth|/mo|p\.m", raw, _re.I) else 1.0
        nums = [float(x.replace(",", "")) * mult for x in _re.findall(r"\d[\d,.]{2,12}", raw)]
        if any(abs(n - usd) / max(usd, 1) < 0.06 for n in nums):
            return raw
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return f"{raw}  ·  ≈ US${usd:,.0f}/yr"


def send_console(digest: Digest, cfg: dict[str, Any]) -> tuple[str, str]:
    print("\n" + digest.markdown())
    return "sent", "console"


def send_telegram(digest: Digest, cfg: dict[str, Any]) -> tuple[str, str]:
    token = cfg.get("telegram_bot_token")
    chat = cfg.get("telegram_chat_id")
    if not token or not chat:
        return "skipped", "telegram not configured"
    # Telegram: 4096 char limit, MarkdownV2 is painful → plain text blocks
    chunks: list[str] = []
    buf = ""
    for line in digest.plain().splitlines():
        if len(buf) + len(line) + 1 > 3500:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    sent = 0
    last_err = ""
    # `telegram_api_base` is overridable so the chunking/error path can be
    # exercised against a local fake in tests instead of the real API.
    base = (cfg.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
    for chunk in chunks[: int(cfg.get("telegram_max_chunks", 4))]:
        ok, err = _post_form(
            f"{base}/bot{token}/sendMessage",
            {"chat_id": chat, "text": chunk, "disable_web_page_preview": "true", "parse_mode": "HTML"},
        )
        if ok:
            sent += 1
        else:
            last_err = err
            break
    if sent:
        return "sent", f"{sent} messages"
    return "failed", last_err or "no messages sent"


def _smtp_auth(smtp: Any, user: str, password: str, where: str = "") -> None:
    """Log in only when the server advertises AUTH.

    Plain `smtp.login()` raises SMTPNotSupportedError against anything without it —
    MailHog/Mailpit in CI, a localhost relay, an IP-allowlisted SES endpoint. Those
    are exactly the places a personal notifier gets pointed at, so authenticating is
    opportunistic rather than mandatory (the credentials are still required by config
    so a half-configured Gmail setup doesn't silently mail nothing).
    """
    if smtp.has_extn("auth"):
        smtp.login(user, password)
    else:
        log.warning("smtp %s does not advertise AUTH; sending unauthenticated", where or "server")


def send_email(digest: Digest, cfg: dict[str, Any]) -> tuple[str, str]:
    host = cfg.get("smtp_host")
    user = cfg.get("smtp_user")
    password = cfg.get("smtp_password")
    to = cfg.get("to_email")
    if not (host and user and password and to):
        return "skipped", "smtp not configured"
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = cfg.get("from_email", user)
    msg["To"] = to
    msg.set_content(digest.plain())
    msg.add_alternative(digest.html(), subtype="html")
    try:
        port = int(cfg.get("smtp_port", 587))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=45) as smtp:
                _smtp_auth(smtp, user, password, f"{host}:{port}")
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
                _smtp_auth(smtp, user, password, f"{host}:{port}")
                smtp.send_message(msg)
        return "sent", f"email → {to}"
    except Exception as exc:  # noqa: BLE001
        return "failed", f"smtp: {type(exc).__name__}: {exc}"


def send_webhook(digest: Digest, cfg: dict[str, Any]) -> tuple[str, str]:
    url = cfg.get("webhook_url")
    if not url:
        return "skipped", "no webhook configured"
    fmt = cfg.get("webhook_format", "slack")
    if fmt == "slack":
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{r.scholarship.title}*\n{r.score:.0f}/100 · {r.scholarship.country or '?'} · <{r.scholarship.url}|apply>"}} for r in digest.rows]
        payload = {"text": digest.subject, "blocks": blocks[:20]}
    elif fmt == "discord":
        payload = {"content": digest.subject, "embeds": [{"description": digest.plain()[:1800]}]}
    else:
        payload = {"subject": digest.subject, "rows": [r.scholarship.as_dict() for r in digest.rows]}
    status, err = _post_json(url, payload)
    return ("sent", fmt) if status else ("failed", err)


CHANNELS = {"console": send_console, "telegram": send_telegram, "email": send_email, "webhook": send_webhook}


def deliver(digest: Digest | None, settings: Any, session: Any, *, force: bool = False) -> dict[str, Any]:
    """Route a digest through every configured channel with dedupe + caps."""
    cfg = settings.notify or {}
    policy = Policy(cfg)
    result: dict[str, Any] = {"sent": {}, "skipped": {}, "rows": 0}
    if digest is None:
        msg = f"no rows above threshold {policy.min_score} — nothing to send"
        log.info(msg)
        result["skipped"]["all"] = msg
        return result
    dedupe_blocked = (not force) and policy.already_sent(session, digest.digest_key)
    if dedupe_blocked:
        result["skipped"]["all"] = f"duplicate digest {digest.digest_key[:10]}"
        print(f"[notify] suppressed: {result['skipped']['all']}")
        return result
    if len(digest.rows) > policy.daily_cap:
        digest.rows = digest.rows[: policy.daily_cap]
    result["rows"] = len(digest.rows)
    for channel in policy.channels:
        fn = CHANNELS.get(channel)
        if fn is None:
            result["skipped"][channel] = "unknown channel"
            continue
        if policy.in_quiet_hours() and not force and channel != "console":
            status, detail = "skipped", f"quiet hours ({policy.quiet_hours[0]:02d}:00-{policy.quiet_hours[1]:02d}:00 UTC)"
        elif policy.dry_run and not force:
            status, detail = "dry-run", f"would send {len(digest.rows)} rows"
        else:
            try:
                status, detail = fn(digest, cfg)
            except Exception as exc:  # noqa: BLE001
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
        result["sent" if status in ("sent", "dry-run") else "skipped"][channel] = detail
        if status in ("sent", "dry-run"):
            policy.record(session, channel, cfg.get("to_email") or cfg.get("telegram_chat_id"), digest, status, detail)
    if result["sent"]:
        print(f"[notify] {len(digest.rows)} rows → " + ", ".join(f"{k}:{v}" for k, v in result["sent"].items()))
    return result


def _post_form(url: str, fields: dict[str, str]) -> tuple[bool, str]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "ignore")
        ok = '"ok":true' in body.replace(" ", "")
        return ok, "" if ok else body[:200]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _post_json(url: str, payload: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
