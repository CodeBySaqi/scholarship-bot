#!/usr/bin/env python3
"""Turn the run summary JSON into a step summary + workflow outputs.

Kept in its own file so the YAML stays readable and the logic is testable
(`python ci/summarise.py sample.json /dev/stdout /tmp/out.txt`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# sources that have failed N runs in a row are worse than a soft warning
ALL_FAILED_EXIT = 1


def main(argv: list[str]) -> int:
    run_json = Path(argv[1]) if len(argv) > 1 else Path("run.json")
    summary_path = Path(argv[2]) if len(argv) > 2 and argv[2] not in ("", "None") else None
    outputs_path = Path(argv[3]) if len(argv) > 3 and argv[3] not in ("", "None") else None

    data = {}
    if run_json.exists():
        text = run_json.read_text(encoding="utf-8").strip()
        # the CLI may print log lines before the JSON blob
        start = text.find("{")
        if start >= 0:
            try:
                data = json.loads(text[start:])
            except json.JSONDecodeError:
                data = {}

    details = data.get("source_details") or {}
    failed = [k for k, v in details.items() if v.get("status") != "ok"]
    rows = [
        "| source | status | found | new | changed | error |",
        "|---|---|---:|---:|---:|---|",
    ]
    for name, info in sorted(details.items()):
        rows.append(
            f"| `{name}` | {info.get('status')} | {info.get('found', 0)} | {info.get('new', 0)} | "
            f"{info.get('changed', 0)} | {str(info.get('error') or '')[:120]} |"
        )
    table = "\n".join(rows)
    notify = data.get("notified") or {}
    stats = (
        f"- candidates: **{data.get('candidates', 0)}** · new: **{data.get('new', 0)}** · "
        f"changed: **{data.get('changed', 0)}** · duplicates merged: **{data.get('duplicates', 0)}** · rejected: **{data.get('rejected', 0)}**\n"
        f"- detail pages fetched: **{data.get('detail_fetches', 0)}** · LLM items: **{data.get('llm_items', 0)}** · "
        f"LLM calls: **{data.get('llm_calls', 0)}** · LLM cost: **${data.get('llm_cost_usd', 0)}**\n"
        f"- lifecycle sweep: {json.dumps(data.get('lifecycle') or {})}\n"
        f"- notifications: `{json.dumps(notify, default=str)}`\n"
    )

    if data.get("fatal"):
        # the run died before it could write a summary — say so, loudly, and show
        # the tail of stderr instead of an empty table
        body = (
            "# ❌ Scholarship run crashed\n\n"
            f"`{data.get('fatal')}`\n\n```\n{(data.get('log_tail') or 'no log captured')[-2500:]}\n```\n"
        )
    else:
        body = f"# Scholarship run `{data.get('run_id', '?')}`\n\n{stats}\n{table}\n"
    if summary_path:
        try:
            summary_path.write_text(body, encoding="utf-8")
        except OSError:
            print(body)
    print(body)

    def out(key: str, value: str) -> None:
        # CI passes $GITHUB_OUTPUT; a standalone run passes nothing, or the literal
        # string "None" when GitHub expands an unset variable in the command line.
        if outputs_path is None or str(outputs_path) in ("", "None"):
            return
        try:
            with outputs_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{key}={value}\n")
        except OSError:
            pass

    out("failed_sources", ",".join(failed))
    out("failed_table", table.replace("\n", "<br>").replace('"', "'"))
    out("all_failed", "true" if details and len(failed) == len(details) else "false")
    out("publish", "true" if Path("data/out/index.html").exists() else "false")
    out("new_count", str(data.get("new", 0)))

    if not details:
        print("::warning::no source_details in run.json — the run may have crashed before storing anything")
    if data.get("fatal"):
        print(f"::error::{data['fatal']}")
        return 1
    # no run_id at all means the CLI died before storing anything
    return 0 if data.get("run_id") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
