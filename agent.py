"""Optional chat front-end over the same SQLite DB — the "LangChain agent" from
the blueprint, without the dependency.

The nightly run stays deterministic; this is just a query surface. Works with
any OpenAI-compatible endpoint (OpenAI, Groq, OpenRouter, Ollama).

    python agent.py "Which fully funded AI master's in Germany closes before March?"
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import env, load_config
from db.models import Scholarship, init_engine
from db.session import get_session


# --------------------------------------------------------------------------- #
# tools — plain functions, so you can also just import and call them
# --------------------------------------------------------------------------- #
def search(query: str, limit: int = 15) -> list[dict]:
    session = get_session()
    like = f"%{query}%"
    rows = (
        session.query(Scholarship)
        .filter(Scholarship.status == "active")
        .filter(
            Scholarship.title.ilike(like)
            | Scholarship.field.ilike(like)
            | Scholarship.university.ilike(like)
            | Scholarship.country.ilike(like)
        )
        .order_by(Scholarship.match_score.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "title": r.title,
            "country": r.country,
            "degree": r.degree_level,
            "funding": r.funding_type,
            "amount_usd": r.amount_usd,
            "deadline": r.deadline.strftime("%Y-%m-%d") if r.deadline else ("rolling" if r.deadline_rolling else None),
            "score": r.match_score,
            "tier": r.tier,
            "url": r.url,
        }
        for r in rows
    ]


def deadlines_soon(days: int = 30, min_score: float = 40) -> list[dict]:
    from db.models import utcnow

    session = get_session()
    rows = (
        session.query(Scholarship)
        .filter(Scholarship.status == "active", Scholarship.match_score >= min_score)
        .filter(Scholarship.deadline.isnot(None), Scholarship.deadline <= utcnow() + timedelta(days=days))
        .filter(Scholarship.deadline >= utcnow())
        .order_by(Scholarship.deadline.asc())
        .all()
    )
    return [{"title": r.title, "days_left": r.days_left, "score": r.match_score, "url": r.url, "tier": r.tier} for r in rows]


def why(url: str) -> dict:
    """Explain the ranking of one row — the answer to 'why is this on top?'."""
    session = get_session()
    row = session.query(Scholarship).filter(Scholarship.normalized_url == url).first() or session.query(Scholarship).filter(
        Scholarship.url.ilike(f"%{url}%")
    ).first()
    if row is None:
        return {"error": "not found"}
    out = {
        "title": row.title,
        "score": row.match_score,
        "tier": row.tier,
        "confidence": row.confidence,
        "quality_flags": (row.quality_flags or "").split(","),
    }
    for key in ("score_breakdown", "gate_reasons"):
        raw = getattr(row, key)
        if raw:
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw
    return out


TOOLS = {
    "search": {
        "fn": search,
        "schema": {
            "name": "search",
            "description": "Search tracked scholarships by keyword (country, field, university, title).",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
        },
    },
    "deadlines_soon": {
        "fn": deadlines_soon,
        "schema": {
            "name": "deadlines_soon",
            "description": "Scholarships whose deadline is within N days, best matches first.",
            "parameters": {"type": "object", "properties": {"days": {"type": "integer"}, "min_score": {"type": "number"}}},
        },
    },
    "why": {
        "fn": why,
        "schema": {
            "name": "why",
            "description": "Explain a row's score, tier, gate reasons and data-quality flags.",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        },
    },
}

SYSTEM = (
    "You answer questions about the user's tracked scholarship list using the tools. "
    "Never invent a scholarship, deadline or amount: if the tools return nothing, say so and "
    "suggest running `python cli.py run`. Quote deadlines exactly as returned. "
    "Answer in plain text, max 200 words, and end with the URLs of what you recommend."
)


def ask(question: str, *, model: str | None = None, base_url: str | None = None, api_key: str | None = None, max_turns: int = 4) -> str:
    import urllib.error
    import urllib.request

    settings = load_config()
    init_engine(settings.db_url)
    api_key = api_key or settings.llm.get("api_key") or env.get("OPENAI_API_KEY")
    base_url = (base_url or settings.llm.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = model or settings.llm.get("model") or "gpt-4o-mini"
    if not api_key:
        # Keyless mode: run the tools the question can actually drive, so the
        # script is a useful CLI search even without a model in the loop.
        print("No LLM_API_KEY / OPENAI_API_KEY set — calling the tools directly.\n")
        hits = search(question)
        print(f"### search({question!r}) → {len(hits)} hit(s)")
        print(json.dumps(hits, indent=2, default=str)[:2500])
        if not hits:
            print("  (no keyword match — try a country, a field, or a university name)")
        soon = deadlines_soon(days=60)
        print(f"\n### deadlines_soon(60) → {len(soon)} closing within 60 days")
        print(json.dumps(soon[:8], indent=2, default=str)[:1800])
        if hits:
            print(f"\n### why({hits[0]['url']!r})")
            print(json.dumps(why(hits[0]["url"]), indent=2, default=str)[:1200])
        return ""

    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    tools = [t["schema"] for t in TOOLS.values()]

    def post(body: dict) -> dict:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))

    for _ in range(max_turns):
        data = post({"model": model, "messages": messages, "tools": tools, "temperature": 0.1})
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls")
        if not calls:
            return msg.get("content") or ""
        messages.append(msg)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = TOOLS[name]["fn"](**args)
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})
    return "(stopped after too many tool rounds)"


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Which full-funding matches close in the next 60 days?"
    print(ask(q))
