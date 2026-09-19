"""Config writing that keeps `config.yaml` human-editable.

The dashboard must not be the only way to read your own config file: a
`yaml.safe_dump` round-trip would flatten the sections, drop every comment and
reorder keys, which turns the file people trust into a machine artifact. So this
module edits the *text*: it finds the line that defines a key path and rewrites
only that line (plus the block it owns, when a list changes).

Rules, in one place:
* comments at the end of a changed line are preserved;
* strings containing `": "` or starting with a YAML-significant character are
  quoted (an unquoted `title: Foo: Bar` is a parse error, and a whole-line
  comment silently becomes data);
* a removed value is written as `null`, never deleted, so the key stays visible
  and documented;
* `write_config` reloads the result through the real loader and rolls the file
  back on any failure — the UI can never brick the config.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

_LIST_KEYS = {
    "fields",
    "exclude_fields",
    "countries_preferred",
    "countries_blocked",
    "target_degrees",
    "citizenships",
    "channels",
    "detail_when_missing",
}

_INT_KEYS = {
    "apply_window_days",
    "graduation_year",
    "age",
    "publications",
    "gre_percentile",
    "pages",
    "limit",
    "priority",
    "max_detail_fetches",
    "max_rows",
    "max_per_source",
    "daily_cap",
    "reschedule_window_hours",
    "batch_size",
    "max_item_chars",
    "max_output_tokens",
    "timeout",
    "retries",
    "min_interval_seconds",
    "cache_fresh_hours",
    "stale_days",
    "report_limit",
    "smtp_port",
    "min_listing_score",
}

_FLOAT_KEYS = {
    "gpa",
    "ielts",
    "toefl",
    "gpa_scale",
    "min_award_usd",
    "max_application_fee_usd",
    "budget_usd_per_year",
    "work_experience_years",
    "min_score",
    "budget_usd",
    "temperature",
    "must_apply",
    "strong",
    "worth_a_look",
    "low",
}

_BOOL_KEYS = {
    "needs_full_funding",
    "has_research",
    "enabled",
    "fill_gaps_only",
    "dry_run",
    "always_summary",
    "fetch_detail",
    "render",
    "dynamic",
    "respect_robots",
    "allow_playwright",
    "skip_unchanged",
    "notify",
    "offline",
    "force",
}

_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.\-]+):(?P<rest>.*)$")
_ITEM_RE = re.compile(r"^(?P<indent> *)-\s+(?P<rest>.*)$")


def format_value(key: str, value: Any) -> str:
    """Render a scalar/short collection the way the hand-written file does.

    Numbers and booleans are never quoted (quoting them changes the type on the
    next read); strings are quoted only when YAML would misread them, which is
    also how a chat id like `-1001234567890` stays a string.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not float(value).is_integer():
            return f"{value!r}" if abs(value) < 1 else f"{value:g}"
        if isinstance(value, float):
            return str(int(value)) if key in _INT_KEYS else f"{value:g}"
        return str(value)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            return "[]"
        return "[" + ", ".join(format_value(key, v) for v in items) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{" + ", ".join(f"{k}: {format_value(str(k), v)}" for k, v in value.items()) + "}"
    return _scalar(str(value))


def _scalar(text: str) -> str:
    """Quote a string only when YAML would otherwise misread it."""
    if text == "" or text.strip() != text:
        return f'"{text}"'
    dangerous_start = ("*", "&", "!", "%", "@", "`", ">", "|", "?", "-", "[", "{", "#", "'", '"', ",")
    if text[0] in dangerous_start:
        return f'"{text}"'
    if ": " in text or text.endswith(":") or " #" in text or text.lower() in {"yes", "no", "on", "off", "true", "false", "null", "none"}:
        return f'"{text}"'
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", text):  # would silently become a number
        return f'"{text}"'
    return text


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_key(lines: list[str], path: list[str], start: int = 0, end: int | None = None,
              base_indent: int = -1) -> tuple[int, int, int] | None:
    """Locate `path` and return (line index, key indent, block end index).

    The block end is the first line at an indent <= the key's indent that is not
    blank, i.e. everything this key owns.
    """
    end = len(lines) if end is None else end
    key = path[0]
    want_indent = base_indent + 2 if base_indent >= 0 else 0
    idx = None
    scan_from = start
    while scan_from < end:
        line = lines[scan_from]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            scan_from += 1
            continue
        indent = _line_indent(line)
        if base_indent >= 0 and indent <= base_indent:
            break
        m = _KEY_RE.match(line.rstrip("\n"))
        if not m:
            scan_from += 1
            continue
        if indent < want_indent:
            scan_from += 1
            continue
        if m.group("key") == key and (base_indent < 0 or indent >= want_indent):
            idx = scan_from
            break
        scan_from += 1
    if idx is None:
        return None
    key_indent = _line_indent(lines[idx])
    block_end = idx + 1
    while block_end < end:
        line = lines[block_end]
        if not line.strip() or line.lstrip().startswith("#"):
            block_end += 1
            continue
        if _line_indent(line) <= key_indent:
            break
        block_end += 1
    if len(path) == 1:
        return idx, key_indent, block_end
    return _find_key(lines, path[1:], idx + 1, block_end, key_indent)


def set_yaml_value(text: str, path: list[str], value: Any) -> str:
    """Set one value inside a YAML document, preserving all other bytes."""
    if not path:
        raise ValueError("empty path")
    lines = text.split("\n")
    found = _find_key(lines, path[:-1] if len(path) > 1 else [])
    if len(path) > 1 and found is None:
        raise KeyError(f"unknown section: {'/'.join(path[:-1])}")
    if len(path) > 1:
        parent_idx, parent_indent, parent_end = found  # type: ignore[misc]
        target = _find_key(lines, [path[-1]], parent_idx + 1, parent_end, parent_indent)
    else:
        target = _find_key(lines, path)
    key = path[-1]
    rendered = format_value(key, value)
    if target is None:
        return _insert_key(lines, path, rendered)
    idx, key_indent, block_end = target
    line = lines[idx]
    comment = ""
    m = re.match(r"^(?P<head>[^#]*?)(?P<comment>\s+#.*)?$", line.rstrip("\n"))
    if m and m.group("comment"):
        comment = m.group("comment")
    lines[idx] = f"{' ' * key_indent}{key}: {rendered}{comment}"
    if isinstance(value, (list, tuple, set, dict)):
        # drop a previous block-style body (it would now be a second, wrong value)
        body = idx + 1
        while body < block_end:
            line = lines[body]
            if not line.strip():
                body += 1
                continue
            if _line_indent(line) <= key_indent:
                break
            body += 1
        del lines[idx + 1 : body]
    return "\n".join(lines)


def _insert_key(lines: list[str], path: list[str], rendered: str) -> str:
    """Add a missing key at the end of its section (keeps the file self-documenting)."""
    key = path[-1]
    if len(path) > 1:
        found = _find_key(lines, path[:-1])
        if found is None:
            raise KeyError(f"unknown section: {'/'.join(path[:-1])}")
        _, indent, block_end = found
        insert_at, child_indent = block_end, indent + 2
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
    else:
        insert_at, child_indent = len(lines), 0
    lines[insert_at:insert_at] = [f"{' ' * child_indent}{key}: {rendered}"]
    return "\n".join(lines)


def _unquote(text: str) -> str:
    raw = text.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('\"', "'"):
        return raw[1:-1]
    return raw



def set_list_item_value(text: str, section: str, match_key: str, match_value: str,
                        key: str, value: Any) -> str:
    """Edit one field inside one item of a list of mappings (`sources:`).

    `sources` is a sequence keyed by `id`, which is not something a dotted path
    can express, so it gets its own lookup: find the item whose `match_key`
    equals `match_value`, then rewrite `key` inside that item only.
    """
    lines = text.split("\n")
    sec = _find_key(lines, [section])
    if sec is None:
        raise KeyError(f"unknown section: {section}")
    sec_idx, sec_indent, sec_end = sec
    item_indent = None
    item_span = None
    i = sec_idx + 1
    while i < sec_end:
        m = _ITEM_RE.match(lines[i].rstrip("\n"))
        if m and _line_indent(lines[i]) == sec_indent + 2:
            start = i
            j = i + 1
            while j < sec_end and not (
                _ITEM_RE.match(lines[j].rstrip("\n")) and _line_indent(lines[j]) == sec_indent + 2
            ):
                j += 1
            # is this the item we want? its first line holds `- key: value`
            first = _ITEM_RE.match(lines[start].rstrip("\n"))
            head = (first.group("rest") if first else "")
            got = re.match(rf"^{re.escape(match_key)}:\s*(.*)$", head.strip())
            if got and _unquote(got.group(1)) == match_value:
                item_indent = sec_indent + 4
                item_span = (start, j)
                break
            i = j
        else:
            i += 1
    if item_span is None:
        raise KeyError(f"no {section} item with {match_key}={match_value!r}")
    rendered = format_value(key, value)
    body = lines[item_span[0] : item_span[1]]
    for k, line in enumerate(body):
        if k == 0:
            continue  # the `- id:` line itself is the match key, never edited here
        m = _KEY_RE.match(line.rstrip("\n"))
        if m and m.group("key") == key and len(m.group("indent")) == item_indent:
            cm = re.match(r"^(?P<head>[^#]*?)(?P<comment>\s+#.*)?$", line.rstrip("\n"))
            indent = len(m.group("indent"))
            body[k] = f"{' ' * indent}{key}: {rendered}" + (cm.group("comment") or "")
            lines[item_span[0] : item_span[1]] = body
            return "\n".join(lines)
    insert_at = item_span[1]
    while insert_at > item_span[0] + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines[insert_at:insert_at] = [f"{' ' * item_indent}{key}: {rendered}"]
    return "\n".join(lines)


def get_yaml_value(text: str, path: list[str], default: Any = None) -> Any:
    """Best-effort read used to show a *current* value without loading config."""
    import yaml

    data = yaml.safe_load(text) or {}
    node: Any = data
    for part in path:
        if isinstance(node, list):
            node = next((x for x in node if isinstance(x, dict) and x.get("id") == part), None)
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
        if node is None:
            return default
    return node


# --------------------------------------------------------------------------- #
# writing files
# --------------------------------------------------------------------------- #
def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # pragma: no cover - only on failure
            os.unlink(tmp)


def apply_patches(text: str, patches: list[tuple[list[str], Any]]) -> str:
    """Apply patches in order; a missing section is an error, not a silent skip."""
    for path, value in patches:
        text = set_yaml_value(text, path, value)
    return text


# --------------------------------------------------------------------------- #
# secrets live in .env, never in config.yaml
# --------------------------------------------------------------------------- #
def apply_env(text: str, values: dict[str, Any]) -> str:
    """Update/append/remove `KEY=value` lines in a .env file, in place."""
    lines = (text or "").split("\n")
    # drop a trailing empty element so appends don't drift
    while lines and lines[-1] == "":
        lines.pop()
    for key, value in values.items():
        idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(rf"^{re.escape(key)}\s*=", stripped):
                idx = i
                break
        if value in (None, "", "__unset__"):
            if idx is not None:
                del lines[idx]
            continue
        rendered = f"{key}={value}" if _needs_no_quotes(str(value)) else f'{key}="{value}"'
        if idx is None:
            lines.append(rendered)
        else:
            lines[idx] = rendered
    return "\n".join(lines) + "\n"


def _needs_no_quotes(value: str) -> bool:
    return not re.search(r"[\s#'\"$`]", value) and value == value.strip()


def env_keys_present(text: str) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if not m or line.strip().startswith("#"):
            continue
        out[m.group(1)] = bool(m.group(2).strip().strip("\"'"))
    return out
