"""Matcher tests: the hard gates are the part you must not get wrong."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.models import Scholarship, utcnow
from matching.matcher import (
    check_gates,
    confidence_of,
    evaluate,
    filter_and_score,
    score_soft,
)


def row(**kw):
    base = dict(
        title="Test Scholarship",
        url="https://example.org/x",
        source="t",
        country="Germany",
        degree_level="Master",
        funding_type="Full",
        amount_usd=30000,
        amount_text="€30,000/year",
        field="Any",
        eligible_countries="[]",
        excluded_countries="[]",
        min_gpa=None,
        gpa_scale=None,
        min_ielts=None,
        min_toefl=None,
        max_age=None,
        min_work_experience_years=None,
        max_years_since_degree=None,
        requires_gre=False,
        application_fee_usd=None,
        deadline=utcnow() + timedelta(days=90),
        deadline_rolling=False,
        deadline_estimated=False,
        description="A research-friendly master's programme",
        eligibility="open to all",
        coverage="[]",
        parse_method="label+regex",
        extras={},
        status="active",
        match_score=0.0,
        confidence=0.9,
        notified=False,
        changed=False,
        is_new=True,
        change_type="new",
    )
    base.update(kw)
    return Scholarship(**base)


class TestHardGates:
    def test_excluded_country_is_a_gate_not_a_deduction(self, profile):
        g = check_gates(row(excluded_countries='["Pakistan", "United Kingdom"]'), profile)
        assert g.passed is False and any("excludes" in r for r in g.reasons)

    def test_eligibility_list_without_home_country_blocks(self, profile):
        g = check_gates(row(eligible_countries='["India","Bangladesh"]'), profile)
        assert g.passed is False

    def test_group_lists_expand(self, profile):
        g = check_gates(row(eligible_countries='["Commonwealth"]'), profile)
        assert g.passed, g.reasons

    def test_degree_mismatch_blocks(self, profile):
        assert check_gates(row(degree_level="Bachelor"), profile).passed is False
        assert check_gates(row(degree_level="Bachelor,Master"), profile).passed is True
        assert check_gates(row(degree_level="Any"), profile).passed is True

    def test_gpa_below_minimum_blocks_and_handles_odd_scales(self, profile):
        assert check_gates(row(min_gpa=3.8), profile).passed is False
        assert check_gates(row(min_gpa=3.0), profile).passed is True
        # prose scales must never raise
        assert check_gates(row(min_gpa=2.2, gpa_scale="UK upper second"), profile).passed is True

    def test_ielts_shortfall_blocks(self, profile):
        assert check_gates(row(min_ielts=8.0), profile).passed is False
        assert check_gates(row(min_ielts=6.5), profile).passed is True

    def test_age_limit_blocks(self, profile):
        assert check_gates(row(max_age=22), profile).passed is False
        assert check_gates(row(max_age=35), profile).passed is True

    def test_work_experience_blocks(self, profile):
        assert check_gates(row(min_work_experience_years=3), profile).passed is False

    def test_years_since_degree_blocks(self, profile):
        profile.graduation_year = utcnow().year - 6   # graduated 6 years ago
        assert check_gates(row(max_years_since_degree=2), profile).passed is False
        assert check_gates(row(max_years_since_degree=10), profile).passed is True

    def test_past_deadline_blocks(self, profile):
        g = check_gates(row(deadline=utcnow() - timedelta(days=3)), profile)
        assert g.passed is False and "deadline passed" in g.reasons[0]

    def test_missing_data_warns_instead_of_blocking(self, profile):
        g = check_gates(row(degree_level=None, deadline=None, eligible_countries="[]"), profile)
        assert g.passed is True
        assert any("degree level unknown" in w for w in g.warnings)

    def test_blocked_country_in_profile(self, profile):
        profile.countries_blocked = ("India",)
        assert check_gates(row(country="India"), profile).passed is False


class TestSoftScore:
    def test_preferred_country_full_points(self, profile):
        assert score_soft(row(country="Germany"), profile)[1]["country"] == pytest.approx(22)
        assert score_soft(row(country="Malawi"), profile)[1]["country"] < 10

    def test_any_field_outranks_unrelated(self, profile):
        any_s = score_soft(row(field="Any"), profile)[1]["field"]
        other = score_soft(row(field="Nursing"), profile)[1]["field"]
        assert any_s > other

    def test_specific_field_match(self, profile):
        assert score_soft(row(field="Machine Learning, Data Science"), profile)[1]["field"] == pytest.approx(16)

    def test_full_funding_beats_partial(self, profile):
        assert score_soft(row(funding_type="Full"), profile)[1]["funding"] > score_soft(row(funding_type="Partial"), profile)[1]["funding"]

    def test_excluded_field_zeroes_field_score(self, profile):
        profile.exclude_fields = ("Nursing",)
        assert score_soft(row(field="Nursing"), profile)[1]["field"] == 0.0

    def test_score_is_bounded(self, profile):
        total = score_soft(row(funding_type="Full", amount_usd=999999, field="Machine Learning"), profile)[1]
        assert 0 <= sum(total.values()) <= 100.001


class TestEvaluate:
    def test_ineligible_rows_are_pushed_below_the_floor(self, profile):
        res = evaluate(row(deadline=utcnow() - timedelta(days=5)), profile)
        assert res.tier == "ineligible" and res.score <= 40

    def test_urgent_deadline_gets_a_nudge(self, profile):
        soon = evaluate(row(deadline=utcnow() + timedelta(days=7)), profile)
        later = evaluate(row(deadline=utcnow() + timedelta(days=200)), profile)
        assert soon.days_left <= 7 and later.days_left > 100

    def test_confidence_penalises_sparse_rows(self, profile):
        rich = evaluate(row(), profile)
        sparse = evaluate(
            row(funding_type=None, amount_text=None, amount_usd=None, eligibility=None, field=None, parse_method=""),
            profile,
        )
        assert sparse.confidence < rich.confidence
        assert confidence_of(row()) > confidence_of(row(deadline=None, country=None))

    def test_top_scholarship_ranks_first(self, profile):
        rows = [
            row(title="Perfect Match Award", country="Germany", field="Machine Learning", funding_type="Full", amount_usd=40000),
            row(title="Random Nursing Award", country="Malawi", field="Nursing", funding_type="Partial", amount_usd=1000),
        ]
        kept = filter_and_score(rows, profile, min_score=45)
        assert kept and kept[0].title == "Perfect Match Award"
        assert kept[0].tier in ("must_apply", "strong")

    def test_score_breakdown_is_valid_json_with_reasons(self, profile):
        res = evaluate(row(min_ielts=8.0), profile)
        import json

        blob = json.loads(res.as_json())
        assert set(blob) >= {"score", "tier", "breakdown", "reasons", "warnings"}


class TestUtcDiscipline:
    def test_utcnow_is_naive_utc(self):
        now = utcnow()
        assert now.tzinfo is None
        assert abs((now - datetime.utcnow()).total_seconds()) < 60
