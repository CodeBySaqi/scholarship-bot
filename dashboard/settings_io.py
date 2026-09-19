"""What the dashboard may change, and how it writes it back safely.

Two rules shape this file:

1. **No parallel settings store.** The page edits `config.yaml` (and `.env` for
   secrets) — the exact files `cli.py run` reads. Anything saved here is
   therefore in force for the nightly GitHub Action too; there is no
   dashboard-only database of preferences to drift out of sync.
2. **Never corrupt the file.** A YAML *dump* would lose the comments that
   explain every key, so edits are textual (see `patch.py`). A write is only
   kept if `load_config()` accepts the result; otherwise the backup goes back
   and the user gets the parse error instead of a broken next run.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from .patch import (apply_env, env_keys_present, format_value, set_list_item_value, set_yaml_value,
                    write_text_atomic)

# --- the editable surface --------------------------------------------------- #
# `type` drives both the input widget and the coercion of the string a browser
# posts back. `section` is the dotted path in config.yaml.


def _f(key: str, label: str, type_: str, *, section: str, help: str = "", options: tuple[str, ...] | None = None,
       nullable: bool = False) -> dict[str, Any]:
    return {"key": key, "label": label, "type": type_, "section": section, "help": help,
            "options": list(options) if options else None, "nullable": nullable}


FIELDS: dict[str, list[dict[str, Any]]] = {
    "profile": [
        _f("degree", "Degree I'm applying for", "select", section="profile",
           options=("Bachelor", "Master", "PhD", "Postdoc")),
        _f("target_degrees", "Also show these levels", "list", section="profile",
           help="Comma separated. Rows at these levels are treated as a match too."),
        _f("home_country", "Home country", "text", section="profile",
           help="Drives the hard citizenship/eligibility gate."),
        _f("citizenships", "Citizenships held", "list", section="profile", nullable=True),
        _f("gpa", "GPA", "float", section="profile", nullable=True),
        _f("gpa_scale", "GPA scale", "text", section="profile", help="e.g. 4.0 or 100"),
        _f("ielts", "IELTS overall", "float", section="profile", nullable=True,
           help="Blank = no English test yet (rows demanding one are penalised, not killed)."),
        _f("toefl", "TOEFL", "int", section="profile", nullable=True),
        _f("gre_percentile", "GRE percentile", "int", section="profile", nullable=True),
        _f("graduation_year", "Graduation year", "int", section="profile", nullable=True),
        _f("work_experience_years", "Work experience (years)", "float", section="profile"),
        _f("age", "Age", "int", section="profile", nullable=True),
        _f("publications", "Publications", "int", section="profile"),
        _f("has_research", "Has research experience", "bool", section="profile"),
        _f("fields", "Fields I want", "list", section="profile"),
        _f("exclude_fields", "Fields to skip", "list", section="profile", nullable=True),
        _f("countries_preferred", "Preferred countries", "list", section="profile",
           help="Empty = no country preference; the country weight then stops rewarding anything."),
        _f("countries_blocked", "Countries to drop entirely", "list", section="profile", nullable=True),
    ],
    "gates": [
        _f("needs_full_funding", "Only fully funded", "bool", section="profile",
           help="Hard gate: rows without full tuition funding are rejected outright."),
        _f("min_award_usd", "Minimum award (USD/yr)", "int", section="profile",
           help="Below this the money is noted but not fatal. This is the ONLY number the money engine gates on."),
        _f("max_application_fee_usd", "Max application fee (USD)", "float", section="profile"),
        _f("budget_usd_per_year", "What a year would cost me", "int", section="profile", nullable=True,
           help="Used for the 'worth it vs. self-funding' note, never for gating."),
        _f("apply_window_days", "Ignore deadlines further out than (days)", "int", section="profile"),
    ],
    "scoring": [
        *[_f(name, label.replace("_", " ").capitalize(), "int", section="scoring.weights",
             help=f"Contributes up to {weight} of the soft score.")
           for (name, label, weight) in (
               ("country", "Country fit", 22), ("field", "Field fit", 16), ("funding", "Funding type", 18),
               ("amount", "Award size", 12), ("degree", "Degree level", 10), ("competition", "Competition", 8),
               ("english_ready", "English readiness", 6), ("research", "Research fit", 8))],
        _f("must_apply", "Tier: must apply (score ≥)", "int", section="scoring.tiers"),
        _f("strong", "Tier: strong (score ≥)", "int", section="scoring.tiers"),
        _f("worth_a_look", "Tier: worth a look (score ≥)", "int", section="scoring.tiers"),
        _f("low", "Tier: low (score ≥)", "int", section="scoring.tiers"),
    ],
    "notify": [
        _f("channels", "Channels", "multiselect", section="notify",
           options=("console", "telegram", "email", "webhook", "file"),
           help="Nothing lands in Telegram unless 'telegram' is ticked here."),
        _f("min_score", "Only notify at or above (score)", "int", section="notify"),
        _f("max_rows", "Max rows per digest", "int", section="notify"),
        _f("max_per_source", "Max rows per source", "int", section="notify"),
        _f("daily_cap", "Daily cap", "int", section="notify"),
        _f("quiet_hours_start", "Quiet hours from (UTC)", "int", section="notify"),
        _f("quiet_hours_end", "Quiet hours to (UTC)", "int", section="notify"),
        _f("reschedule_window_hours", "Don't re-send the same set within (hours)", "int", section="notify"),
        _f("dry_run", "Dry run (log, never send)", "bool", section="notify"),
        _f("always_summary", "Always send a summary", "bool", section="notify"),
        _f("base_url", "Public base URL for links", "text", section="notify", nullable=True),
    ],
    "llm": [
        _f("enabled", "Use the LLM", "bool", section="llm",
           help="Off = regex only. This is the switch that decides whether anything is spent."),
        _f("fill_gaps_only", "Only for rows with gaps", "bool", section="llm"),
        _f("model", "Model", "text", section="llm"),
        _f("base_url", "Base URL", "text", section="llm",
           help="Any OpenAI-compatible endpoint: Ollama is http://localhost:11434/v1"),
        _f("temperature", "Temperature", "float", section="llm"),
        _f("batch_size", "Batch size", "int", section="llm"),
        _f("budget_usd", "Budget per cycle (USD)", "float", section="llm"),
        _f("timeout", "Request timeout (s)", "int", section="llm"),
        _f("retries", "Retries", "int", section="llm"),
    ],
    "runtime": [
        _f("timeout", "Fetch timeout (s)", "int", section="runtime"),
        _f("min_interval_seconds", "Seconds between requests per source", "float", section="runtime"),
        _f("jitter_seconds", "Jitter (s)", "float", section="runtime"),
        _f("retries", "Fetch retries", "int", section="runtime"),
        _f("respect_robots", "Honour robots.txt", "bool", section="runtime"),
        _f("allow_playwright", "Allow Playwright for JS pages", "bool", section="runtime"),
        _f("cache_fresh_hours", "Cache freshness (hours, 0 = always refetch)", "int", section="runtime"),
        _f("skip_unchanged", "Skip re-parsing unchanged pages", "bool", section="runtime"),
        _f("stale_days", "Mark a row stale after (days without sighting)", "int", section="runtime"),
        _f("report_limit", "Max rows in exports", "int", section="runtime"),
        _f("user_agent", "User-Agent", "text", section="runtime"),
    ],
}

SECTIONS = ("profile", "gates", "scoring", "notify", "llm", "runtime")

SOURCE_FIELDS = [
    _f("enabled", "Enabled", "bool", section="sources"),
    _f("priority", "Priority", "int", section="sources", help="Lower runs first; a daily Action runs the top few."),
    _f("pages", "Listing pages", "int", section="sources"),
    _f("limit", "Max items", "int", section="sources", nullable=True),
    _f("fetch_detail", "Fetch detail pages", "bool", section="sources"),
    _f("detail_when_missing", "Detail only when fields are missing", "bool", section="sources"),
    _f("max_detail_fetches", "Max detail fetches", "int", section="sources"),
    _f("min_interval_seconds", "Seconds between requests", "float", section="sources", nullable=True),
    _f("note", "Note", "text", section="sources", nullable=True),
]

SECRETS = [
    {"key": "TELEGRAM_BOT_TOKEN", "label": "Telegram bot token", "config": "notify.telegram_bot_token",
     "help": "From @BotFather. Stored in .env only — config.yaml is committed, so it never holds a secret."},
    {"key": "TELEGRAM_CHAT_ID", "label": "Telegram chat id", "config": "notify.telegram_chat_id",
     "help": "Negative for groups. Fill it by pairing, not by guessing."},
    {"key": "LLM_API_KEY", "label": "LLM API key", "config": "llm.api_key", "help": "OPENAI_API_KEY also works."},
    {"key": "SMTP_PASSWORD", "label": "SMTP password", "config": "notify.smtp_password"},
    {"key": "SMTP_USER", "label": "SMTP user", "config": "notify.smtp_user"},
    {"key": "WEBHOOK_URL", "label": "Slack/Discord/ntfy webhook", "config": "notify.webhook_url"},
]


def coerce(spec: dict[str, Any], raw: Any) -> Any:
    """Browser strings → the type `config.yaml` expects."""
    type_ = spec["type"]
    if raw is None:
        return None
    if type_ == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}
    if type_ in {"list", "multiselect"}:
        items = raw if isinstance(raw, (list, tuple)) else str(raw).replace(";", ",").split(",")
        return [str(x).strip() for x in items if str(x).strip()]
    if type_ in {"int", "float"}:
        # An empty box must never become 0: `min_score: 0` or
        # `min_award_usd: 0` silently switches a gate off, which is far worse
        # than a validation message.
        if isinstance(raw, str) and not raw.strip():
            if spec["nullable"]:
                return None
            raise ValueError(f"{spec['label']} cannot be empty (empty would disable the setting)")
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} needs a number, got {raw!r}") from None
        if type_ == "int":
            if abs(num - int(num)) > 1e-9:
                raise ValueError(f"{spec['label']} must be a whole number, got {raw!r}")
            return int(num)
        return num
    text = str(raw).strip()
    if not text:
        return None if (spec["nullable"] or type_ == "text") else ""
    if spec.get("options") and text not in spec["options"] and type_ == "select":
        raise ValueError(f"{text!r} is not one of {', '.join(spec['options'])}")
    return text


def spec_for(section: str, key: str) -> dict[str, Any]:
    for spec in FIELDS.get(section, []):
        if spec["key"] == key:
            return spec
    # The page splits `profile` into "Applicant" and "Funding gates" cards, and
    # `scoring` into weights and tiers, but they are one submit. So a key is also
    # found through a sibling card that writes to the same config section.
    wanted = {spec["section"] for spec in FIELDS.get(section, [])}
    if wanted:
        for specs in FIELDS.values():
            for spec in specs:
                if spec["key"] == key and spec["section"] in wanted:
                    return spec
    raise KeyError(f"{section}.{key} is not editable from the dashboard")


def _container(settings, section: str) -> dict[str, Any]:
    """The dict in `Settings` that holds this config section.

    A wrong path here reads as `None` rather than raising, which would show the
    page an empty field for a value the engine is actually using — so the
    section is looked up exactly as `set_yaml_value` would write it.
    """
    if section in {"profile", "gates"}:
        return {}          # read from the Profile dataclass instead
    if section.startswith("scoring."):
        return settings.scoring.get(section.split(".", 1)[1]) or {}
    return getattr(settings, section, {}) or {}


def snapshot(settings) -> dict[str, dict[str, Any]]:
    """Current values for every editable field, in the shape the UI posts back."""
    out: dict[str, dict[str, Any]] = {section: {} for section in FIELDS}
    holders: dict[str, dict[str, Any]] = {}
    for section, specs in FIELDS.items():
        for spec in specs:
            if spec["section"] not in holders:
                holders[spec["section"]] = _container(settings, spec["section"])
            holder = holders[spec["section"]]
            key = spec["key"]
            if section in {"profile", "gates"}:
                value = getattr(settings.profile, key, None)
                if isinstance(value, (tuple, list)):
                    value = list(value)
            elif key == "quiet_hours_start":
                value = (settings.notify.get("quiet_hours") or [0, 7])[0]
            elif key == "quiet_hours_end":
                value = (settings.notify.get("quiet_hours") or [0, 7])[1]
            else:
                value = holder.get(key)
                if isinstance(value, (tuple, set)):
                    value = list(value)
            out[section][key] = value
    return out


def _spec_paths(section: str) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    return [((tuple(spec["section"].split(".")) + (spec["key"],)), spec) for spec in FIELDS[section]]


def build_patches(section: str, values: dict[str, Any]) -> list[tuple[list[str], Any]]:
    """Turn {key: raw value} from the page into patch instructions."""
    patches: list[tuple[list[str], Any]] = []
    for key, raw in (values or {}).items():
        spec = spec_for(section, key)
        if key in {"quiet_hours_start", "quiet_hours_end"}:
            continue
        coerced = coerce(spec, raw)
        path = list(spec["section"].split(".")) + [key]
        patches.append((path, coerced))
    if section == "notify" and any(k in values for k in ("quiet_hours_start", "quiet_hours_end")):
        start = coerce(spec_for("notify", "quiet_hours_start"), values.get("quiet_hours_start", 0))
        end = coerce(spec_for("notify", "quiet_hours_end"), values.get("quiet_hours_end", 7))
        if start == end:
            raise ValueError("quiet hours must span at least one hour")
        if not (0 <= int(start) <= 23 and 0 <= int(end) <= 23):
            raise ValueError("quiet hours are 0-23 (UTC)")
        patches.append((["notify", "quiet_hours"], [int(start), int(end)]))
    return patches


# --- writing --------------------------------------------------------------- #
class ConfigRejected(RuntimeError):
    """Raised when a write does not reload cleanly; the backup has been restored."""


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = path.with_suffix(path.suffix + f".bak-{stamp}")
    copy.write_bytes(path.read_bytes())
    return copy


def reload_settings(config_path: Path):
    from core.config import load_config

    return load_config(config_path)


def write_settings(config_path: Path, *, config_patches: list[tuple[list[str], Any]] | None = None,
                   source_edits: list[dict[str, Any]] | None = None,
                   env_path: Path | None = None, env_values: dict[str, Any] | None = None,
                   source_add: dict[str, Any] | None = None,
                   source_delete: str | None = None) -> dict[str, Any]:
    """Apply everything in one go, then validate by reloading. Roll back on failure.

    Returns {changed, backups, config_text_changed, env_keys}. The reload result
    is not returned on purpose — the caller reloads through `ctx` so its cache is
    refreshed too, and a stale cache here would show the page one thing while the
    engine used another.
    """
    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8")
    new_text = text
    count = 0
    for path, value in (config_patches or []):
        new_text = _set_or_raise(new_text, list(path), value)
        count += 1
    for edit in source_edits or []:
        new_text = set_list_item_value(new_text, "sources", "id", edit["id"], edit["key"], edit["value"])
        count += 1
    if source_add:
        new_text = _append_source(new_text, source_add)
        count += 1
    if source_delete:
        new_text = _remove_source(new_text, source_delete)
        count += 1

    backups: list[str] = []
    touched_config = new_text != text
    if touched_config:
        backups.append(str(backup(config_path)))
        write_text_atomic(config_path, new_text)
        try:
            reload_settings(config_path)
        except Exception as exc:  # noqa: BLE001
            write_text_atomic(config_path, text)
            raise ConfigRejected(f"the new config does not load: {exc}") from exc

    env_keys: dict[str, bool] = {}
    if env_values:
        env_path = Path(env_path or (config_path.parent / ".env"))
        old_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        new_env = apply_env(old_env, env_values)
        if new_env != old_env:
            if env_path.exists():
                backups.append(str(backup(env_path)))
            write_text_atomic(env_path, new_env)
            env_keys = env_keys_present(new_env)
            # The running server already imported os.environ; without this the
            # page would say "saved" while `load_config` still saw the old value.
            for key, value in env_values.items():
                if value in (None, "", "__unset__"):
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)
    if not touched_config and env_keys:
        reload_settings(config_path)
    return {"changed": bool(touched_config or env_keys), "patches": count,
            "config": str(config_path), "config_text_changed": touched_config,
            "backups": backups, "env_keys": env_keys}


def _set_or_raise(text: str, path: list[str], value: Any) -> str:
    return set_yaml_value(text, path, value)


def _append_source(text: str, src: dict[str, Any]) -> str:
    """Add a source by rendering a block under `sources:`.

    Appending text (rather than dumping the whole list) is what keeps every
    existing source's comments and key order intact.
    """
    missing = [k for k in ("id", "kind", "url") if not src.get(k)]
    if missing:
        raise ValueError(f"a source needs {', '.join(missing)}")
    if f"id: {src['id']}" in text:
        raise ValueError(f"a source with id {src['id']!r} already exists")
    order = ["id", "kind", "enabled", "priority", "url", "page_url", "pages", "item", "list_selector",
             "link_selector", "title_selector", "detail_selector", "fetch_detail", "detail_when_missing",
             "max_detail_fetches", "limit", "min_interval_seconds", "note"]
    lines = [f"  - id: {format_value('id', src['id'])}"]
    for key in order[1:]:
        if key not in src or src[key] is None:
            continue
        lines.append(f"    {key}: {format_value(key, src[key])}")
    block = "\n".join(lines)
    marker = "\nsources:\n"
    idx = text.find(marker)
    if idx == -1:
        raise ValueError("this config has no `sources:` list to add to")
    insert_at = idx + len(marker)
    return text[:insert_at] + block + "\n" + text[insert_at:]


def _remove_source(text: str, source_id: str) -> str:
    lines = text.split("\n")
    out, skipping = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- id:"):
            skipping = stripped == f"- id: {source_id}"
        elif stripped.startswith("id:") and line.startswith("  - "):
            skipping = stripped == f"id: {source_id}"
        elif stripped and not line.startswith("    ") and not line.startswith("  -"):
            skipping = False
        if not skipping:
            out.append(line)
    result = "\n".join(out)
    if result == text:
        raise KeyError(f"no source {source_id!r} in this config")
    return result


def secret_status(env_path: Path, settings) -> dict[str, dict[str, Any]]:
    """Where each secret lives and what it looks like — never the value."""
    from core.env import masked

    text = env_path.read_text(encoding="utf-8") if Path(env_path).exists() else ""
    present = env_keys_present(text)
    out: dict[str, dict[str, Any]] = {}
    for spec in SECRETS:
        section, _, key = spec["config"].partition(".")
        current = getattr(settings, section, {}).get(key) if section != "llm" else settings.llm.get(key)
        out[spec["key"]] = {
            "label": spec["label"],
            "in_env": bool(present.get(spec["key"], False)),
            "effective": current not in (None, ""),
            "masked": masked(str(current)) if current else "<unset>",
            "help": spec.get("help", ""),
        }
    return out


def describe_yaml_path(path: list[str]) -> str:
    return ".".join(path)


def editable_sections() -> dict[str, list[dict[str, Any]]]:
    return {k: [dict(s) for s in v] for k, v in FIELDS.items()}


def parse_config_for_preview(path: Path) -> dict[str, Any]:
    """Raw YAML for the 'advanced' view, minus nothing (it's not a secret store)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {k: v for k, v in data.items() if k != "sources"}
