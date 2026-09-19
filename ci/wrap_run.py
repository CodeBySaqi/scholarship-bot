"""Normalise what `cli.py run --json` left behind, for CI.

Called by the workflow as `python ci/wrap_run.py $RC`. If the run crashed,
`run.json` holds a stack trace instead of JSON and every downstream step
( summariser, issue opener, Pages publish ) breaks on it. This turns any
state into valid JSON plus a readable failure, and never raises itself.
"""

from __future__ import annotations

import json
import pathlib
import sys


def main(argv: list[str]) -> int:
    rc = int(argv[1]) if len(argv) > 1 else 0
    run = pathlib.Path("run.json")
    log = pathlib.Path("run.log")
    tail = ""
    if log.exists():
        tail = log.read_text(errors="ignore")[-4000:]

    data: dict | None = None
    if run.exists():
        raw = run.read_text(errors="ignore")
        start = raw.find("{")
        if start >= 0:
            try:
                data = json.loads(raw[start:])
            except json.JSONDecodeError:
                tail = tail or raw[-4000:]
        else:
            tail = tail or raw[-4000:]

    if data is None:
        data = {"fatal": f"cli.py run exited {rc}", "log_tail": tail}
    elif rc != 0:
        data["fatal"] = f"cli.py run exited {rc}"
        if tail:
            data["log_tail"] = tail
    if tail and data.get("fatal"):
        print(tail[-1500:], file=sys.stderr)

    run.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
