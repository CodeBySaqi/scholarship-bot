"""The two CI helpers must never lie about a run.

A silent green "0 sources" nightly is the worst failure mode this project has:
the tracker just stops telling you about new scholarships and you only notice
months later. These tests pin the fail-loud behaviour.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARISE = ROOT / "ci" / "summarise.py"
WRAP = ROOT / "ci" / "wrap_run.py"


def _run(script: Path, *args: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(script), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
        timeout=60,
    )


def test_good_run_is_green_and_tabular(tmp_path):
    payload = {
        "run_id": "r1",
        "candidates": 30,
        "new": 4,
        "changed": 1,
        "source_details": {
            "a": {"status": "ok", "found": 20, "new": 4, "changed": 1, "error": None},
            "b": {"status": "error", "found": 0, "new": 0, "changed": 0, "error": "HTTP 403"},
        },
        "lifecycle": {"expired": 2},
    }
    f = tmp_path / "run.json"
    f.write_text(json.dumps(payload))
    out, err = tmp_path / "s.md", tmp_path / "o.txt"
    res = _run(SUMMARISE, f, out, err)
    assert res.returncode == 0, res.stderr
    text = out.read_text()
    assert "r1" in text and "HTTP 403" in text and "| `a` | ok | 20 |" in text
    outputs = err.read_text()
    assert "failed_sources=b" in outputs and "all_failed=false" in outputs


def test_all_sources_failed_is_flagged(tmp_path):
    f = tmp_path / "run.json"
    f.write_text(
        json.dumps(
            {
                "run_id": "r2",
                "source_details": {
                    "a": {"status": "error", "found": 0, "error": "dns"},
                    "b": {"status": "error", "found": 0, "error": "403"},
                },
            }
        )
    )
    o = tmp_path / "o.txt"
    res = _run(SUMMARISE, f, tmp_path / "s.md", o)
    assert res.returncode == 0
    assert "all_failed=true" in o.read_text()


def test_empty_run_is_red(tmp_path):
    """`{}` — the CLI died before writing anything. Must not look like success."""
    f = tmp_path / "run.json"
    f.write_text("{}")
    res = _run(SUMMARISE, f)
    assert res.returncode == 1, res.stdout


def test_standalone_run_without_github_paths_does_not_crash(tmp_path):
    f = tmp_path / "run.json"
    f.write_text(json.dumps({"run_id": "r3", "source_details": {}}))
    res = _run(SUMMARISE, f, "None", "None")  # what an unset $GITHUB_* expands to
    assert res.returncode == 0, res.stderr
    assert "r3" in res.stdout


def test_traceback_becomes_fatal_json(tmp_path):
    (tmp_path / "run.json").write_text("Traceback (most recent call last):\n  ValueError: boom\n")
    (tmp_path / "run.log").write_text("the real error line\n")
    res = subprocess.run(
        [sys.executable, str(WRAP), "1"], capture_output=True, text=True, cwd=str(tmp_path), timeout=60
    )
    assert res.returncode == 0
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["fatal"] == "cli.py run exited 1" and "real error" in data["log_tail"]

    res2 = _run(SUMMARISE, tmp_path / "run.json")
    assert res2.returncode == 1 and "crashed" in res2.stdout


def test_valid_json_is_left_alone_but_still_marked(tmp_path):
    payload = {"run_id": "r4", "source_details": {"a": {"status": "error", "found": 0, "error": "403"}}}
    (tmp_path / "run.json").write_text(json.dumps(payload))
    subprocess.run([sys.executable, str(WRAP), "1"], cwd=str(tmp_path), capture_output=True, timeout=60)
    data = json.loads((tmp_path / "run.json").read_text())
    assert data["source_details"] == payload["source_details"] and data["fatal"].endswith("1")
