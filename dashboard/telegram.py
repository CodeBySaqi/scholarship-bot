"""Telegram Bot API, small and dependency-free.

Only the four calls the dashboard needs: `getMe` (does this token work?),
`getUpdates` (who has messaged the bot — how a chat id is discovered without
guessing), `sendMessage` (the test push) and `deleteWebhook`.

The chat id is never guessed or invented: pairing always reads it back from a
real update, which also proves the user has actually pressed Start.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_TIMEOUT = 25.0
TEXT_LIMIT = 4096


class TelegramError(RuntimeError):
    """A failure worth showing the user verbatim (Telegram's own wording)."""


def api_url(method: str, token: str, base: str | None = None) -> str:
    root = (base or "https://api.telegram.org").rstrip("/")
    return f"{root}/bot{token.strip()}/{method}"


def call(method: str, token: str, *, payload: dict[str, Any] | None = None,
         base: str | None = None, timeout: float = DEFAULT_TIMEOUT,
         session: Any | None = None) -> Any:
    """One Bot API call. Returns `result`; raises TelegramError otherwise."""
    token = (token or "").strip()
    if not token:
        raise TelegramError("No bot token set. Paste the token from @BotFather first.")
    http = session or requests
    try:
        resp = http.post(api_url(method, token, base), data=payload or {}, timeout=timeout)
    except requests.RequestException as exc:  # DNS, TLS, timeout, proxy
        raise TelegramError(f"Could not reach Telegram: {exc.__class__.__name__}: {exc}") from exc
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        snippet = (resp.text or "").strip().replace("\n", " ")[:180]
        raise TelegramError(f"Telegram replied {resp.status_code} without JSON: {snippet}") from None
    if not data.get("ok"):
        raise TelegramError(data.get("description") or f"Telegram error {resp.status_code}")
    return data.get("result")


def _secret(settings, token: str | None = None) -> tuple[str, str | None, str | None]:
    """(token, chat id, api base) — `token` overrides what config/.env holds.

    The override exists so a token can be verified *before* it is saved, which is
    the only order that lets the page say "this token works" and then write it.
    """
    notify = dict(getattr(settings, "notify", {}) or {})
    return (str(token or notify.get("telegram_bot_token") or ""),
            (str(notify.get("telegram_chat_id")).strip() if notify.get("telegram_chat_id") else None),
            notify.get("telegram_api_base"))


def get_me(settings, *, session: Any | None = None, token: str | None = None) -> dict[str, Any]:
    token, _chat, base = _secret(settings, token)
    me = call("getMe", token, base=base, session=session) or {}
    return {
        "ok": True,
        "id": me.get("id"),
        "username": me.get("username"),
        "name": me.get("first_name"),
        "can_send": True,
    }


def updates(settings, *, offset: int | None = None, limit: int = 25,
            session: Any | None = None, token: str | None = None) -> list[dict[str, Any]]:
    token, _chat, base = _secret(settings, token)
    payload: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
    if offset:
        payload["offset"] = int(offset)
    # A configured webhook would swallow getUpdates entirely, so clear it first.
    # Its own failure is not worth reporting: the caller asked for updates.
    with contextlib.suppress(TelegramError):
        call("deleteWebhook", token, payload={"drop_pending_updates": False}, base=base, session=session)
    return list(call("getUpdates", token, payload=payload, base=base, session=session) or [])


def chats_from_updates(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unique chats that have talked to the bot, newest first."""
    found: dict[str, dict[str, Any]] = {}
    for upd in raw:
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        found[str(cid)] = {
            "chat_id": str(cid),
            "title": chat.get("title") or chat.get("first_name") or "",
            "type": chat.get("type") or "",
            "username": (chat.get("username") or (msg.get("from") or {}).get("username") or ""),
            "text": (msg.get("text") or "")[:120],
            "update_id": upd.get("update_id"),
        }
    return list(reversed(list(found.values())))


def pair(settings, *, session: Any | None = None, token: str | None = None) -> dict[str, Any]:
    """Find the chat to notify: the most recent message sent to the bot.

    Returns {chat_id, chats, hint}; chat_id is None when nobody has messaged yet,
    which is the case that makes users think the bot is broken, so say it plainly.
    """
    try:
        raw = updates(settings, limit=50, session=session, token=token)
    except TelegramError as exc:
        return {"ok": False, "error": str(exc), "chat_id": None, "chats": []}
    chats = chats_from_updates(raw)
    out = {"ok": True, "chats": chats, "chat_id": chats[0]["chat_id"] if chats else None}
    if not chats:
        out["hint"] = ("The bot has no messages yet. Open Telegram, press START in the chat with "
                       "your bot (or send it any message), then click Pair again.")
    if raw:
        out["next_offset"] = max(int(u.get("update_id") or 0) for u in raw) + 1
    return out


def send(settings, chat_id: str | None, text: str, *, session: Any | None = None,
         disable_preview: bool = True, token: str | None = None) -> dict[str, Any]:
    """Send one message, splitting at Telegram's limit the way the notifier does."""
    token, configured, base = _secret(settings, token)
    chat = (str(chat_id) if chat_id else configured or "").strip()
    if not chat:
        raise TelegramError("No chat id yet. Pair the bot first.")
    chunks = chunk_text(text)
    ids = []
    for part in chunks:
        payload = {"chat_id": chat, "text": part, "disable_web_page_preview": "true" if disable_preview else "false"}
        res = call("sendMessage", token, payload=payload, base=base, session=session) or {}
        ids.append(res.get("message_id"))
    return {"ok": True, "chat_id": chat, "messages": len(chunks), "message_ids": ids}


def chunk_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on blank lines first so a digest never breaks mid-bullet."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for block in text.split("\n\n"):
        while len(block) > limit:  # a single oversized block: cut on a newline if possible
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            if buf.strip():
                parts.append(buf)
                buf = ""
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > limit:
            if buf.strip():
                parts.append(buf)
            buf = block
        else:
            buf = candidate
    if buf.strip():
        parts.append(buf)
    return [p for p in parts if p.strip()]


CHAT_URL_RE = re.compile(r"(?:t(?:elegram)?\.me/)(?:c/)?(?P<id>-?\d{4,20})(?:[/?#]|$)")


def chat_id_from_link(text: str) -> str | None:
    """`https://t.me/c/123456789` or `t.me/-1001234567890` → the numeric id."""
    if not text:
        return None
    raw = text.strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return raw
    match = CHAT_URL_RE.search(raw)
    return match.group("id") if match else None


def looks_like_token(text: str) -> bool:
    """BotFather tokens are `<digits>:<35 urlsafe chars>`."""
    return bool(re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,50}", (text or "").strip()))


def describe_token(token: str) -> str:
    """A safe echo of the token: enough to recognise it, not enough to use it."""
    raw = (token or "").strip()
    if not raw:
        return ""
    head, _, tail = raw.partition(":")
    return f"{head}:{(tail[:2] or '?') + '…' + (tail[-2:] or '?')}"


def token_from_url(text: str) -> str | None:
    """Accept the whole `https://api.telegram.org/bot123:abc/sendMessage` line."""
    raw = (text or "").strip()
    if "t.me/" in raw or "telegra.ph" in raw:
        return None
    if "/bot" in raw:
        path = urlparse(raw if "://" in raw else f"//{raw}").path
        found = re.search(r"/bot(?P<token>\d{6,12}:[A-Za-z0-9_-]{30,50})", path)
        if found:
            return found.group("token")
    return raw if looks_like_token(raw) else None
