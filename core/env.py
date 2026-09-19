"""Zero-dependency .env loader + small value parsers.

Why not python-dotenv? One less dependency to install in CI and on a $0 VPS.
Precedence: real process environment wins over .env, so GitHub Secrets and
`export FOO=bar` always override the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOTENV_PATH = _ROOT / ".env"


def load_dotenv(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE lines into os.environ. Returns the values that were applied."""
    p = Path(path) if path else DEFAULT_DOTENV_PATH
    applied: dict[str, str] = {}
    if not p.exists():
        return applied
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def get(key: str, default: Any = None) -> str | Any:
    return os.environ.get(key, default)


def get_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def get_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


def get_float(key: str, default: float | None) -> float | None:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def get_json(key: str, default: Any) -> Any:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def masked(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<unset>"
    return f"…{value[-keep:]}" if len(value) > keep else "***"
