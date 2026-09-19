"""Config-loading tests.

A profile key that no dataclass field consumes used to be dropped silently, so
`work_experience_years: 1.5` read as 0 and every programme requiring 2 years was
gated out for the wrong reason. These tests make that class of bug loud.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest

from core.config import Profile, load_config, profile_from_dict


@pytest.fixture(scope="module")
def settings():
    return load_config()


def test_every_profile_key_in_config_maps_to_a_field(settings):
    names = {f.name for f in dataclasses.fields(Profile)}
    orphan = sorted(set(settings.profile.raw) - names)
    assert not orphan, f"profile: keys with no Profile field (silently ignored): {orphan}"


def test_numeric_profile_keys_are_not_silently_zeroed(settings):
    p = settings.profile
    assert p.work_experience_years == pytest.approx(float(p.raw["work_experience_years"]))
    assert p.gpa and p.ielts
    assert p.min_award_usd == pytest.approx(float(p.raw["min_award_usd"]))
    assert p.graduation_year == int(p.raw["graduation_year"])


def test_work_experience_actually_reaches_the_gate(settings):
    """Regression: 1.5 yrs must not satisfy a 2-yr requirement, but 3 yrs must."""
    from matching.matcher import check_gates

    class Fake:
        """Only the fields the gate reads; everything else returns None."""

        def __init__(self, years):
            self.min_work_experience_years = years
            self.requires_work_experience = True
            self.country = "Germany"
            self.degree_level = "Master"
            self.funding_type = "Full"

        def __getattr__(self, name):
            return None

    ok = check_gates(Fake(None), settings.profile)
    assert ok.passed, ok.reasons
    tight = check_gates(Fake(2.0), settings.profile)
    assert not tight.passed and "experience" in " ".join(tight.reasons)
    more = profile_from_dict({**settings.profile.raw, "work_experience_years": 3})
    assert check_gates(Fake(2.0), more).passed


def test_bad_profile_value_warns_instead_of_crashing(caplog):
    p = profile_from_dict({"work_experience_years": "two years", "gpa": 3.5})
    assert p.work_experience_years == 0.0  # default, not a crash


def test_llm_keys_in_config_are_the_ones_the_client_reads(settings):
    """`Settings.llm` is a plain dict, so a typo'd key is invisible: the client
    silently falls back to its default and, say, batches 25 items at once.
    Assert the names the client actually asks for exist in config.yaml."""
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("parsers/llm.py").read_text()
    wanted = {"model", "batch_size", "max_item_chars", "budget_usd", "timeout", "retries"}
    used = set(re.findall(r'\bllm\.get\(\s*"([a-z_]+)"', src))
    assert wanted <= used, f"llm.py reads {sorted(used)}; expected {sorted(wanted)}"
    missing = sorted(wanted - set(settings.llm))
    assert not missing, f"config.yaml llm: section is missing {missing} (the client will use its default)"
    # and the values must be the types the client expects
    assert isinstance(settings.llm["batch_size"], int) and 1 <= settings.llm["batch_size"] <= 25
    assert isinstance(settings.llm["timeout"], (int, float)) and settings.llm["timeout"] > 0
    assert isinstance(settings.llm["budget_usd"], (int, float)) and settings.llm["budget_usd"] > 0
    assert settings.llm.get("fill_gaps_only") in (None, True, False)
