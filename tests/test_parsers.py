"""Parser tests: dates, money, requirements, labels, schema validation."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from parsers.fields import (
    clean_text,
    extract_all,
    parse_amounts,
    parse_application_fee,
    parse_coverage,
    parse_deadline,
    parse_gpa,
    parse_ielts,
)
from parsers.labels import degree_from_label, parse_labels
from parsers.schema import ScholarshipRecord
from tests.conftest import read_fixture


def d(offset_days: int) -> date:
    return (datetime.utcnow() + timedelta(days=offset_days)).date()


# --------------------------------------------------------------------------
class TestDeadline:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Deadline: 6 Oct 2026 (annual)", date(2026, 10, 6)),
            ("Closing date: 12 January, 2027", date(2027, 1, 12)),
            ("Deadline:\n18 Sept 2026 (annual)", date(2026, 9, 18)),
            ("Applications close on 30 November 2026", date(2026, 11, 30)),
            ("deadline 2027-02-28", date(2027, 2, 28)),
            ("Deadline: 15/01/2027", date(2027, 1, 15)),
        ],
    )
    def test_formats(self, text, expected):
        got = parse_deadline(text)["deadline"]
        assert got and got.date() == expected, f"{text!r} -> {got}"

    def test_month_only_is_flagged_estimated(self):
        r = parse_deadline("Deadline: January 2028")
        assert r["deadline"] and r["deadline"].day == 31 and r["deadline_estimated"]

    def test_literal_day_beats_month_end_guess(self):
        """A passed literal deadline must not be hidden behind a still-open guess."""
        r = parse_deadline("Deadline: 18 Sept 2026 (annual)")
        assert r["deadline"].date() == date(2026, 9, 18)

    def test_rolling(self):
        r = parse_deadline("Deadline: rolling — apply until places are filled")
        assert r["deadline_rolling"] is True and r["deadline"] is None

    def test_no_invented_date(self):
        assert parse_deadline("There is no date anywhere here")["deadline"] is None

    def test_bare_day_month_rolls_forward(self):
        r = parse_deadline("Deadline: 25 December")
        assert r["deadline"] and r["deadline"].year in (datetime.utcnow().year, datetime.utcnow().year + 1)
        assert r["deadline"].date() >= datetime.utcnow().date()

    def test_past_dates_are_marked(self):
        assert parse_deadline("Deadline: 31 Dec 2025 (annual)")["past"] is True

    def test_nearest_future_wins_from_a_list(self):
        r = parse_deadline("Deadline: 6 Oct 2026 (annual)  ·  Course starts Sept/Oct 2027")
        assert r["deadline"].date() == date(2026, 10, 6)


class TestMoney:
    def test_usd(self):
        a = parse_amounts("The award is worth US$30,000 per year")
        assert a["amount_usd"] == 30000 and a["currency"] == "USD"

    def test_monthly_stipend_annualised(self):
        a = parse_amounts("a stipend of €1,200 per month")
        assert a["amount_usd"] and 10000 < a["amount_usd"] < 15000

    def test_full_funding_without_a_number(self):
        assert parse_amounts("fully funded programme, no cash figure")["amount_usd"] is None

    def test_huge_european_number(self):
        assert parse_amounts("stipend of 1,20,000 INR per annum")["amount_usd"]

    def test_fee(self):
        assert parse_application_fee("A non-refundable application fee of US$75 applies") == 75.0
        assert parse_application_fee("no application fee required") is None


class TestRequirements:
    def test_gpa_on_4(self):
        val, scale, _ = parse_gpa("minimum CGPA of 3.2 out of 4")
        assert (val, scale) == (3.2, "4")

    def test_gpa_on_5_is_kept_with_its_scale(self):
        val, scale, on4 = parse_gpa("CGPA 3.6/4.0")
        assert val == 3.6 and on4 == pytest.approx(3.6)
        val5, scale5, on45 = parse_gpa("minimum cgpa 4.0 on 5")
        assert val5 == 4.0 and float(scale5) == 5.0 and on45 < 4.0

    def test_ielts_and_toefl(self):
        assert parse_ielts("IELTS overall band score of 6.5 (TOEFL iBT 90)")[0] == 6.5
        assert parse_ielts("IELTS overall band score of 6.5 (TOEFL iBT 90)")[1] == 90
        assert parse_ielts("no language test needed")[0] is None

    def test_coverage_keywords(self):
        cov = parse_coverage("covers tuition, a monthly stipend, health insurance and return airfare")
        assert {"tuition", "stipend", "health_insurance", "airfare"} <= set(cov)

    def test_degree_label(self):
        assert degree_from_label("Masters Degree") == "Master"
        assert degree_from_label("Master / PhD") == "Master,PhD"
        assert degree_from_label("Open to all levels") == "Any"


class TestCleanText:
    def test_removes_scripts_and_keeps_lines(self):
        html = "<div><script>var x=1;</script><p>Deadline: 1 Jan 2027</p><p>Study in: Fiji</p></div>"
        out = clean_text(html)
        assert "Deadline: 1 Jan 2027" in out and "Study in: Fiji" in out and "var x" not in out

    def test_inline_tags_do_not_split_labelled_lines(self):
        html = "<p>Deadline: <span>6 Oct</span> 2026 (annual)</p>"
        assert "Deadline: 6 Oct 2026 (annual)" in clean_text(html)


class TestLabels:
    def test_labelled_blocks(self):
        labels = parse_labels(clean_text(read_fixture("scholars4dev_listing.html")))
        assert labels.get("deadline") == "6 Oct 2026 (annual)"
        assert labels.get("country") == "UK"

    def test_label_does_not_bleed_into_the_next_one(self):
        labels = parse_labels(clean_text(read_fixture("scholars4dev_detail.html")))
        assert labels.get("country") == "Belgium"
        assert "Brief description" not in (labels.get("country") or "")


class TestExtractAll:
    def test_listing_only_row(self):
        ex = extract_all(
            clean_text(read_fixture("scholars4dev_listing.html")),
            title="British Chevening Scholarships in UK for International Students",
        )
        assert ex.deadline and ex.deadline.date() == date(2026, 10, 6)
        assert ex.degree_level == "Master"
        assert ex.country == "United Kingdom"

    def test_detail_page_fills_funding(self):
        ex = extract_all(clean_text(read_fixture("scholars4dev_detail.html")), title="ARES Scholarships in Belgium for Developing Countries")
        assert ex.funding_type == "Full"
        assert "accommodation" in ex.coverage
        assert ex.deadline.date() == date(2026, 9, 18)

    def test_completeness_is_a_real_number(self):
        ex = extract_all("Deadline: 6 Oct 2026\nStudy in: UK\nMasters Degree", title="X")
        assert 0 < ex.completeness <= 1

    def test_generic_text_yields_no_fabricated_country(self):
        ex = extract_all("Some page about studying abroad with no structured data at all.", title="Mystery Award")
        assert ex.eligible_countries == []
        assert ex.min_gpa is None


class TestSchema:
    def test_deadline_must_be_a_real_date(self):
        with pytest.raises(ValidationError):
            ScholarshipRecord.model_validate({"title": "Award X", "deadline": "sometime next year"})

    def test_feb_31_rejected(self):
        with pytest.raises(ValidationError):
            ScholarshipRecord.model_validate({"title": "Award X", "deadline": "2027-02-31"})

    def test_enum_normalisation(self):
        rec = ScholarshipRecord.model_validate({"title": "Award X", "degree_level": "MSc / MPhil", "funding_type": "fully funded"})
        assert rec.degree_level == "Master" and rec.funding_type == "Full"

    def test_lists_are_deduped_and_clipped(self):
        rec = ScholarshipRecord.model_validate({"title": "Award X", "eligible_countries": "Pakistan, pakistan, N/A, Bangladesh"})
        assert rec.eligible_countries == ["Pakistan", "Bangladesh"]

    def test_merged_over_only_fills_gaps(self):
        strong = ScholarshipRecord.model_validate({"title": "Award A", "amount_usd": 1000, "country": "Germany"})
        weak = ScholarshipRecord.model_validate({"title": "Award A", "amount_usd": 99, "country": "Spain", "field": "AI"})
        merged = strong.merged_over(weak)
        assert merged.amount_usd == 1000 and merged.country == "Germany" and merged.field == "AI"

    def test_db_fields_are_orm_column_names(self):
        from db.models import Scholarship

        rec = ScholarshipRecord.model_validate({"title": "Test Award in Germany", "deadline": "2027-01-01"})
        fields = rec.db_fields(url="https://x.org/a", source="s")
        bad = set(fields) - {c.name for c in Scholarship.__table__.columns}
        assert not bad, f"unknown columns: {bad}"
        assert isinstance(fields["deadline"], datetime)
