r"""Money parsing: the numbers you actually compare offers on.

Two bug classes live here and both are silent:

1. **Alternation precedence.** `US$|USD|$|€…\s*(\d…)` parses as
   `(US$) | (USD) | ($\s*\d…)`, so the bare symbols match *alone*, the amount is
   dropped, and a later rule re-attaches some other number. The `_CURRENCY`
   alternation must stay inside a group that every branch shares with the amount.
2. **Units.** "992 EUR" on a DAAD page is a *monthly* stipend (~$10.8k/yr).
   Reading it as a one-off understates the single best Germany match by 10×, and
   `min_award_usd` then gates it out.
"""

from __future__ import annotations

import pytest

from parsers.fields import parse_amounts


def _usd(text: str) -> float | None:
    return parse_amounts(text)["amount_usd"]


class TestCurrencyDetection:
    @pytest.mark.parametrize(
        "text,cur",
        [
            ("992 eur", "EUR"),
            # `currency` is always an ISO code, never a symbol: the CSV export and
            # the `min_award_usd` gate both key on it, and "€" / "$" would split
            # the same currency across three different values.
            ("€992", "EUR"),
            ("£1,690 per month", "GBP"),
            ("US$50,000", "USD"),
            ("$992/month", "USD"),
            ("HK$110,000 per year", "HKD"),
            ("AU$20,000", "AUD"),
            ("RMB 3,500", "CNY"),
            ("₹5,00,000 per annum", "INR"),
            ("RM 5,000 per annum", "MYR"),
        ],
    )
    def test_symbol_and_code_forms(self, text, cur):
        assert parse_amounts(text)["currency"] == cur

    def test_bare_dollar_needs_a_number_next_to_it(self):
        # "TAX $500": the $ is real, but "TAX" must not be read as a currency code
        assert _usd("TAX $500") == 500.0
        # "500$" is the same amount written the other way round
        assert _usd("500$") == 500.0

    def test_amount_is_required_after_the_symbol(self):
        """The classic bug: a currency-only alternative matched the symbol alone."""
        assert parse_amounts("€")["amount_usd"] is None
        assert parse_amounts("save $ today")["amount_usd"] is None

    def test_no_currency_means_no_amount(self):
        assert parse_amounts("free tuition only")["amount_usd"] is None
        assert parse_amounts("")["amount_usd"] is None
        assert parse_amounts(None)["amount_usd"] is None

    def test_tiny_usd_fragments_are_noise(self):
        assert parse_amounts("$3 shipping")["amount_usd"] is None

    def test_european_decimal_and_thousand_separators(self):
        assert _usd("€1.200 per month") == pytest.approx(1200 * 1.09 * 10, rel=0.01)
        assert _usd("€1,200 per month") == pytest.approx(1200 * 1.09 * 10, rel=0.01)


class TestPeriodAnnualisation:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("992 eur per month", 992 * 1.09 * 10),
            ("€992 per month", 992 * 1.09 * 10),
            ("$992/month", 992 * 10),
            ("monthly stipend of €992", 992 * 1.09 * 10),
            ("monthly stipend: 992 EUR", 992 * 1.09 * 10),
            ("EUR 992 p.m.", 992 * 1.09 * 10),
            ("992 EUR a month", 992 * 1.09 * 10),
            ("€500 per annum", 500 * 1.09),
            ("$12,000 one-off", 12000.0),
            ("annual award of $12,000", 12000.0),
            ("US$50,000", 50000.0),
        ],
    )
    def test_unit_scaling(self, text, expected):
        assert _usd(text) == pytest.approx(expected, rel=0.01), text

    def test_explicit_duration_beats_the_ten_month_default(self):
        assert _usd("€1,000/month for 24 months") == pytest.approx(1000 * 1.09 * 24, rel=0.02)
        assert _usd("€1,000 per month") == pytest.approx(1000 * 1.09 * 10, rel=0.02)

    def test_a_range_is_priced_by_the_unit_after_its_second_bound(self):
        """'¥143,000–148,000 per month' puts the unit behind the *upper* bound, so
        reading it needs both halves of the range: the pair is priced with that unit
        and the figure we store is the upper bound (what an applicant can count on).
        """
        assert _usd("¥143,000–148,000 per month") == pytest.approx(148000 * 0.0066 * 10, rel=0.01)
        assert _usd("¥143,000 per month") == pytest.approx(143000 * 0.0066 * 10, rel=0.01)
        # a range written with a symbol on both bounds is the same offer
        assert _usd("award of €1,000 - €2,000 per month") == pytest.approx(2000 * 1.09 * 10, rel=0.01)
        # …and the right-hand bound is never scaled a second time
        assert _usd("¥143,000–148,000/month") >= _usd("¥143,000 per month") * 0.99

    def test_adjacent_amounts_do_not_steal_each_others_unit(self):
        """'stipend 862 EUR, paid 10 times a year' — the 'a year' belongs to the
        frequency phrase, not to a second amount; and the 862 must not inherit
        'per month' from a *previous* amount either."""
        assert _usd("€800/month + €200 book allowance") == pytest.approx(800 * 1.09 * 10, rel=0.02)
        assert _usd("US$50,000. Housing: $12,000 per year") == 50000.0

    def test_picks_the_largest_annualised_figure(self):
        assert _usd("tuition of $12,000 plus $2,000/year living allowance") == 12000.0
        assert _usd("$15,000 or $2,000/year, whichever is larger") == 15000.0

    def test_a_faraway_unit_does_not_annualise_an_earlier_amount(self):
        """/month belongs to the second figure; reading it onto $18,000 would print
        a $180k award for a $18k tuition waiver."""
        # 2,000 × 10 = 20,000 legitimately outranks an 18,000 waiver, and the
        # tuition figure itself is NOT annualised (the unit is 34 chars away).
        assert _usd("tuition worth $18,000 plus a living allowance of $2,000/month") == 20000.0
        assert _usd("tuition worth $18,000 per year plus a living allowance") == 18000.0

    def test_no_annualisation_when_disabled(self):
        assert parse_amounts("€1,000 per month", annual=False)["amount_usd"] == pytest.approx(1000 * 1.09, rel=0.02)


class TestMoneyLine:
    def test_derived_annual_figure_is_labelled(self):
        from notifiers import money_line

        class R:
            amount_text = "992 eur per month"
            amount_usd = 10812.8

        out = money_line(R())
        assert "992 eur per month" in out and "≈ US$10,813/yr" in out

    def test_already_annual_text_is_left_alone(self):
        from notifiers import money_line

        class R:
            amount_text = "US$50,000"
            amount_usd = 50000.0

        assert money_line(R()) == "US$50,000"

    def test_missing_data_says_so(self):
        from notifiers import money_line

        class R:
            amount_text = None
            amount_usd = None

        assert money_line(R()) == "amount not stated"


def _numbers(text: str):
    import re

    for m in re.finditer(r"\d[\d,.]{1,11}", text):
        n = m.group(0).replace(",", "")
        if re.fullmatch(r"\d{1,3}\.\d{3}", m.group(0)):
            n = m.group(0).replace(".", "")
        try:
            yield float(n)
        except ValueError:
            continue


def _rate(text: str) -> float:
    from parsers.fields import FX_TO_USD, _CURRENCY
    import re

    hit = re.search(_CURRENCY, text)
    if not hit:
        return 1.0
    cur = hit.group().upper()
    cur = {"HK$": "HKD", "AU$": "AUD", "US$": "USD", "RMB": "CNY", "RM": "MYR"}.get(cur, cur)
    return FX_TO_USD.get(cur, 1.0)
