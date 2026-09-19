"""Delivery path, exercised against local fakes — no network, no credentials.

This is the part of the project that cannot be eyeballed in a log: SMTP framing,
webhook payloads, Telegram chunking, quiet hours, caps and the dedupe guard.
It runs a real `smtplib` client against a real (tiny) server, because the bugs
that matter here are protocol-level — AUTH-not-advertised, missing STARTTLS,
dot-stuffing, 4096-char limits.
"""

from __future__ import annotations

import json
import pathlib
import re
import socket
import tempfile
import threading
from datetime import timedelta
from email import policy as email_policy
from email.parser import Parser

import pytest

from db.models import NotificationLog, Scholarship, utcnow

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tests"))
import smtp_sink


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def inbox():
    """SMTP sink + HTTP sink for webhook/telegram, both on ephemeral ports."""
    smtp_port, http_port = _free_port(), _free_port()
    sink_dir = pathlib.Path(tempfile.mkdtemp(prefix="smtpsink-"))
    smtp_sink.serve(smtp_port, out_dir=sink_dir)

    captured: dict[str, list] = {"webhook": [], "telegram": []}

    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8", "ignore")
            if "sendMessage" in self.path:
                import urllib.parse as up

                captured["telegram"].append(dict(up.parse_qsl(raw)))
                body = b'{"ok":true,"result":{"message_id":1}}'
            else:
                captured["webhook"].append(raw)
                body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    class Reuse(http.server.ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = Reuse(("127.0.0.1", http_port), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"smtp_port": smtp_port, "http_port": http_port, "captured": captured, "dir": sink_dir}
    httpd.shutdown()


@pytest.fixture()
def two_rows(tmp_path):
    """A scratch DB with two rows good enough to be notified."""
    from core.config import load_config
    from db.models import init_engine as ie
    from db.models import upsert_scholarship
    from db.session import get_session

    settings = load_config()
    settings = settings.__class__(
        profile=settings.profile,
        sources=[],
        scoring=settings.scoring,
        llm={"enabled": False},
        notify=settings.notify,
        runtime={**settings.runtime, "db_path": str(tmp_path / "t.db"), "out_dir": str(tmp_path / "out"),
                 "cache_dir": str(tmp_path / "cache")},
        path=settings.path,
    )
    ie(f"sqlite:///{tmp_path / 't.db'}", force=True)
    sess = get_session()
    base = dict(
        source="test", country="United Kingdom", degree_level="Master", funding_type="Full",
        field="Machine Learning", amount_usd=35000, amount_text="full funding", min_ielts=6.5,
        coverage=["tuition", "stipend"], status="active",
    )
    for i, title in enumerate(["Chevening Test Scholarship", "Second Ranked Match"]):
        row, outcome = upsert_scholarship(sess, title=title, url=f"https://x.test/{i}",
                                          deadline=utcnow() + timedelta(days=20 + i), **base)
        row.is_new, row.changed, row.change_type = True, True, "new"
    sess.commit()
    yield settings, sess
    sess.close()


def _digest(settings, sess):
    from core.pipeline import _notification_pool, _score_all
    from notifiers import build_digest

    scored = _score_all(sess, settings)
    pool = _notification_pool(sess, scored, force=True, settings=settings)
    return build_digest(pool, settings.profile, settings=settings,
                        stats={"sources_ok": 1, "new": len(pool), "changed": 0, "llm_calls": 0, "llm_cost_usd": 0.0})


def test_all_channels_reach_the_wire(two_rows, inbox):
    from notifiers import deliver

    settings, sess = two_rows
    settings.notify = {
        **settings.notify, "channels": ["console", "email", "webhook", "telegram"], "dry_run": False,
        "quiet_hours": [10, 13], "smtp_host": "127.0.0.1", "smtp_port": inbox["smtp_port"],
        "smtp_user": "u", "smtp_password": "p", "from_email": "Bot <bot@local>", "to_email": "me@example.com",
        "webhook_url": f"http://127.0.0.1:{inbox['http_port']}/services/slack", "webhook_format": "slack",
        "telegram_bot_token": "123:TEST", "telegram_chat_id": "42",
        "telegram_api_base": f"http://127.0.0.1:{inbox['http_port']}",
        "base_url": "https://me.github.io/dash", "digest_secret": "s3cret",
    }
    dg = _digest(settings, sess)
    assert dg is not None and len(dg.rows) == 2
    result = deliver(dg, settings, sess, force=False)
    assert set(result["sent"]) == {"console", "email", "webhook", "telegram"}, result

    # --- the SMTP conversation actually produced a parseable message ----------
    files = sorted(inbox["dir"].glob("msg*.txt"))
    assert len(files) == 1, "sink captured nothing — the send never completed"
    raw = files[0].read_text()
    # the sink prefixes the envelope on its own lines; drop them before parsing
    _, _, rest = raw.partition("\n\n")
    while rest.startswith(("FROM:", "TO:")):
        _, _, rest = rest.partition("\n")
        rest = rest.lstrip("\n")
    msg = Parser(policy=email_policy.default).parsestr(rest)
    assert msg["To"] == "me@example.com"
    assert "scholarship" in (msg["Subject"] or "").lower()
    kinds = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in kinds and "text/html" in kinds, kinds  # multipart/alternative

    html_part = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
    assert 'href="https://me.github.io/dash"' in html_part, "dashboard link missing"
    assert "href=\"#\"" not in html_part, "dead placeholder link shipped to a real inbox"
    assert "<script>" not in html_part.lower()
    assert "Apply / read details" in html_part
    assert msg.get_content_maintype() == "multipart"

    # --- webhook + telegram payloads -----------------------------------------
    wh = json.loads(inbox["captured"]["webhook"][0])
    assert wh["blocks"] and len(wh["blocks"]) == 2
    tg = inbox["captured"]["telegram"][0]
    assert tg["chat_id"] == "42" and "Chevening" in tg["text"]

    # --- every send is logged, and the log is what stops a resend -------------
    logged = sess.query(NotificationLog).filter(NotificationLog.status == "sent").all()
    assert {r.channel for r in logged} >= {"email", "webhook", "telegram"}


def test_dedupe_blocks_the_next_identical_digest(two_rows, inbox):
    from notifiers import deliver

    settings, sess = two_rows
    settings.notify = {**settings.notify, "channels": ["webhook"], "dry_run": False, "quiet_hours": [10, 13],
                       "webhook_url": f"http://127.0.0.1:{inbox['http_port']}/x"}
    dg = _digest(settings, sess)
    assert deliver(dg, settings, sess)["sent"].get("webhook")
    again = deliver(dg, settings, sess)
    assert again["sent"] == {} and "duplicate digest" in again["skipped"].get("all", "")
    # force=True must override the guard (that's how a manual "send it again" works)
    assert deliver(dg, settings, sess, force=True)["sent"].get("webhook")


def test_quiet_hours_suppress_everything_but_the_console(two_rows, inbox):
    from notifiers import deliver

    settings, sess = two_rows
    settings.notify = {**settings.notify, "channels": ["console", "email", "webhook"], "dry_run": False,
                       "quiet_hours": [0, 24], "smtp_host": "127.0.0.1", "smtp_port": inbox["smtp_port"],
                       "smtp_user": "u", "smtp_password": "p", "to_email": "me@example.com",
                       "webhook_url": f"http://127.0.0.1:{inbox['http_port']}/x"}
    result = deliver(_digest(settings, sess), settings, sess)
    assert set(result["sent"]) == {"console"}, "a quiet window must never swallow the local run output"
    assert set(result["skipped"]) == {"email", "webhook"}
    assert "quiet hours" in result["skipped"]["email"]


def test_missing_credentials_skip_instead_of_failing(two_rows):
    from notifiers import send_email, send_telegram, send_webhook

    settings, sess = two_rows
    dg = _digest(settings, sess)
    for fn in (send_email, send_telegram, send_webhook):
        status, detail = fn(dg, {})
        assert status == "skipped" and "not configured" in detail or "no webhook" in detail, (fn.__name__, status, detail)


def test_broken_smtp_reports_failure_without_raising(two_rows):
    from notifiers import send_email

    settings, sess = two_rows
    dg = _digest(settings, sess)
    status, detail = send_email(dg, {"smtp_host": "127.0.0.1", "smtp_port": _free_port(), "smtp_user": "u",
                                    "smtp_password": "p", "to_email": "a@b.c"})
    assert status == "failed"
    assert re.search(r"Connection|refused|timed out", detail, re.IGNORECASE)


def test_long_digest_is_chunked_for_telegram(two_rows, inbox):
    """Telegram's hard limit is 4096 chars; a 20-row digest must be split, not truncated."""
    from datetime import timedelta

    from core.pipeline import _score_all
    from db.models import upsert_scholarship
    from notifiers import build_digest, send_telegram

    settings, sess = two_rows
    for i in range(18):
        upsert_scholarship(sess, title=f"Many Row Scholarship Programme {i:02d} for International Students",
                           url=f"https://y.test/{i}", source="test", country="Germany", degree_level="Master",
                           funding_type="Full", amount_usd=40000 + i, min_ielts=6.0,
                           deadline=utcnow() + timedelta(days=30 + i), status="active")
    for row in sess.query(Scholarship):
        row.is_new, row.changed, row.change_type = True, True, "new"
    sess.commit()
    pool = [s for s in sess.query(Scholarship).filter(Scholarship.status == "active").all()]
    _score_all(sess, settings)
    dg = build_digest(pool, settings.profile, settings=settings,
                      stats={"sources_ok": 1, "new": len(pool), "changed": 0, "llm_calls": 0, "llm_cost_usd": 0.0})
    assert dg is not None
    cfg = {"telegram_bot_token": "t", "telegram_chat_id": "9", "telegram_api_base": f"http://127.0.0.1:{inbox['http_port']}",
           "telegram_max_chunks": 4}
    status, detail = send_telegram(dg, cfg)
    assert status == "sent", detail
    sent = inbox["captured"]["telegram"]
    assert 1 <= len(sent) <= 4
    assert all(len(m["text"]) <= 4096 for m in sent), [len(m["text"]) for m in sent]
    total = "".join(m["text"] for m in sent)
    assert "Many Row Scholarship Programme 00" in total
