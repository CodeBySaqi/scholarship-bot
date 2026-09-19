"""Tests for the control panel.

`dashboard.api.dispatch` is called directly (no socket) for the routing and
data tests, and `TestServer` binds a real ephemeral port to prove the token
gate and the cookie flow. Telegram is served by a throwaway local HTTP server
through the `notify.telegram_api_base` override, so nothing in here talks to
api.telegram.org and no real bot is involved.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from dashboard import api as API
from dashboard import settings_io as S
from dashboard import telegram as TG
from dashboard.jobs import Busy, Runner
from dashboard.patch import apply_env, env_keys_present, set_list_item_value, set_yaml_value
from dashboard.query import (
    archive_stats,
    facets,
    get_scholarship,
    list_scholarships,
    set_flags,
)
from dashboard.ui import LOGIN_HTML, PAGE

ROOT = Path(__file__).resolve().parent.parent
GOOD_TOKEN = "123456789:AAgoodgoodgoodgoodgoodgoodgoodgoodgoo"
BAD_TOKEN = "987654321:AAwrongwrongwrongwrongwrongwrongwrong"


# --------------------------------------------------------------------- helpers
def sample(session, **kw):
    """Insert a row through the pipeline's own upsert (it computes the derived keys)."""
    from db.models import upsert_scholarship, utcnow

    base = dict(  # noqa: C408 - kwargs read better here than a string-keyed literal
        title="Commonwealth Master's Scholarship in UK",
        url=f"https://example.com/{abs(hash(tuple(sorted(kw.items())))) % 10**8}",
        source="scholars4dev_masters",
        provider="CSC",
        country="United Kingdom",
        degree_level="Master",
        field="Artificial Intelligence",
        funding_type="Full",
        amount_text="£18,000 per year",
        amount_usd=22500.0,
        currency="USD",
        deadline=utcnow().replace(year=utcnow().year + 1, month=10, day=6),
        min_gpa=3.0,
        min_ielts=6.5,
        eligibility="Open to Commonwealth applicants",
        description="Tuition plus a stipend",
        coverage=json.dumps(["tuition", "stipend"]),
        match_score=88.5,
        tier="must_apply",
        confidence=1.0,
        gate_pass=True,
        status="active",
        parse_method="regex",
        llm_used=False,
        times_seen=1,
        seen_in_sources="scholars4dev_masters",
        score_breakdown=json.dumps({"breakdown": {"country": 22}, "warnings": []}),
        gate_reasons=json.dumps([]),
    )
    base.update(kw)
    row, _outcome = upsert_scholarship(session, **base)
    session.commit()
    return row


@pytest.fixture
def ctx(tmp_path, settings):
    """A Ctx whose config/.env/DB are all inside tmp_path.

    The DB path is deliberately the same file the autouse `_isolate_db` fixture
    bound to the global engine, otherwise the panel and the test data would look
    at different databases and the assertions would be about plumbing, not code.
    """
    cfg_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    db = tmp_path / "test.db"
    for path, value in ((["runtime", "db_path"], str(db)), (["runtime", "cache_dir"], str(tmp_path / "cache")),
                        (["runtime", "out_dir"], str(tmp_path / "out")), (["llm", "enabled"], False),
                        (["notify", "channels"], ["console"]), (["notify", "dry_run"], True)):
        cfg_text = set_yaml_value(cfg_text, path, value)
    cfg_text = set_yaml_value(cfg_text, ["notify", "telegram_api_base"], "http://127.0.0.1:%d")
    (tmp_path / "config.yaml").write_text(cfg_text, encoding="utf-8")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env")
    (tmp_path / "cache").mkdir(exist_ok=True)
    return API.Ctx(root=tmp_path, config_path=tmp_path / "config.yaml", env_path=tmp_path / ".env")


@pytest.fixture(autouse=True)
def _restore_secret_env():
    """`write_settings` pushes saved secrets into os.environ on purpose (so the
    running server uses them immediately). Tests must not leak that into each other.
    """
    keys = [spec["key"] for spec in S.SECRETS] + ["SMTP_PASSWORD", "SMTP_USER", "FROM_EMAIL", "TO_EMAIL"]
    before = {k: os.environ.get(k) for k in keys}
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def get(ctx, path, query=""):
    return API.dispatch(ctx, "GET", path, query, None)


def post(ctx, path, body):
    return API.dispatch(ctx, "POST", path, "", json.dumps(body).encode())


def payload(response):
    status, headers, body = response
    if "json" in headers.get("Content-Type", ""):
        return status, json.loads(body.decode())
    return status, body.decode("utf-8", "replace")


# ---------------------------------------------------------------- patch.py
class TestPatch:
    def test_types_and_quotes(self, tmp_path):
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        out = set_yaml_value(text, ["profile", "gpa"], 3.9)
        out = set_yaml_value(out, ["profile", "needs_full_funding"], False)
        out = set_yaml_value(out, ["notify", "telegram_chat_id"], "-100999")
        out = set_yaml_value(out, ["profile", "ielts"], None)
        data = yaml.safe_load(out)
        assert data["profile"]["gpa"] == 3.9
        assert data["profile"]["needs_full_funding"] is False
        assert data["notify"]["telegram_chat_id"] == "-100999"      # stays a string
        assert data["profile"]["ielts"] is None
        assert out.count("#") == text.count("#")                     # comments survived

    def test_nested_weight_and_new_key(self):
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        out = set_yaml_value(text, ["scoring", "weights", "country"], 41)
        out = set_yaml_value(out, ["notify", "brand_new"], ["a", "b"])
        data = yaml.safe_load(out)
        assert data["scoring"]["weights"]["country"] == 41
        assert data["notify"]["brand_new"] == ["a", "b"]

    def test_unknown_section_raises_instead_of_skipping(self):
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        with pytest.raises(KeyError, match="unknown section"):
            set_yaml_value(text, ["nope", "gpa"], 1)
        # The text editor is deliberately generic — adding a key is not its job to
        # police; `settings_io.spec_for` is what restricts the page to real fields.
        out = set_yaml_value(text, ["profile", "no_such_field_here"], 1)
        assert yaml.safe_load(out)["profile"]["no_such_field_here"] == 1

    def test_source_item_edit(self):
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        out = set_list_item_value(text, "sources", "id", "scholars4dev_masters", "pages", 4)
        out = set_list_item_value(out, "sources", "id", "scholars4dev_masters", "enabled", False)
        src = next(s for s in yaml.safe_load(out)["sources"] if s["id"] == "scholars4dev_masters")
        assert (src["pages"], src["enabled"]) == (4, False)
        with pytest.raises(KeyError, match="no source"):
            set_list_item_value(text, "sources", "id", "ghost", "pages", 1)

    def test_env_apply_and_remove(self):
        base = (ROOT / ".env.example").read_text(encoding="utf-8")
        out = apply_env(base, {"TELEGRAM_BOT_TOKEN": GOOD_TOKEN, "WEBHOOK_URL": "https://x.example/a b"})
        assert f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}" in out
        assert 'WEBHOOK_URL="https://x.example/a b"' in out          # spaces get quoted
        present = env_keys_present(out)
        assert present["TELEGRAM_BOT_TOKEN"] is True
        removed = apply_env(out, {"TELEGRAM_BOT_TOKEN": ""})
        assert env_keys_present(removed).get("TELEGRAM_BOT_TOKEN") is None

    def test_atomic_write_leaves_no_temp(self, tmp_path):
        from dashboard.patch import write_text_atomic

        target = tmp_path / "sub" / "config.yaml"
        write_text_atomic(target, "a: 1\n")
        assert target.read_text() == "a: 1\n"
        assert [q.name for q in target.parent.iterdir()] == ["config.yaml"]
        write_text_atomic(target, "a: 2\n")           # overwrite, still no leftovers
        assert [q.name for q in target.parent.iterdir()] == ["config.yaml"]


# ----------------------------------------------------------------- query.py
class TestQuery:
    def test_filters_sort_and_paging(self, ctx, settings):
        from db.session import get_session

        session = get_session()
        a = sample(session, title="Chevening UK award", match_score=91.0)
        b = sample(session, title="Japan MEXT", country="Japan", match_score=61.0, tier="strong")
        gated = sample(session, title="Hungarian Stipendium", country="Hungary", match_score=40.0,
                       tier="low", gate_pass=False, gate_reasons=json.dumps(["GPA below the minimum"]))
        out = list_scholarships(session)
        assert out["total"] == 3
        assert out["rows"][2]["gate_reasons"] == ["GPA below the minimum"]   # decoded from JSON text
        assert [r["title"] for r in out["rows"]] == ["Chevening UK award", "Japan MEXT", "Hungarian Stipendium"]
        assert "days_left" in out["rows"][0] and "deadline" in out["rows"][0]
        assert list_scholarships(session, country="Japan")["total"] == 1
        assert list_scholarships(session, search="mext")["total"] == 1
        failing = list_scholarships(session, gate="fail")
        assert failing["total"] == 1 and failing["rows"][0]["id"] == gated.id
        assert list_scholarships(session, tier="strong")["total"] == 1
        assert list_scholarships(session, sort="title")["rows"][0]["title"].startswith("Chevening")
        assert list_scholarships(session, limit=1, offset=1)["rows"][0]["title"] == "Japan MEXT"
        assert list_scholarships(session, source="scholars4dev_masters")["total"] == 3
        assert list_scholarships(session, source="nope")["total"] == 0
        fac = facets(session)
        assert {"Japan", "United Kingdom", "Hungary"} <= {f["value"] for f in fac["country"]}
        # ids are stable for the detail endpoint
        assert get_scholarship(session, a.id)["title"] == "Chevening UK award"
        assert get_scholarship(session, b.url[-12:])["title"] == "Japan MEXT"
        assert get_scholarship(session, "nothing-matches") is None

    def test_hidden_and_starred(self, ctx, settings):
        from db.session import get_session

        session = get_session()
        row = sample(session, title="To hide")
        assert set_flags(session, row.id, starred=True)["starred"] is True
        assert list_scholarships(session, starred_only=True)["total"] == 1
        flags = set_flags(session, row.id, hidden=True)
        assert flags["hidden"] is True and flags["starred"] is False   # hiding drops the star
        assert list_scholarships(session)["total"] == 0
        assert list_scholarships(session, include_hidden=True)["total"] == 1
        assert set_flags(session, row.id, notes="email asked about IELTS waiver")["notes"].startswith("email")
        with pytest.raises(KeyError):
            set_flags(session, 999999)
        stats = archive_stats(session)
        assert stats["hidden"] == 1 and stats["starred"] == 0
        assert stats["rows"] == 1 and stats["gate_pass"] == 0           # hidden excluded

    def test_stats_shape(self, ctx, settings):
        from db.session import get_session

        session = get_session()
        sample(session)
        stats = archive_stats(session)
        assert stats["active"] == 1
        assert stats["soonest_deadline"]
        assert stats["by_tier"]["must_apply"] == 1
        assert stats["max_award_usd"] == 22500.0


# -------------------------------------------------------------- settings_io
class TestSettingsIO:
    def test_coerce_matrix(self):
        spec = lambda t, **kw: S._f("k", "L", t, section="profile", **kw)      # noqa: E731
        assert S.coerce(spec("bool"), "on") is True
        assert S.coerce(spec("bool"), "false") is False
        assert S.coerce(spec("int"), "42") == 42
        assert S.coerce(spec("int", nullable=True), "") is None
        with pytest.raises(ValueError, match="cannot be empty"):
            S.coerce(spec("int"), "")          # empty must not silently become 0
        with pytest.raises(ValueError, match="whole number"):
            S.coerce(spec("int"), "12.5")
        with pytest.raises(ValueError, match="needs a number"):
            S.coerce(spec("float"), "abc")
        assert S.coerce(spec("float"), "3.5") == 3.5
        assert S.coerce(spec("list"), "AI, Security ,,") == ["AI", "Security"]
        assert S.coerce(spec("text"), " x ") == "x"
        assert S.coerce(spec("text", nullable=True), "") is None
        with pytest.raises(ValueError, match="not one of"):
            S.coerce(spec("select", options=("Master", "PhD")), "Bachelors")

    def test_snapshot_reads_real_values(self, ctx):
        snap = S.snapshot(ctx.settings())
        assert snap["profile"]["degree"] == "Master"
        assert snap["gates"]["needs_full_funding"] is True
        assert snap["scoring"]["must_apply"] == 76
        assert snap["notify"]["quiet_hours_start"] == 0 and snap["notify"]["quiet_hours_end"] == 7
        assert snap["runtime"]["min_interval_seconds"] == pytest.approx(2.0)
        # every editable field must have a value (None allowed only if nullable)
        missing = [f"{g}.{k}" for g, vals in snap.items() for k, v in vals.items()
                   if v is None and not (S.spec_for(g, k)["nullable"])]
        assert missing == [], missing

    def test_build_patches_pairs_quiet_hours(self):
        patches = {tuple(p): v for p, v in S.build_patches("notify", {
            "quiet_hours_start": "22", "quiet_hours_end": "5", "min_score": "61"})}
        assert patches[("notify", "quiet_hours")] == [22, 5]
        assert patches[("notify", "min_score")] == 61
        with pytest.raises(ValueError, match="at least one hour"):
            S.build_patches("notify", {"quiet_hours_start": "9", "quiet_hours_end": "9"})
        with pytest.raises(ValueError, match="0-23"):
            S.build_patches("notify", {"quiet_hours_start": "25", "quiet_hours_end": "9"})

    def test_spec_lookup_across_sibling_groups(self):
        # `gates` fields live in the profile section; the page submits either group
        assert S.spec_for("gates", "min_award_usd")["section"] == "profile"
        assert S.spec_for("profile", "min_award_usd")["section"] == "profile"
        assert S.spec_for("scoring", "must_apply")["section"] == "scoring.tiers"
        with pytest.raises(KeyError, match="not editable"):
            S.spec_for("profile", "db_path")

    def test_write_reload_and_rollback(self, ctx):
        patches = S.build_patches("profile", {"gpa": "2.9", "ielts": "8"})
        patches += S.build_patches("gates", {"needs_full_funding": "false"})
        patches += S.build_patches("scoring", {"country": "30"})
        result = S.write_settings(ctx.config_path, config_patches=patches)
        assert result["changed"] is True and result["config_text_changed"] is True
        data = yaml.safe_load(ctx.config_path.read_text(encoding="utf-8"))
        assert data["profile"]["gpa"] == 2.9
        assert (data["profile"]["gpa"], data["profile"]["ielts"]) == (2.9, 8)
        assert data["profile"]["needs_full_funding"] is False
        assert data["scoring"]["weights"]["country"] == 30
        original = (ROOT / "config.yaml").read_text(encoding="utf-8")
        assert data["profile"]["fields"] == yaml.safe_load(original)["profile"]["fields"]
        assert ctx.config_path.read_text(encoding="utf-8").count("#") == original.count("#")
        assert len(list(ctx.config_path.parent.glob("config.yaml.bak-*"))) == 1

        before = ctx.config_path.read_text(encoding="utf-8")
        with pytest.raises(KeyError):        # unknown section never reaches the file
            S.write_settings(ctx.config_path, config_patches=[(["ghost", "x"], 1)])
        assert ctx.config_path.read_text(encoding="utf-8") == before

    def test_broken_yaml_is_restored(self, ctx, monkeypatch):
        # Simulate a write that produces a config the loader rejects.
        real = S.set_yaml_value

        def sabotage(text, path, value):
            out = real(text, path, value)
            return out + "\n  orphan_indent: [\n"

        monkeypatch.setattr(S, "set_yaml_value", sabotage)
        ctx._settings = None                       # a fresh load must see the sabotage
        with pytest.raises(S.ConfigRejected):
            S.write_settings(ctx.config_path, config_patches=[(["notify", "min_score"], 70)])
        data = yaml.safe_load(ctx.config_path.read_text(encoding="utf-8"))
        assert data["notify"]["min_score"] == 58                      # rolled back
        assert not ctx.config_path.read_text(encoding="utf-8").lstrip().startswith("  orphan")

    def test_source_add_delete_and_edit(self, ctx):
        ctx.refresh()
        S.write_settings(ctx.config_path,
                         source_add={"id": "ui_added", "kind": "listing", "url": "https://a.example/list",
                                     "enabled": True, "priority": 30, "pages": 2, "note": "from the page"})
        ctx.refresh()
        src = next(s for s in ctx.settings().sources if s["id"] == "ui_added")
        assert (src["kind"], src["pages"], src["note"]) == ("listing", 2, "from the page")
        with pytest.raises(ValueError, match="already exists"):
            S.write_settings(ctx.config_path, source_add={"id": "ui_added", "kind": "listing", "url": "x"})
        with pytest.raises(ValueError, match="needs"):
            S.write_settings(ctx.config_path, source_add={"id": "nope"})
        S.write_settings(ctx.config_path, source_edits=[{"id": "ui_added", "key": "enabled", "value": False}])
        ctx.refresh()
        assert next(s for s in ctx.settings().sources if s["id"] == "ui_added")["enabled"] is False
        S.write_settings(ctx.config_path, source_delete="ui_added")
        ctx.refresh()
        assert "ui_added" not in ctx.config_path.read_text(encoding="utf-8")
        with pytest.raises(KeyError):
            S.write_settings(ctx.config_path, source_delete="ui_added")

    def test_secrets_land_in_env_never_in_config(self, ctx):
        result = S.write_settings(ctx.config_path, env_path=ctx.env_path,
                                  env_values={"TELEGRAM_BOT_TOKEN": GOOD_TOKEN, "TELEGRAM_CHAT_ID": "-10042"})
        ctx.refresh()
        assert result["env_keys"]["TELEGRAM_BOT_TOKEN"] is True
        assert GOOD_TOKEN in (ctx.env_path).read_text(encoding="utf-8")
        assert GOOD_TOKEN not in ctx.config_path.read_text(encoding="utf-8")
        assert ctx.settings().notify["telegram_bot_token"] == GOOD_TOKEN
        import os

        assert os.environ["TELEGRAM_BOT_TOKEN"] == GOOD_TOKEN          # live process sees it
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

        status = S.secret_status(ctx.env_path, ctx.settings())
        blob = json.dumps(status)
        assert GOOD_TOKEN not in blob and "…" in status["TELEGRAM_BOT_TOKEN"]["masked"]
        assert status["TELEGRAM_BOT_TOKEN"]["in_env"] is True
        assert status["WEBHOOK_URL"]["in_env"] is False

    def test_clearing_a_secret_removes_the_line_and_the_env_var(self, ctx):
        import os

        S.write_settings(ctx.config_path, env_path=ctx.env_path, env_values={"TELEGRAM_CHAT_ID": "-10042"})
        assert os.environ.get("TELEGRAM_CHAT_ID") == "-10042"
        S.write_settings(ctx.config_path, env_path=ctx.env_path, env_values={"TELEGRAM_CHAT_ID": ""})
        assert "TELEGRAM_CHAT_ID" not in ctx.env_path.read_text(encoding="utf-8")
        assert "TELEGRAM_CHAT_ID" not in os.environ
        assert ctx.settings().notify.get("telegram_chat_id") in (None, "")


# ------------------------------------------------------------------ telegram
class FakeTelegram(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        method = self.path.rsplit("/", 1)[-1]
        fields = dict(kv.split("=", 1) for kv in body.split("&") if "=" in kv)
        FakeTelegram.calls.append((method, fields))
        token = self.path.split("/bot")[1].split("/")[0]
        if token != GOOD_TOKEN:
            out = {"ok": False, "description": "Unauthorized"}
        elif method == "getMe":
            out = {"ok": True, "result": {"id": 7, "username": "my_radar_bot", "first_name": "Radar"}}
        elif method == "getUpdates":
            out = {"ok": True, "result": [{"update_id": 3, "message": {
                "text": "/start", "from": {"username": "saqi"},
                "chat": {"id": -100987, "type": "group", "title": "Radar alerts"}}}]}
        elif method == "deleteWebhook":
            out = {"ok": True, "result": True}
        elif method == "sendMessage":
            out = {"ok": True, "result": {"message_id": 42 + len(FakeTelegram.calls)}}
        else:
            out = {"ok": False, "description": f"unsupported {method}"}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def fake_tg():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeTelegram.calls = []
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def tg_ctx(ctx, fake_tg):
    text = ctx.config_path.read_text(encoding="utf-8")
    text = set_yaml_value(text, ["notify", "telegram_api_base"], f"http://127.0.0.1:{fake_tg}")
    ctx.config_path.write_text(text, encoding="utf-8")
    ctx.refresh()
    return ctx


class TestTelegram:
    def test_parsers(self):
        assert TG.looks_like_token(GOOD_TOKEN)
        assert not TG.looks_like_token("123456:short")
        assert TG.token_from_url(f"https://api.telegram.org/bot{GOOD_TOKEN}/sendMessage") == GOOD_TOKEN
        assert TG.token_from_url("123456789:AAH1234567890abcdefghijABCDEFGHIJKL")
        assert TG.token_from_url("https://t.me/MyRadarBot") is None
        assert TG.chat_id_from_link("https://t.me/c/123456789") == "123456789"
        assert TG.chat_id_from_link(" t.me/-10012345 ") == "-10012345"
        assert TG.chat_id_from_link("not a link") is None
        assert TG.describe_token(GOOD_TOKEN).startswith("123456789:")
        assert GOOD_TOKEN not in TG.describe_token(GOOD_TOKEN)

    def test_chunking_keeps_blocks_whole(self):
        text = "\n\n".join(f"• item {i} " + "x" * 40 for i in range(40))
        parts = TG.chunk_text(text, limit=400)
        assert all(len(p) <= 400 for p in parts)
        assert "\n\n".join(parts).split() == text.split()
        assert TG.chunk_text("short", 400) == ["short"]
        assert TG.chunk_text("", 400) == []
        assert [len(p) for p in TG.chunk_text("y" * 1000, limit=400)] == [400, 400, 200]

    def test_api_url_honours_base_override(self):
        assert TG.api_url("getMe", "1:x") == "https://api.telegram.org/bot1:x/getMe"
        assert TG.api_url("getMe", "1:x", "http://127.0.0.1:9/") == "http://127.0.0.1:9/bot1:x/getMe"

    def test_call_errors_are_readable(self):
        with pytest.raises(TG.TelegramError, match="No bot token"):
            TG.call("getMe", "")
        settings = type("S", (), {"notify": {"telegram_bot_token": BAD_TOKEN,
                                             "telegram_api_base": "http://127.0.0.1:1"}})()
        with pytest.raises(TG.TelegramError):
            TG.get_me(settings)   # unreachable host → the network error branch

    def test_pair_send_and_verify(self, tg_ctx):
        me = TG.get_me(tg_ctx.settings(), token=GOOD_TOKEN)
        assert me["username"] == "my_radar_bot"
        pair = TG.pair(tg_ctx.settings(), token=GOOD_TOKEN)
        assert pair["chat_id"] == "-100987"
        assert pair["chats"][0]["title"] == "Radar alerts"
        assert pair["next_offset"] == 4
        with pytest.raises(TG.TelegramError, match="No chat id"):
            TG.send(tg_ctx.settings(), None, "hello there", token=GOOD_TOKEN)
        sent = TG.send(tg_ctx.settings(), "-100987", "hello there", token=GOOD_TOKEN)
        assert sent["messages"] == 1 and sent["chat_id"] == "-100987" and sent["message_ids"]
        kinds = [c[0] for c in FakeTelegram.calls]
        assert {"getMe", "getUpdates", "sendMessage"} <= set(kinds)
        # pairing must clear any webhook first, or getUpdates is always empty
        assert kinds.index("deleteWebhook") < kinds.index("getUpdates")
        assert any("chat_id=-100987" in urllib.parse.urlencode(c[1]) for c in FakeTelegram.calls)


# ----------------------------------------------------------------- dispatch
class TestApi:
    def test_status_masks_secrets(self, tg_ctx):
        S.write_settings(tg_ctx.config_path, env_path=tg_ctx.env_path,
                         env_values={"TELEGRAM_BOT_TOKEN": GOOD_TOKEN, "SMTP_PASSWORD": "hunter2"})
        tg_ctx.refresh()
        status, body = payload(get(tg_ctx, "/api/status"))
        assert status == 200
        assert GOOD_TOKEN not in json.dumps(body) and "hunter2" not in json.dumps(body)
        assert body["telegram"]["configured"] is True
        assert body["writable"] is True
        assert body["notify"]["channels"] == ["console"]
        assert {s["id"] for s in body["sources"]} >= {"scholars4dev_masters"}
        assert "rows" in body["counts"] and "by_tier" in body["counts"]
        assert body["secrets"]["SMTP_PASSWORD"]["masked"] != "hunter2"

    def test_settings_roundtrip(self, tg_ctx):
        status, view = payload(get(tg_ctx, "/api/settings"))
        assert status == 200 and view["groups"]["profile"]["gpa"] == 3.5
        assert {g: len(v) for g, v in view["specs"].items()}["notify"] >= 8
        status, saved = payload(post(tg_ctx, "/api/settings", {"groups": {"notify": {
            "min_score": "67", "channels": ["console", "telegram"]}}}))
        assert status == 200 and saved["changed"] is True
        notify = yaml.safe_load(tg_ctx.config_path.read_text(encoding="utf-8"))["notify"]
        assert notify["min_score"] == 67 and notify["channels"] == ["console", "telegram"]
        status, bad = payload(post(tg_ctx, "/api/settings", {"groups": {"profile": {"degree": "Masters"}}}))
        assert status == 422 and "not one of" in bad["error"]
        status, unknown = payload(post(tg_ctx, "/api/settings", {"groups": {"runtime": {"db_path": "/tmp/evil"}}}))
        assert status == 422 and "not editable" in unknown["error"]

    def test_read_only_server_refuses_writes(self, tg_ctx):
        tg_ctx.allow_writes = False
        status, body = payload(post(tg_ctx, "/api/settings", {"groups": {"notify": {"min_score": "10"}}}))
        assert status == 403 and "read-only" in body["error"]
        assert payload(get(tg_ctx, "/api/status"))[1]["writable"] is False
        status, body = payload(post(tg_ctx, "/api/jobs", {"kind": "run"}))
        assert status == 403

    def test_scholarship_endpoints(self, tg_ctx, settings):
        from db.session import get_session

        session = get_session()
        row = sample(session, title="Erasmus Mundus joint master")
        status, body = payload(get(tg_ctx, "/api/scholarships", "q=mundus&limit=5"))
        assert status == 200 and body["total"] == 1
        assert body["facets"]["country"][0]["value"]
        status, detail = payload(get(tg_ctx, f"/api/scholarships/{row.id}"))
        assert status == 200 and detail["score_breakdown"]["breakdown"]["country"] == 22
        assert detail["coverage"] == ["tuition", "stipend"] or detail["coverage"]
        status, body = payload(post(tg_ctx, f"/api/scholarships/{row.id}", {"star": True, "notes": "ask"}))
        assert body == {"id": row.id, "starred": True, "hidden": False, "notes": "ask"}
        assert payload(get(tg_ctx, f"/api/scholarships/{row.id}"))[1]["starred"] is True
        assert payload(get(tg_ctx, "/api/scholarships", "starred=1"))[1]["total"] == 1
        assert payload(get(tg_ctx, "/api/scholarships", "q=nothinghere"))[1]["rows"] == []
        assert payload(get(tg_ctx, "/api/scholarships/424242"))[0] == 404
        assert payload(post(tg_ctx, "/api/scholarships/424242", {"star": True}))[0] == 404

    def test_export_allowlist(self, tg_ctx):
        status, body = payload(get(tg_ctx, "/api/exports"))
        assert status == 200 and all(not f["exists"] for f in body["files"])
        missing = get(tg_ctx, "/api/export/scholarships.csv")
        assert missing[0] == 404 and b"not generated" in missing[2]
        assert get(tg_ctx, "/api/export/../../etc/passwd")[0] == 404
        out = Path(tg_ctx.settings().runtime["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "scholarships.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        status, headers, body = get(tg_ctx, "/api/export/scholarships.csv")
        assert status == 200 and body == b"a,b\n1,2\n" and "csv" in headers["Content-Type"]
        assert payload(get(tg_ctx, "/api/exports"))[1]["files"][2]["exists"] is True

    def test_raw_config(self, tg_ctx):
        status, body = payload(get(tg_ctx, "/api/raw-config"))
        assert status == 200 and body["path"].endswith("config.yaml") and "sources:" in body["text"]

    def test_route_errors(self, tg_ctx):
        status, body = payload(get(tg_ctx, "/api/nope"))
        assert status == 404 and "no route" in body["error"]
        status, body = payload(get(tg_ctx, "/api/settings", ""))
        assert status == 200
        status, body = payload(API.dispatch(tg_ctx, "DELETE", "/api/settings", "", None))
        assert status == 405 and "GET" in body["error"]
        status, body = payload(API.dispatch(tg_ctx, "POST", "/api/settings", "", b"{oops"))
        assert status == 400 and "not JSON" in body["error"]
        assert payload(API.dispatch(tg_ctx, "POST", "/api/settings", "", b"[1,2]"))[0] == 400

    def test_source_toggle_through_api(self, tg_ctx):
        first = next(s["id"] for s in payload(get(tg_ctx, "/api/status"))[1]["sources"])
        status, body = payload(post(tg_ctx, "/api/settings",
                                    {"source_edits": [{"id": first, "key": "enabled", "value": "false"}]}))
        assert status == 200
        assert next(s for s in payload(get(tg_ctx, "/api/status"))[1]["sources"] if s["id"] == first)["enabled"] is False
        assert payload(post(tg_ctx, "/api/settings", {"source_edits": [{"id": first, "key": "secret_key", "value": "1"}]}))[0] == 422

    def test_telegram_actions(self, tg_ctx):
        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "verify", "token": GOOD_TOKEN}))
        assert status == 200 and body["bot"]["username"] == "my_radar_bot"
        assert tg_ctx.telegram_state["verified"] is True
        assert GOOD_TOKEN in tg_ctx.env_path.read_text(encoding="utf-8")

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "pair"}))
        assert status == 200 and body["chat_id"] == "-100987" and body["chats"]

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "bind", "chat_id": "https://t.me/c/-100987",
                                                              "also_channel": True}))
        assert status == 200
        notify = yaml.safe_load(tg_ctx.config_path.read_text(encoding="utf-8"))["notify"]
        assert "telegram" in notify["channels"]
        assert "TELEGRAM_CHAT_ID=-100987" in tg_ctx.env_path.read_text(encoding="utf-8")

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "test"}))
        assert status == 200 and body["ok"] and body["messages"] == 1

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "verify", "token": "garbage"}))
        assert status == 422 and "BotFather" in body["error"]

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "verify", "token": BAD_TOKEN}))
        assert status == 502 and body["error"] == "Unauthorized"

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "nope"}))
        assert status == 422 and "unknown telegram action" in body["error"]

        status, body = payload(post(tg_ctx, "/api/telegram", {"action": "unbind"}))
        assert status == 200
        assert "TELEGRAM_CHAT_ID" not in tg_ctx.env_path.read_text(encoding="utf-8")
        assert payload(get(tg_ctx, "/api/telegram"))[1]["chat_id"] in (None, "")

    def test_digest_job_wires_the_pool(self, tg_ctx):
        from db.session import get_session

        session = get_session()
        sample(session, match_score=95.0)
        status, body = payload(post(tg_ctx, "/api/jobs", {"kind": "notify", "force": True}))
        assert status == 202, body
        job = tg_ctx.runner.get(body["id"])
        assert job.wait(30) is True
        assert job.status == "ok", job.tail(0)
        result = job.tail(0)["result"]
        assert result["pool"] == 1 and result["channels"] == ["console"]
        assert "Commonwealth" in result["preview"]

    def test_offline_run_job_and_log(self, tg_ctx):
        status, body = payload(post(tg_ctx, "/api/jobs", {"kind": "run", "offline": True, "no_notify": True,
                                                          "sources": ["scholars4dev_masters"], "limit": 2}))
        assert status == 202, body
        job = tg_ctx.runner.get(body["id"])
        assert job.wait(90), "offline run did not finish"
        tail = job.tail(0)
        assert tail["status"] == "ok", tail
        assert "sources:" in "\n".join(tail["lines"])
        assert tail["result"]["candidates"] == 0                    # empty cache dir
        assert Path(tg_ctx.settings().runtime["out_dir"], "data.json").exists()
        assert payload(get(tg_ctx, f"/api/jobs/{job.id}"))[1]["lines"][-1].startswith("finished: ok")
        assert payload(get(tg_ctx, "/api/jobs/nope"))[0] == 404
        assert payload(get(tg_ctx, "/api/jobs"))[1]["recent"][0]["id"] == job.id

    def test_job_validation_and_busy(self, tg_ctx):
        assert payload(post(tg_ctx, "/api/jobs", {"kind": "explode"}))[0] == 422
        assert payload(post(tg_ctx, "/api/jobs", {"kind": "test-source", "source": "ghost"}))[0] == 422
        assert payload(post(tg_ctx, "/api/jobs", {"kind": "test-source"}))[0] == 422
        started = threading.Event()
        release = threading.Event()
        runner = Runner()
        blocker = runner.submit("hold", "blocking job", lambda job: (started.set(), release.wait(5), "done")[-1])
        assert started.wait(5)
        with pytest.raises(Busy):
            runner.submit("second", "another", lambda job: None)
        release.set()
        assert blocker.wait(5)
        assert runner.current is None

    def test_busy_returns_409_not_a_traceback(self, tg_ctx, monkeypatch):
        from dashboard.jobs import Busy

        def explode(*a, **k):
            raise Busy("Scrape now is still running (started 10:00:00)")

        monkeypatch.setattr(API, "start_run", explode)
        status, body = payload(post(tg_ctx, "/api/jobs", {"kind": "run"}))
        assert status == 409 and body["busy"] is True and "still running" in body["error"]

    def test_job_list_carries_lines_and_cursor(self, tg_ctx):
        runner = tg_ctx.runner
        job = runner.submit("demo", "demo job", lambda jb: (jb.log("one"), jb.log("two"), "x")[-1])
        assert job.wait(5)
        status, body = payload(get(tg_ctx, "/api/jobs"))
        assert status == 200
        entry = body["recent"][0]
        assert entry["total"] == len(entry["lines"]) >= 2                   # tail(0) = the whole buffer
        assert "one" in entry["lines"] and entry["status"] == "ok"
        head = len(entry["lines"])
        # the query goes in its own argument: `dispatch` receives a parsed path,
        # unlike the socket server which splits it before calling
        status, paged = payload(get(tg_ctx, f"/api/jobs/{job.id}", "after=2"))
        assert paged["lines"] == entry["lines"][2:] and paged["next"] == head   # cursor semantics

    def test_health_job(self, tg_ctx):
        status, body = payload(post(tg_ctx, "/api/jobs", {"kind": "health"}))
        job = tg_ctx.runner.get(body["id"])
        assert job.wait(20) and job.status == "ok"
        assert job.tail(0)["result"]["runs"] == 0
        assert "no runs recorded" in "\n".join(job.tail(0)["lines"])


class TestServerSocket:
    @pytest.fixture
    def server(self, tg_ctx):
        from dashboard import server as SRV

        ctx = tg_ctx
        SRV.Dashboard.ctx = ctx
        SRV.Dashboard.token = "tok-123"
        SRV.Dashboard.require_token = True
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), SRV.Dashboard)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()

    def _get(self, base, path, headers=None):
        req = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_gate_and_page(self, server):
        code, headers, body = self._get(server, "/")
        assert code == 401 and b"Unlock" in body
        assert "token" in body.decode()
        code, headers, body = self._get(server, "/healthz")
        assert code == 200 and body.strip() == b"ok"          # monitors don't need a token
        code, headers, body = self._get(server, "/api/status")
        assert code == 401 and b"unauthorized" in body
        code, headers, body = self._get(server, "/?token=tok-123")
        assert code == 200 and b"Scholar" in body
        assert "radar_token" in headers.get("Set-Cookie", "")
        code, headers, body = self._get(server, "/api/status", {"X-Radar-Token": "tok-123"})
        assert code == 200 and json.loads(body)["counts"]["rows"] >= 0
        code, headers, body = self._get(server, "/api/status", {"Authorization": "Bearer tok-123"})
        assert code == 200
        code, headers, body = self._get(server, "/api/status", {"X-Radar-Token": "tok-12"})
        assert code == 401                                    # same length, wrong value
        code, headers, body = self._get(server, "/nowhere", {"X-Radar-Token": "tok-123"})
        assert code == 404 and b"Back to the dashboard" in body

    def test_post_through_the_socket(self, server):
        body = json.dumps({"groups": {"notify": {"min_score": "72"}}}).encode()
        req = urllib.request.Request(server + "/api/settings", data=body, method="POST",
                                     headers={"X-Radar-Token": "tok-123", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["changed"] is True
        req = urllib.request.Request(server + "/api/status", headers={"X-Radar-Token": "tok-123"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert json.loads(resp.read())["notify"]["min_score"] == 72
        req = urllib.request.Request(server + "/api/raw-config", headers={"X-Radar-Token": "tok-123"})
        assert "min_score: 72" in urllib.request.urlopen(req, timeout=10).read().decode()

    def test_token_file_created_once(self, tg_ctx, monkeypatch):
        from dashboard.server import load_or_create_token

        first = load_or_create_token(tg_ctx.root)
        assert len(first) >= 16 and len(list(tg_ctx.root.glob("data/dashboard.token*"))) == 1
        assert load_or_create_token(tg_ctx.root) == first
        mode = (tg_ctx.root / "data" / "dashboard.token").stat().st_mode & 0o777
        assert mode == 0o600
        (tg_ctx.root / "data" / "dashboard.token").write_text("short\n")
        assert load_or_create_token(tg_ctx.root) != "short"     # too short → rotated


class TestUI:
    """The page is one inline script, so these checks stand in for a browser.

    They are not busywork: an id typo or a string that spans lines inside a
    template literal is otherwise invisible until someone clicks the tab, and a
    syntax error there kills the whole page rather than one widget.
    """

    @staticmethod
    def _script() -> str:
        import re

        return re.search(r"<script>(.*)</script>", PAGE, re.S).group(1)

    def test_inline_script_parses(self, tmp_path):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed; the JS is otherwise unchecked")
        script = tmp_path / "page.js"
        script.write_text(self._script(), encoding="utf-8")
        proc = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr[:1500]

    def test_every_element_the_script_touches_exists(self):
        import re

        script = self._script()
        made = set(re.findall(r"id=\"?([A-Za-z][\w-]*)\"?", PAGE))       # static + template-generated
        touched = set(re.findall(r"\$\('#([\w-]+)'\)", script)) | set(re.findall(r"getElementById\('([\w-]+)'\)", script))
        assert touched - made == set(), f"the script looks up ids that no markup creates: {touched - made}"

    def test_click_handlers_name_defined_functions(self):
        import re

        script = self._script()
        handlers = set(re.findall(r"onclick=\"?(?:event\.stopPropagation\(\);)?(\w+)\(", PAGE))
        defined = set(re.findall(r"(?:function\s+|const\s+)(\w+)\s*[=(]", script))
        assert handlers - defined == set(), f"onclick calls something undefined: {handlers - defined}"

    def test_page_is_self_contained(self):
        for tab in ("overview", "scholarships", "sources", "notify", "settings", "log", "exports"):
            assert f'id="tab-{tab}"' in PAGE, tab
        assert "src=\"http" not in PAGE and "href=\"http" not in PAGE.replace('href="${esc(row.url)}"', "")
        assert "127.0.0.1" not in PAGE and "localhost" not in PAGE
        assert "<style>" in PAGE and "<script>" in PAGE
        for endpoint in ("/api/status", "/api/scholarships", "/api/settings", "/api/telegram", "/api/jobs",
                         "/api/exports", "/api/raw-config", "/api/sources/help"):
            assert endpoint in PAGE, endpoint
        assert "cdn" not in PAGE.lower()

    def test_login_page_mentions_the_token_file(self):
        assert "data/dashboard.token" in LOGIN_HTML and "input" in LOGIN_HTML

