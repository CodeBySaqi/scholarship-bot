"""Config loading: config.yaml is the source of truth, env vars override it.

Design change vs. the blueprint: profile and source definitions live in one
editable file, so adding/fixing a source is a YAML edit, not a code change.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

import yaml

from . import env

log = logging.getLogger("config")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("SCHOLARSHIP_CONFIG", ROOT / "config.yaml"))
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class Profile:
    """Everything the matcher needs to know about the applicant."""

    gpa: float | None = None
    gpa_scale: str = "4.0"
    ielts: float | None = None
    toefl: int | None = None
    gre_percentile: int | None = None
    degree: str = "Master"  # Bachelor | Master | PhD | Postdoc
    target_degrees: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    exclude_fields: tuple[str, ...] = ()
    countries_preferred: tuple[str, ...] = ()
    countries_blocked: tuple[str, ...] = ()
    home_country: str = ""
    citizenships: tuple[str, ...] = ()
    needs_full_funding: bool = True
    has_research: bool = False
    publications: int = 0
    graduation_year: int | None = None
    work_experience_years: float = 0.0
    age: int | None = None
    max_application_fee_usd: float = 50.0
    budget_usd_per_year: float | None = None
    min_award_usd: float = 0.0
    apply_window_days: int = 240  # ignore deadlines further out than this
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def degrees_we_can_satisfy(self) -> set[str]:
        degrees = {self.degree.title()} if self.degree else set()
        degrees |= {d.title() for d in self.target_degrees}
        # A Master applicant can also apply to "Any" programmes.
        return degrees | {"Any"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        kwargs: dict[str, Any] = {}
        for key in (
            "gpa",
            "ielts",
            "toefl",
            "gre_percentile",
            "graduation_year",
            "age",
            "publications",
        ):
            if data.get(key) is not None:
                kwargs[key] = int(data[key]) if key in {"toefl", "gre_percentile", "graduation_year", "age", "publications"} else float(data[key])
        for key in (
            "gpa_scale",
            "degree",
            "home_country",
            "needs_full_funding",
            "has_research",
        ):
            if key in data:
                kwargs[key] = data[key]
        for key in ("fields", "exclude_fields", "countries_preferred", "countries_blocked", "citizenships", "target_degrees"):
            if data.get(key):
                kwargs[key] = tuple(str(v) for v in data[key])
        for key in ("max_application_fee_usd", "min_award_usd", "budget_usd_per_year"):
            if data.get(key) is not None:
                kwargs[key] = float(data[key])
        if data.get("apply_window_days"):
            kwargs["apply_window_days"] = int(data["apply_window_days"])
        # Anything else that is a declared Profile field is coerced from its
        # annotation. Without this a new config key is silently ignored — which
        # is exactly how `work_experience_years` once defaulted to 0 and every
        # 2-year-experience programme got gated out for the wrong reason.
        hints = get_type_hints(cls)
        for f in fields(cls):
            if f.name in kwargs or f.name == "raw" or f.name not in data or data[f.name] is None:
                continue
            ann = str(hints.get(f.name, ""))
            val = data[f.name]
            try:
                if "int" in ann and "float" not in ann:
                    val = int(float(val))
                elif "float" in ann:
                    val = float(val)
                elif "bool" in ann:
                    val = bool(val) if not isinstance(val, str) else val.strip().lower() in {"1", "true", "yes", "on"}
                elif "tuple" in ann and isinstance(val, (list, tuple)):
                    val = tuple(str(v) for v in val)
            except (TypeError, ValueError):
                log.warning("profile.%s=%r could not be read as %s — ignored", f.name, data[f.name], ann or "the field type")
                continue
            kwargs[f.name] = val
        kwargs["raw"] = data
        return cls(**kwargs)


def profile_from_dict(data: dict[str, Any]) -> Profile:
    """Public shim for `Profile.from_dict` — used by tests and by ad-hoc
    "what if my profile said X?" scoring experiments."""
    return Profile.from_dict(data)


@dataclass
class Settings:
    profile: Profile
    sources: list[dict[str, Any]]
    scoring: dict[str, Any]
    llm: dict[str, Any]
    notify: dict[str, Any]
    runtime: dict[str, Any]
    path: Path

    @property
    def db_url(self) -> str:
        override = env.get("DATABASE_URL")
        if override:
            return override
        rel = self.runtime.get("db_path", "data/scholarships.db")
        p = Path(rel)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p}"

    @property
    def enabled_sources(self) -> list[dict[str, Any]]:
        return [s for s in self.sources if s.get("enabled", True)]


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Env vars are the escape hatch for CI: SCHOLARSHIP__LLM__ENABLED=false."""
    for key, value in os.environ.items():
        if not key.startswith("SCHOLARSHIP__"):
            continue
        parts = [p.lower() for p in key.split("__")[1:]]
        if len(parts) < 2:
            continue
        node = cfg
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        leaf = parts[-1]
        low = value.strip().lower()
        if low in {"true", "false"}:
            node[leaf] = low == "true"
        elif low in {"null", "none"}:
            node[leaf] = None
        elif leaf in {"fields", "countries_preferred", "exclude_fields", "target_degrees"}:
            node[leaf] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            try:
                node[leaf] = int(value)
            except ValueError:
                try:
                    node[leaf] = float(value)
                except ValueError:
                    node[leaf] = value
    return cfg


def load_config(path: str | os.PathLike[str] | None = None) -> Settings:
    env.load_dotenv()
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    data = _apply_env_overrides(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    data.setdefault("profile", {})
    data.setdefault("sources", [])
    data.setdefault("scoring", {})
    data.setdefault("llm", {})
    data.setdefault("notify", {})
    data.setdefault("runtime", {})
    # Secrets never live in config.yaml
    llm = data["llm"]
    llm["api_key"] = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or ""
    llm["base_url"] = llm.get("base_url") or env.get("LLM_BASE_URL") or "https://api.openai.com/v1"
    llm["model"] = llm.get("model") or env.get("LLM_MODEL") or "gpt-4o-mini"
    if env.get("LLM_ENABLED") is not None:
        llm["enabled"] = env.get_bool("LLM_ENABLED", True)
    notify = data["notify"]
    for key, env_key in (
        ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
        ("smtp_host", "SMTP_HOST"),
        ("smtp_port", "SMTP_PORT"),
        ("smtp_user", "SMTP_USER"),
        ("smtp_password", "SMTP_PASSWORD"),
        ("from_email", "FROM_EMAIL"),
        ("to_email", "TO_EMAIL"),
        ("webhook_url", "WEBHOOK_URL"),
        ("digest_secret", "DIGEST_SECRET"),
        ("base_url", "DASHBOARD_URL"),
    ):
        if env.get(env_key):
            notify[key] = env.get(env_key)
    if env.get("SMTP_PORT"):
        notify["smtp_port"] = env.get_int("SMTP_PORT", 587)
    return Settings(
        profile=Profile.from_dict(data.get("profile") or {}),
        sources=data["sources"],
        scoring=data["scoring"],
        llm=llm,
        notify=notify,
        runtime=data["runtime"],
        path=p,
    )


def source_by_id(settings: Settings, source_id: str) -> dict[str, Any] | None:
    for s in settings.sources:
        if s.get("id") == source_id:
            return copy.deepcopy(s)
    return None


def summarise(settings: Settings) -> dict[str, Any]:
    return {
        "config": str(settings.path),
        "db": settings.db_url,
        "profile": {
            "degree": settings.profile.degree,
            "gpa": settings.profile.gpa,
            "ielts": settings.profile.ielts,
            "home_country": settings.profile.home_country,
            "fields": list(settings.profile.fields),
            "preferred_countries": list(settings.profile.countries_preferred),
        },
        "sources": [
            {
                "id": s.get("id"),
                "kind": s.get("kind"),
                "enabled": s.get("enabled", True),
                "priority": s.get("priority", 1),
            }
            for s in settings.sources
        ],
        "llm": {
            "enabled": bool(settings.llm.get("enabled")),
            "has_key": bool(settings.llm.get("api_key")),
            "model": settings.llm.get("model"),
            "base_url": settings.llm.get("base_url"),
        },
        "notify": {
            "channels": settings.notify.get("channels"),
            "threshold": settings.notify.get("min_score", 60),
            "telegram": bool(settings.notify.get("telegram_bot_token")),
            "smtp": bool(settings.notify.get("smtp_host")),
            "webhook": bool(settings.notify.get("webhook_url")),
        },
        "playwright_available": Path(__file__).resolve().parent.parent is not None and _playwright_present(),
    }


def _playwright_present() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except Exception:  # pragma: no cover
        return False
