from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Profile, Settings, load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def today() -> date:
    return date.today()


@pytest.fixture
def profile() -> Profile:
    return Profile(
        gpa=3.5,
        ielts=7.5,
        gre_percentile=90,
        degree="Master",
        target_degrees=("Master", "PhD"),
        fields=("Artificial Intelligence", "Machine Learning", "Data Science", "Computer Science"),
        countries_preferred=("United Kingdom", "Germany", "Netherlands", "Sweden", "Türkiye", "Japan"),
        home_country="Pakistan",
        needs_full_funding=True,
        has_research=True,
        publications=2,
        work_experience_years=1.5,
        graduation_year=datetime.utcnow().year - 2,
        age=24,
    )


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, settings):
    """Every test gets its own SQLite file and cache dir, bound to the app's
    session factory — no shared state, no leaked rows between tests."""
    from db.models import init_engine

    settings.runtime["db_path"] = str(tmp_path / "test.db")
    settings.runtime["cache_dir"] = str(tmp_path / "cache")
    settings.runtime["out_dir"] = str(tmp_path / "out")
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    init_engine(f"sqlite:///{tmp_path/'test.db'}", force=True)
    yield settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """The real config.yaml, but pointed at a throwaway DB and with notifications
    switched off — so tests exercise production settings, not a parallel copy."""
    cfg = load_config(ROOT / "config.yaml")
    out = tmp_path
    cfg.runtime = dict(cfg.runtime)
    cfg.notify = dict(cfg.notify)
    cfg.notify.update({"channels": ["console"], "dry_run": True})
    cfg.llm = dict(cfg.llm)
    cfg.llm.update({"enabled": False, "api_key": ""})
    return cfg


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
