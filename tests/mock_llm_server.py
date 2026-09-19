"""A tiny OpenAI-compatible mock, used to exercise the real LLMClient code path
in tests (and handy if you want to dry-run the bot without spending tokens).

    python tests/mock_llm_server.py --port 8799
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLS: Counter = Counter()
RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        CALLS["chat"] += 1
        user = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "user":
                user = msg.get("content", "")
        try:
            req = json.loads(user)
            items = req.get("items", [])
        except json.JSONDecodeError:
            items = []
        RECEIVED.append({"items": items, "model": body.get("model")})
        results = []
        for item in items:
            text = item.get("page_text", "")
            row = {
                "idx": item.get("idx"),
                "title": item.get("title"),
                "country": None,
                "funding_type": "Full" if re.search(r"tuition", text, re.IGNORECASE) else "Partial",
                "amount_usd": 30000.0,
                "amount_text": "USD 30,000 per year (mock)",
                "degree_level": "Master",
                "field": "Machine Learning",
                "deadline": None,
                "deadline_rolling": True,
                "requires_gre": False,
                "min_ielts": 6.5,
                "eligibility": "mock eligibility text",
                "coverage": ["tuition", "stipend"],
                "eligible_countries": ["Pakistan", "Bangladesh"],
                "notes": "written by the mock server",
            }
            results.append(row)
        payload = {
            "id": "mock",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{"message": {"role": "assistant", "content": json.dumps({"results": results})}, "index": 0, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 120, "total_tokens": len(user) // 4 + 120},
        }
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def serve(port: int = 8799) -> threading.Thread:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    print(f"mock LLM on http://127.0.0.1:{a.port}/v1/chat/completions")
    HTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
