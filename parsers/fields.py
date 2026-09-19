"""Deterministic field extraction. Runs BEFORE any LLM call.

The blueprint spent ~6000 tokens of GPT input per scholarship to re-derive
things that are printed in the page as `Deadline: 6 Oct 2026 (annual)`.
Here, a regex pass extracts 90% of the fields for free; the LLM is only used
to fill the gaps, and it never overwrites a value we read verbatim.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_NAMES = "|".join(MONTHS)

# Static, ~monthly-averaged rates. Good enough for sorting; shown as estimate.
FX_TO_USD = {
    'USD': 1.0, 'US$': 1.0, '$': 1.0, 'EUR': 1.09,
    '€': 1.09, 'GBP': 1.27, '£': 1.27, 'CAD': 0.73,
    'AUD': 0.66, 'NZD': 0.6, 'JPY': 0.0066, '¥': 0.0066,
    'CNY': 0.14, 'RMB': 0.14, 'RMB¥': 0.14, '￥': 0.14,
    'CHF': 1.12, 'SEK': 0.095, 'NOK': 0.094, 'DKK': 0.146,
    'INR': 0.012, '₹': 0.012, 'PKR': 0.0036, 'TRY': 0.029,
    '₺': 0.029, 'ZAR': 0.055, 'SGD': 0.74, 'HKD': 0.13,
    'KRW': 0.00072, '₩': 0.00072, 'RUB': 0.011, '₽': 0.011,
    'PLN': 0.25, 'HUF': 0.0027, 'CZK': 0.043, 'BRL': 0.18,
    'R$': 0.18, 'MXN': 0.055, 'SAR': 0.27, '﷼': 0.27,
    'AED': 0.27, 'QAR': 0.27, 'ILS': 0.27, 'TWD': 0.031,
    'MYR': 0.21, 'RM': 0.21, 'IDR': 6.2e-05, 'PHP': 0.017,
    'VND': 4e-05, 'NGN': 0.00065, '₦': 0.00065, 'KES': 0.0078,
    'EGP': 0.02, 'BDT': 0.0083, 'NPR': 0.0075, 'LKR': 0.003,
    'MAD': 0.1, 'TND': 0.32, 'GHS': 0.065, 'UAH': 0.024,
    '₴': 0.024, 'RSD': 0.01, 'BGN': 0.58, 'RON': 0.22,
    'HRK': 0.15, 'BZD': 0.5, 'JMD': 0.0064, 'TTD': 0.148,
    "THB": 0.028,
}
COVERAGE_KEYWORDS = {
    "tuition": (r"\btuition\b", ),
    "stipend": (r"\b(stipend|living (allowance|expense)s?|maintenance allowance|monthly allowance|pocket money|"
                 r"board and lodging|expenditure account)\b",),
    "airfare": (r"\b(flight|airfare|air ticket|travel (grant|allowance|costs?)|travelto)\b",),
    "health_insurance": (r"\b(health|medical|sickness|accident)\s*(insurance|cover(age)?|scheme)|khda\b",),
    "visa_fee": (r"\b(visa (fee|cost|charge)|residence permit fee)\b",),
    "accommodation": (r"\b(accommodation|housing|hostel|residence|rent|boarding)\b",),
    "books": (r"\b(books?|study material|literature|conference fee)\b",),
    "language_course": (r"\b(language course|german course|pre[- ]university|foundation year|prep year)\b",),
    "research_budget": (r"\b(research (grant|budget|allowance)|consumables|equipment)\b",),
}

FULL_FUNDING_HINTS = (
    r"fully funded", r"full funding", r"full financial support", r"covers? (the )?(full |all )?tuition",
    r"all expenses", r"complete funding", r"fully[- ]financed", r"full scholarship",
    r"covers? (all|most) (of )?(the )?(costs?|fees?)", r"including (a )?(monthly|living)",
)
PARTIAL_HINTS = (r"partial(ly)? (funded|coverage)", r"tuition (fee )?(waiver|reduction)", r"up to \$", r"one[- ]year tuition")

FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "AI/ML": (r"artificial intelligence", r"machine learning", r"deep learning", r"\bnlp\b",
              r"natural language processing", r"computer vision", r"neural network", r"ai\s*/\s*ml", r"\bai\b"),
    "Data Science": (r"data science", r"data analytics", r"big data", r"\bstatistics\b", r"biostatistics", r"data mining"),
    "Computer Science": (r"computer science", r"\binformatics\b", r"software engineering", r"data engineering",
                         r"computer engineering", r"cyber\s*security", r"robotics", r"human-computer", r"\bcomputing\b"),
    "Engineering": (r"\bengineering\b", r"mechanical", r"electrical", r"electronics", r"materials? science",
                    r"aerospace", r"industrial engineering", r"energy systems", r"civil engineering", r"chemical engineering"),
    "STEM": (r"\bstem\b", r"\bphysics\b", r"\bchemistry\b", r"mathematics", r"biotechnology", r"neuroscience",
             r"\bastronomy\b", r"marine science"),
    "Business": (r"business administration", r"\bmba\b", r"\bfinance\b", r"accounting", r"marketing",
                 r"entrepreneur", r"business management"),
    "Public Policy": (r"public policy", r"international relations", r"political science", r"governance",
                      r"development studies", r"international development"),
    "Health": (r"public health", r"epidemiolog", r"\bnursing\b", r"pharmac", r"global health", r"medical science",
               r"\bmedicine\b"),
    "Environment": (r"environment", r"\bclimate\b", r"sustainab", r"renewable energy", r"\becology\b",
                     r"agriculture", r"water resources|water management"),
    "Education": (r"\beducation\b", r"teaching", r"pedagogy", r"curriculum"),
    "Law": (r"\blaw\b", r"\blegal\b", r"jurisprudence", r"human rights law"),
    "Arts/Humanities": (r"humanities", r"\bhistory\b", r"literature", r"philosophy", r"linguistics",
                        r"\bfine art", r"\bmusic\b", r"media studies", r"archaeolog"),
    "Social Sciences": (r"sociolog", r"anthropolog", r"\beconomics\b", r"psycholog", r"social science",
                        r"communication studies", r"\bsocial policy\b"),
}

DEGREE_PATTERNS = {
    "Bachelor": (r"\bbachelor(?:'s|s)?\b", r"\bundergraduate\b", r"\buba ?laurea\b"),
    "Master": (r"\bmaster(?:'s|s)?\b", r"\bpostgraduate\b", r"\bpost-graduate\b", r"\bmba\b", r"\bpg diploma\b",
               r"\bm\.?sc\b", r"\bm\.?tech\b", r"laurea", r"\bmaster's degree programme\b"),
    "PhD": (r"\bph\.?d\.?\b", r"\bdoctorate\b", r"\bdoctoral\b", r"\bphd\b", r"\bdocteur\b", r"\bresearch scholar\b",
            r"\bpost-?doc\b"),
    "Diploma": (r"\bdiploma\b", r"\bfoundation (?:year|course)\b", r"\bpre-?master\b"),
}
DEGREE_STOPWORDS = (r"\bmaster programme\b",)

_STRIP_BOILERPLATE = (
    r"^\s*(share|print|bookmark|facebook|twitter|whatsapp|linkedin|email|copy ?link|post navigation|older posts?)\b.*$",
    r"^\s*(related posts?|you may also like|leave a (comment|reply)|subscribe|sign ?in)\b.*$",
    r"^\s*(error|cookies?|privacy policy|terms of use)\b.*$",
)


@dataclass
class RawExtraction:
    """Everything we can read deterministically off a page. All values are
    `None`/empty when not found — never guessed."""

    title: str | None = None
    provider: str | None = None
    university: str | None = None
    country: str | None = None
    eligible_countries: list[str] = dc_field(default_factory=list)
    excluded_countries: list[str] = dc_field(default_factory=list)
    degree_level: str | None = None
    field: str | None = None
    funding_type: str | None = None
    coverage: list[str] = dc_field(default_factory=list)
    amount_text: str | None = None
    amount_usd: float | None = None
    currency: str | None = None
    application_fee_usd: float | None = None
    deadline: datetime | None = None
    open_date: datetime | None = None
    deadline_text: str | None = None
    deadline_rolling: bool = False
    deadline_estimated: bool = False
    intake: str | None = None
    round_label: str | None = None
    min_gpa: float | None = None
    gpa_scale: str | None = None
    min_ielts: float | None = None
    min_toefl: int | None = None
    requires_gre: bool = False
    requires_work_experience: bool = False
    min_work_experience_years: float | None = None
    max_age: int | None = None
    max_years_since_degree: int | None = None
    eligibility: str | None = None
    last_updated: datetime | None = None
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # `deadline` may hold a datetime (page parse) or an ISO string (a value
        # handed over from a listing page's pre-parse) — accept both.
        d["deadline"] = _iso(self.deadline)
        d["open_date"] = _iso(self.open_date)
        return d

    @property
    def completeness(self) -> float:
        """0..1 — drives whether we bother calling the LLM at all."""
        key_fields = ("country", "degree_level", "funding_type", "amount_text", "deadline", "field")
        return sum(1 for f in key_fields if getattr(self, f)) / len(key_fields)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text or None


def clean_text(html_or_text: str, *, max_chars: int = 14000) -> str:
    """Collapse a scraped blob into plain, deduped, boilerplate-free lines."""
    if not html_or_text:
        return ""
    text = re.sub(r"(?is)<(script|style|noscript|svg|form)[^>]*>.*?</\1>", " ", html_or_text)
    # block-level tags become line breaks; inline tags are dropped so that a
    # <span> inside a sentence does not split a labelled line in half
    text = re.sub(r"(?i)</?(?:br|/br)\s*/?>", "\n", text)
    text = re.sub(r"(?i)</?(?:p|div|li|tr|h[1-6]|ul|ol|table|section|article|header|footer|td|th|dt|dd|figure)\b[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#8217;", "'")
        .replace("&#8211;", "-").replace("&#8220;", '"').replace("&#8221;", '"')
        .replace("&quot;", '"').replace("&#8216;", "'").replace("&gt;", ">").replace("&lt;", "<")
    )
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 2:
            continue
        if any(re.match(p, line, re.IGNORECASE) for p in _STRIP_BOILERPLATE):
            continue
        if line not in lines[-3:]:
            lines.append(line)
    out = "\n".join(lines)
    return out[:max_chars]


def _search(pattern: str, text: str, flags: int = re.IGNORECASE):
    return re.search(pattern, text, flags)


# --------------------------------------------------------------------------- #
# deadline
# --------------------------------------------------------------------------- #
def parse_deadline(text: str | None, *, today: date | None = None, horizon_days: int = 760) -> dict[str, Any]:
    """Parse a deadline out of free text.

    Never invents a date: `deadline` is null unless the page states one. When a
    stated deadline has already passed, it is returned as-is (the pipeline
    decides whether to roll an annual cycle forward) and flagged via `past`.
    """
    today = today or datetime.utcnow().date()
    out: dict[str, Any] = {"deadline": None, "deadline_text": None, "deadline_estimated": False,
                           "deadline_rolling": False, "round_label": None, "past": False}
    if not text:
        return out

    window = _search(r"([^\n]{0,110}(?:deadline|closing date|apply by|applications? (?:close|deadline)|last date|final date)[^\n]{0,120})", text)
    line = window.group(1).strip(" -:") if window else ""
    haystack = line if _find_dates(line, today=today, horizon_days=horizon_days) else text
    if line:
        out["deadline_text"] = line[:180]
    if re.search(r"rolling|until.{0,20}(?:filled|allocated)|open until|no (?:fixed )?deadline|ongoing|year[- ]round", line or "", re.IGNORECASE):
        out["deadline_rolling"] = True

    cands = _find_dates(haystack, today=today, horizon_days=horizon_days)
    chosen = _choose_deadline(cands)
    if chosen["deadline"]:
        out["deadline"] = chosen["deadline"]
        out["deadline_estimated"] = chosen["estimated"]
        out["past"] = chosen["past"]
    rm = _search(r"round\s*([0-9])|(?:^|[\s(])(annual(?:ly)?)(?:[\s)]|$)|(\b(?:first|second|third|autumn|spring|fall|winter|summer) intake\b)", line or text, re.IGNORECASE)
    if rm:
        tag = next((g for g in rm.groups() if g), None)
        if tag and tag.isdigit():
            tag = ["", "Round 1", "Round 2", "Round 3", "Round 4"][int(tag)]
        if tag:
            out["round_label"] = tag.strip().title()
    return out


MONTHS_ALT = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _find_date(text: str, *, today: date, horizon_days: int) -> tuple[str, datetime, str, bool] | None:
    """Kept for single-shot callers; returns the best candidate."""
    cands = _find_dates(text, today=today, horizon_days=horizon_days)
    return cands[0] if cands else None


def _find_dates(text: str, *, today: date, horizon_days: int) -> list[tuple[str, datetime, str, bool]]:
    """All plausible dates in the text, best-first.

    Listing pages carry several dates ("Deadline: 14 Oct/8 Dec 2026/6 Jan 2027",
    "Course starts August 2027", "Last updated: 07 Sep 2026"), so returning the
    first regex hit is wrong: we want the nearest *future, in-window* date.
    """
    if not text:
        return []
    found: list[tuple[str, datetime, str, bool]] = []

    def push(kind: str, d: date, matched: str, estimated: bool) -> None:
        if d.year < today.year - 5 or d.year > today.year + 8:
            return
        exact = kind in {"exact", "iso"}
        if not exact and not (today - timedelta(days=400) <= d <= today + timedelta(days=horizon_days)):
            return
        val = datetime(d.year, d.month, d.day, 23, 59)
        # a precise day beats a month-end guess for the same month
        if exact:
            for i, f in enumerate(found):
                if f[0] not in {"exact", "iso"} and f[1].year == d.year and f[1].month == d.month and f[1] != val:
                    found[i] = (kind, val, matched[:120], False)
                    return
        if any(f[1] == val for f in found):
            return
        found.append((kind, val, matched[:120], estimated))

    for m in re.finditer(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", text):
        try:
            push("iso", date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0), False)
        except ValueError:
            continue

    pats = [
        rf"\b({MONTHS_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*((?:19|20)\d{{2}})\b",   # Sep 18, 2026
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTHS_ALT})\.?,?\s*((?:19|20)\d{{2}})\b",  # 18 Sept 2026
        rf"\b(\d{{1,2}})\s*[/-]\s*({MONTHS_ALT})\s*[/\-]\s*((?:19|20)?\d{{2}})\b",             # 18-Sep-2026
        r"\b(\d{1,2})[/-](\d{1,2})[/-]((?:19|20)\d{2})\b",                                      # 15/01/2027
    ]
    for pat in pats:
        for m in re.finditer(pat, text, re.IGNORECASE):
            g = list(m.groups())
            try:
                if re.fullmatch(MONTHS_ALT, str(g[0]), re.IGNORECASE):
                    mon_s, day, year_s = g
                elif re.fullmatch(MONTHS_ALT, str(g[1]), re.IGNORECASE):
                    day, mon_s, year_s = g
                else:  # numeric dd/mm/yyyy — assume day-first (common outside the US)
                    day, mon_s, year_s = g[0], None, g[2]
                    month = int(g[1])
                    year = int(year_s)
                    d = date(year, month, min(int(day), 28))
                    push("exact", d, m.group(0), True)
                    continue
                month = MONTHS[mon_s[:3].lower()]
                year = int(year_s)
                if year < 100:
                    year += 2000 if year < 70 else 1900
                day = int(re.sub(r"\D", "", str(day)) or 1)
                day = min(day, _last_day(year, month))
                push("exact", date(year, month, day), m.group(0), False)
            except Exception:
                continue

    # "January 2027" (aggregators often publish month only) → last day, flagged
    for m in re.finditer(rf"\b({MONTHS_ALT})\.?\s+(20\d{{2}})\b", text, re.IGNORECASE):
        month = MONTHS[m.group(1)[:3].lower()]
        year = int(m.group(2))
        if 1 <= month <= 12:
            push("month_year", date(year, month, _last_day(year, month)), m.group(0), True)

    # bare "6 Oct" / "18 Sept" with no year → roll forward to the next occurrence
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS_ALT})\b", text, re.IGNORECASE):
        try:
            day, mon_s = int(m.group(1)), m.group(2)
            month = MONTHS[mon_s[:3].lower()]
            if month == 12 and day > 25:
                continue
            day = min(day, _last_day(today.year, month))
            d = date(today.year, month, day)
            if d < today - timedelta(days=3):
                d = date(today.year + 1, month, day)
            if 0 <= (d - today).days <= horizon_days:
                push("day_month_rollover", d, m.group(0), True)
        except Exception:
            continue

    # A literal day for a month makes the month-end guess for that same month
    # redundant — and dangerous, because the guess looks "still open" while the
    # real deadline has passed.
    exact_months = {(f[1].year, f[1].month) for f in found if f[0] in {"exact", "iso"}}
    found = [f for f in found if not (f[0] == "month_year" and (f[1].year, f[1].month) in exact_months)]

    # Rank: (1) not-yet-passed beats passed — compare full datetimes, because a
    # 23:59 deadline is still open today; (2) a literal date beats a guess;
    # (3) soonest first. Passed dates stay in the list (newest first) so the
    # caller can roll an annual cycle forward instead of dropping the row.
    now = datetime.now()

    def rank(f):
        kind, dt, _matched, _est = f
        passed = 1 if dt < now else 0
        exactness = 0 if kind in {"exact", "iso"} else 1
        return (passed, exactness, dt)

    future = sorted((f for f in found if rank(f)[0] == 0), key=rank)
    past = sorted((f for f in found if rank(f)[0] == 1), key=lambda f: f[1], reverse=True)
    return future + past


def _choose_deadline(cands: list[tuple[str, datetime, str, bool]]) -> dict[str, Any]:
    if not cands:
        return {"deadline": None, "matched": None, "estimated": False}
    kind, dt, matched, est = cands[0]
    return {
        "deadline": dt,
        "matched": matched,
        "estimated": est or kind in {"month_year", "day_month_rollover"},
        "past": dt < datetime.now(),
    }


# --------------------------------------------------------------------------- #
# money
# --------------------------------------------------------------------------- #
_CURRENCY = (
    r"(?:"
    # longer forms first: `AU$` must win over a bare `$`, and the bare `$` carries
    # look-around so `TAX $500` can't be read as a currency-coded amount.
    r"US\$|USD|AU\$|CA\$|HK\$|SG\$|NZ\$|NT\$|BZ\$|JM\$|TT\$|EC\$|SV\$|MX\$|C\$"
    r"|(?:USD|AUD|CAD|HKD|SGD|NZD|TWD|BZD|JMD|TTD|INR|RMB|CNY|GBP|EUR|JPY|CHF|SEK|NOK|DKK|PLN|HUF|CZK|TRY|ZAR|MYR|RM|SGD)"
    r"|(?<![A-Za-z])\$(?![A-Za-z])|€|£|¥|￥|₹|₩|₽|﷼|₺|₴|₦|R$|RM"
    r"|(?:CAD|AUD|NZD|JPY|CHF|SEK|NOK|DKK|INR|PKR|TRY|ZAR|KRW|RUB|SAR|AED|BRL|MXN|EGP|NGN|KES|BDT|IDR|VND|THB|PHP|NPR|LKR|MAD|TND|GHS|UAH|RSD|BGN|RON|HRK)"
    r")"
)
_UNIT = r"months?|mo|years?|yrs?|yr|annum|semesters?|terms?|weeks?|hours?|days?"
# NOTE the `(?: … )` around _CURRENCY in each branch. Without it, `A|B|C\s*amount`
# parses as `(A) | (B) | (C\s*amount)`: the bare symbols match *alone* and the
# amount is never required — which is exactly how "€992 per month" once produced
# a nonsense $1,081 figure (the € matched, "992" was dropped, some other rule
# re-added a number). Alternation has the lowest precedence in a regex.


def _resolve_currency(raw: str, text: str, at: int) -> tuple[str, int]:
    """Normalise a matched currency token to an ISO code, and report how much of
    `text` before `at` it occupies (so the tail can be re-derived when the regex
    had to backtrack over a range operator)."""
    cur = (raw or "").upper().strip()
    cur = {
        "US$": "USD", "$": "USD", "USD$": "USD", "€": "EUR", "£": "GBP",
        "A$": "AUD", "AU$": "AUD", "CA$": "CAD", "C$": "CAD",
        "HK$": "HKD", "SG$": "SGD", "NZ$": "NZD", "NT$": "TWD", "BZ$": "BZD", "JM$": "JMD",
        "TT$": "TTD", "EC$": "USD", "SV$": "USD", "MX$": "MXN", "R$": "BRL", "RMB": "CNY",
        "RM": "MYR", "¥": "JPY", "￥": "CNY", "₹": "INR", "₩": "KRW", "₽": "RUB", "﷼": "SAR",
        "₺": "TRY", "₦": "NGN", "₴": "UAH",
    }.get(cur, cur)
    return cur, at


_MONEY_NUM = r"\d+(?:[.,]\d{2,3})*(?:\.\d{1,2})?(?![.,]\d)"


def _to_number(raw: str) -> float | None:
    """Back-compatible name for :func:`_money_number`."""
    return _money_number(raw)


def _money_number(raw: str) -> float | None:
    """Read "1,200" / "1.200" / "1.234,56" / "5,00,000" / "1,200.50"."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):            # 1.000 / 1.234.567 (EU)
        return float(raw.replace(".", ""))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{1,2}", raw):    # 1.234,56 (EU decimal)
        return float(raw.replace(".", "").replace(",", "."))
    return float(re.sub(r"[.,](?=\d{1,2}$)|,", "", raw)) if re.fullmatch(r"[\d,.]+", raw) else None


_MONTHLY_FACTOR = 10.0   # a stipend pays during term, not over the calendar year

_TAIL_PERIOD_RE = re.compile(
    r"(?:per\s*(?:mo(?:nth)?|month|yr|year|annum|wk|week|sem(?:ester)?|term|day)"
    r"|a\s+month(?!ly)"
    r"|\bp\.m\.?(?=[\s).,;]|$)|\bp\.a\.?(?=[\s).,;]|$)|\bpm(?![a-z])|\bpa(?![a-z])"
    r"|/(?:(?:month|mo|year|yr|week|wk|term|semester|sem|annum|an|day))"
    r"|\b(?:monthly|annually|yearly|weekly|daily)\b)",
    re.IGNORECASE,
)
_PERIOD_FACTOR = {
    "month": _MONTHLY_FACTOR, "annum": 1.0, "year": 1.0, "semester": 2.0, "term": 2.0, "week": 40.0,
}
_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_DURATION_RE = re.compile(
    r"\bfor\s+(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*"
    r"(months?|years?)\b", re.IGNORECASE)
# how far from the number a unit word may sit and still describe it
_MAX_UNIT_GAP = 16
# "€800/month + €200 …": a second *new* amount, as opposed to "143,000–148,000"
# ("–148,000"), which is the other half of our own figure.
_OTHER_AMOUNT_RE = re.compile(r"\b(?:plus|and|or|with|including|excluding|covers|less|minus)\b", re.I)
# "143,000–148,000 per month": the tail holds the other bound, and the unit that
# prices the pair sits behind it.  Anything else (" or €1,300") is a new amount.
_RANGE_LEAD_RE = re.compile(r"^\s*[-–—]\s*")
_RANGE_NUM_RE = re.compile(r"[$€£¥]?\s*" + _MONEY_NUM + r"(?:\s*(?:EUR|USD|GBP|JPY|CNY))?", re.I)
_ADDITIVE_RE = re.compile(
    r"\b(?:plus|and|or|with|including|excluding|covers|less|minus|but|&|additionally)\b|[,;]", re.I)
_NEW_ITEM_RE = re.compile(r"\b(?:plus|and|or|with|including|excluding|covers|less|minus|housing|living|fee|costs)\b", re.I)
_MONTH_AFTER_RE = re.compile(
    # `;` is deliberately absent: "a €7,000 grant; monthly costs" is a new clause.
    r"^(?:\s*[—–,:-]?\s*(?:per|a|/|each|of)\s*month(?!ly)\b"
    r"|\s*/(?:mo|month)\b"
    r"|\s*[,(]?\s*p\.m\.?(?=[\s).,;]|$))",
    re.IGNORECASE,
)
_MONTH_BEFORE_RE = re.compile(r"\b(?:monthly|per\s+month(?:ly)?|a\s+month|pro\s+month)\b", re.IGNORECASE)
_CLAUSE_END_RE = re.compile(r"[.;!\n]")  # not ":": it usually introduces the amount
_GLUE_RE = re.compile(r"(?:\d[\d,.]{1,11}\s*)?(?:[$€£¥]|EUR|USD|GBP|JPY|CNY)\s*\d", re.I)

_MONEY_RE = re.compile(
    # a leading period word: "monthly stipend of €992", "annual award of $12,000"
    r"(?:(?P<pre_unit>monthly(?:\s+stipend)?(?:\s+of)?|per\s+month(?:ly)?(?:\s+of)?|annually|annual(?:\s+award)?(?:\s+of)?|a\s+year\s+of)\s+)?"
    r"(?:(?P<cur_a>" + _CURRENCY + r")\s*(?P<amount_a>" + _MONEY_NUM + r")"
    r"|(?P<amount_b>" + _MONEY_NUM + r")\s*(?P<cur_b>" + _CURRENCY + r"))"
    # The tail never crosses a newline or braces, and it stops dead in front of the
    # *next* amount ("$2,000", "EUR 3,000"): a unit written for that figure can
    # then never be read onto this one ("…plus a living allowance of $2,000/month").
    # A range's second bound is let through by _money_candidates itself, which
    # re-reads the raw text, because there the unit prices the whole range.
    r"(?P<tail>(?:(?!\s*(?:[$€£¥]\s*|(?<![A-Za-z])(?:USD|EUR|GBP|JPY|CNY)\s*)\d)[^\n{}]){0,28})",
    re.IGNORECASE,
)


def _unit_window(tail: str, tail_low: str) -> str:
    """Trim a matched tail to the fragment that can name this amount's period."""
    cut = tail_low.find(" for ")                    # "for 24 months" is a duration
    window = tail if cut < 0 else tail[:cut]
    brk = re.search(r"[;.]\s", window)              # new clause, new subject
    if brk:
        window = window[: brk.start()]
    return window


def _period_from(window: str) -> re.Match | None:
    """A period word in `window`, if it is close enough to be this number's unit."""
    tm = _TAIL_PERIOD_RE.search(window)
    if tm is None:
        return None
    # whatever sits between the number and the word must be a connector or the
    # name of the payment, never an addition: "€992 per month" and "$500 stipend
    # per month" yes, "US$50,000 plus a monthly allowance" no (that allowance is a
    # separate line item and "monthly" is its own unit)
    prefix = window[: tm.start()]
    if _ADDITIVE_RE.search(prefix) or len(prefix) > _MAX_UNIT_GAP:
        return None
    return tm


def _unit_name(match: re.Match | None) -> str:
    """Canonical period name for a matched unit word ("" = nothing usable)."""
    raw = (match.group(0) if match else "").lower().strip(". /")
    if not raw:
        return ""
    if raw in _MONTHISH or "month" in raw:
        return "month"
    if raw in _YEARISH or "annual" in raw or "year" in raw or "annum" in raw:
        return "annum"
    if raw in ("wk", "week", "weeks"):
        return "week"
    if raw in ("sem", "semester", "term", "terms"):
        return "semester"
    return ""


_MONTHISH = {"month", "months", "mo", "monthly", "per month", "permo", "per mo", "p.m.", "p.m", "pm"}
_YEARISH = {"year", "years", "yr", "yrs", "annum", "per year", "per annum", "p.a.", "p.a", "pa",
            "annual", "annually", "yearly", "an"}


def _monthly_context(text: str, num_end: int, amount_start: int) -> bool:
    """Does a word *in front of* the amount make it a monthly figure?

    Anchored on the clause: "monthly stipend of €992" and "a €7,000 grant; monthly
    costs extra" both contain the word, but only the first is inside our clause.
    A lead-in that sits behind a *different* amount in the same clause ("worth
    $18,000 plus a monthly allowance") belongs to that other line item.
    """
    if _MONTH_AFTER_RE.match(text[num_end : num_end + 24]):
        return True
    head = text[max(0, amount_start - 60) : amount_start]
    cut = max((m.end() for m in _CLAUSE_END_RE.finditer(head)), default=0)
    clause = head[cut:]
    hit = None
    for cand in _MONTH_BEFORE_RE.finditer(clause):
        hit = cand
        break
    if hit is None:
        return False
    between = clause[hit.end() :]
    return not re.search(r"(?:[$€£¥]\s*\d|\d[\d,]{2,}\s*(?:EUR|USD|GBP|JPY|CNY|PLN|AUD|CAD))", between, re.I)


def _range_after(tail: str) -> str:
    """The text after the last number of a range ("... per month + flights")."""
    nums = list(re.finditer(_MONEY_NUM, tail))
    return tail[nums[-1].end() :] if nums else tail


def _range_top_number(tail: str) -> float | None:
    """The largest number inside a range tail ("143,000–148,000" -> 148000)."""
    vals = [_money_number(m.group(0)) for m in re.finditer(_MONEY_NUM, tail)]
    vals = [v for v in vals if v]
    return max(vals) if vals else None


def _money_candidates(text: str, *, annual: bool = True) -> list[dict]:
    """Every money expression in `text` with the USD value it is worth alone.

    Kept separate from :func:`parse_amounts` because a range's unit is written
    after its *second* number ("¥143,000–148,000 per month"), so the pair can only
    be priced once both candidates are known.
    """
    out: list[dict] = []
    for m in _MONEY_RE.finditer(text):
        _an = "amount_a" if m.group("amount_a") else "amount_b"
        num = _money_number(m.group(_an))
        if num is None:
            continue
        cur_raw = m.group("cur_a") or m.group("cur_b") or ""
        # the regex can drop the currency when the tail refuses to cross a second
        # number, so recover it from the text in front of / behind the digits
        if not cur_raw:
            near = text[max(0, m.start(_an) - 3) : m.start(_an)]
            cur_raw = near.strip() if re.fullmatch(r"\s*[$€£¥]", near) else ""
        cur, _ = _resolve_currency(cur_raw, text, m.start(_an))
        rate = FX_TO_USD.get(cur, 1.0)
        tail = m.group("tail") or ""
        # re-derive the tail if the lazy group gave up too early
        if not tail.strip():
            tail = text[m.end(_an) if not m.group("cur_b") else m.end("cur_b") :
                        (m.end(_an) if not m.group("cur_b") else m.end("cur_b")) + 30]
        raw = text[m.end(_an) : m.end(_an) + 30]
        # the tail was clipped in front of a new amount; if that amount is only
        # the second half of a range ("€1,000 - €2,000 per month") take it back
        if _RANGE_LEAD_RE.match(raw) and len(raw) > len(tail):
            tail = raw
        tail_low = tail.lower()
        # ---- is the tail still inside a range we started? --------------------
        rng = None
        bound = re.match(r"\s*[-–—]\s*(?:[$€£¥]\s*)?" + _MONEY_NUM + r"(?:\s*(?:EUR|USD|GBP|JPY|CNY))?", tail, re.I)
        if bound:
            rng = bound.end()
        unit = ""
        tm_end = 0   # offset into `tail` where the unit text ends
        factor = 1.0
        if rng is not None:
            # the unit after the *other* bound prices both numbers
            tm = _period_from(tail[rng : rng + 22])
            unit = _unit_name(tm)
            tm_end = rng + (tm.end() if tm else 0)
        else:
            window = _unit_window(tail, tail_low)
            cut = _NEW_ITEM_RE.search(window)
            if cut and re.search(r"(?:[$€£¥]\s*\d|\d[\d,.]*\s*(?:USD|EUR|GBP|JPY|CNY))", window[cut.end() :], re.I):
                window = window[: cut.start()]   # the tail's unit belongs to that amount
            tm = _period_from(window)
            if tm is None:
                nxt = _GLUE_RE.search(window)
                if nxt:
                    window = window[: nxt.start()]
                tm = _period_from(window)
            unit = _unit_name(tm)
            tm_end = tm.end() if tm else 0
            if annual and not unit:
                lead = m.start("cur_a") if m.group("cur_a") else m.start(_an)
                if _monthly_context(text, m.end(_an), lead):
                    unit = "month"
        if annual and unit:
            factor = _PERIOD_FACTOR.get(unit, 1.0)
            dur = _DURATION_RE.search(tail)
            if dur and unit == "month":
                n_txt = dur.group(1).lower()
                n = (int(n_txt) if n_txt.isdigit() else _NUM_WORDS.get(n_txt, 0)) * (
                    12 if dur.group(2).startswith("year") else 1)
                if 1 <= n <= 60:
                    factor = float(n)     # "for 24 months" beats the 10-month default
        if rate == 1.0 and cur in ("USD", "$", "") and num < 50 and not m.group("pre_unit"):
            continue   # "$3" style fragments are noise, not funding
        # What we show next to the derived figure: the amount plus its own unit,
        # cut where the source moves on to another item — the digest prints this,
        # so "€992/month" is what the reader should see, not the whole tail.
        shown = text[m.start() : m.start("tail") + tm_end] if tm_end else m.group(0)
        shown = re.split(
            r"\s*(?:\+|;|\bor\b|\bplus\b|\bdepending\b|\bwhichever\b"
            r"| \((?!per\b|a\b|the\b|monthly\b))",
            shown.strip())[0].strip(" ,;.")
        if shown.count("(") > shown.count(")"):
            shown += ")"
        # the tail is capped at 28 chars, so a word cut in half is an artefact of
        # the clip, not of the page: drop it rather than print "undergraduate schol"
        if text[m.end() : m.end() + 1].isalpha():
            shown = re.sub(r"\s*\S+$", "", shown)
        out.append({"usd": num * rate * factor, "raw_usd": num * rate, "num": num, "cur": cur,
                    "rate": rate, "unit": unit, "factor": factor, "text": (shown or m.group(0).strip())[:180],
                    "span": (m.start(), m.end()), "rng": rng, "tail": tail})
    return out


def parse_amounts(text: str, *, annual: bool = True) -> dict[str, Any]:
    """Best-effort funding value. Picks the largest annualised figure, which
    matches how applicants actually compare offers."""
    if not text:
        return {"amount_text": None, "amount_usd": None, "currency": None}
    cands = _money_candidates(text, annual=annual)
    # A range's unit is written after its second bound, so a candidate whose tail
    # is "–148,000 per month" prices *both* bounds.  Report the upper bound: that
    # is the figure an applicant can actually count on, and it keeps the digest
    # from showing a stipend that is 3 % lower than the one on the page.
    if annual:
        for c in cands:
            if c["rng"] and c["unit"]:
                top = _range_top_number(c["tail"]) or c["num"]
                c["num"] = max(c["num"], top)
                c["usd"] = c["num"] * c["rate"] * c["factor"]
                # a matching amount that is only the range's right bound must not
                # be scaled a second time
                for d in cands:
                    if d is not c and d["num"] > c["num"] and d["span"][0] < c["span"][1] + 40 \
                            and d["span"][0] >= c["span"][0] and _RANGE_LEAD_RE.match(text[c["span"][1] : d["span"][0]]):
                        d["usd"] = d["raw_usd"] if d["raw_usd"] < d["usd"] else d["usd"]
    best = max(((c["usd"], c["text"], c["cur"]) for c in cands), default=None)
    if best is None:
        m = _search(r"(\d[\d,.]{2,12})\s*(USD|EUR|GBP|CAD|AUD|JPY|CNY|TRY|INR)", text, re.IGNORECASE)
        if m:
            num = _money_number(m.group(1))
            if num is not None:
                cur = m.group(2).upper()
                best = (num * FX_TO_USD.get(cur, 1.0), m.group(0), cur)
    if not best:
        return {"amount_text": None, "amount_usd": None, "currency": None}
    return {"amount_text": best[1][:180], "amount_usd": round(float(best[0]), 2), "currency": best[2]}


def parse_application_fee(text: str) -> float | None:
    if not text:
        return None
    m = _search(r"(?:application|registration)\s*(?:fee|charge)[^0-9$€£]{0,25}(US\$|\$|€|£|GBP|EUR)?\s*([\d][\d,.]{0,9})", text, re.IGNORECASE)
    if not m:
        m = _search(r"(US\$|\$|€|£)\s*([\d][\d,.]{0,9})\s*(?:as\s*)?application fee", text, re.IGNORECASE)
    if not m:
        return None
    num = _to_number(m.group(2))
    if num is None or num <= 0:
        return None
    cur = (m.group(1) or "USD").upper()
    cur = {"$": "USD", "€": "EUR", "£": "GBP"}.get(cur, cur)
    return round(num * FX_TO_USD.get(cur, 1.0), 2)


# --------------------------------------------------------------------------- #
# other structured fields
# --------------------------------------------------------------------------- #
def parse_gpa(text: str) -> tuple[float | None, str | None, float | None]:
    """Return (min_gpa, scale, min_gpa_on_4)."""
    if not text:
        return None, None, None
    m = _search(r"(?:cgpa|gpa|g\.p\.a)[^0-9]{0,22}(\d\.\d{1,2}|\d)(?:\s*(?:on|/|out of)\s*(\d(?:\.\d)?))?", text, re.IGNORECASE)
    if not m:
        m = _search(r"minimum\s+(?:of\s+)?(\d\.\d{1,2})\s*cgpa", text, re.IGNORECASE)
        if not m:
            return None, None, None
        return float(m.group(1)), "4.0", float(m.group(1))
    val = float(m.group(1))
    scale = float(m.group(2)) if m.group(2) else (5.0 if val > 4.0 else 4.0)
    on4 = val * (4.0 / scale) if scale else val
    return round(val, 2), (str(scale).rstrip("0").rstrip(".") if scale else None), round(min(on4, 4.0), 2)


def parse_ielts(text: str) -> tuple[float | None, int | None]:
    if not text:
        return None, None
    m = _search(r"ielts[^0-9]{0,32}(\d(?:\.\d)?)", text, re.IGNORECASE)
    ielts = float(m.group(1)) if m and 3.0 <= float(m.group(1)) <= 9.0 else None
    m = _search(r"toefl[^0-9]{0,32}(\d{2,3})", text, re.IGNORECASE)
    toefl = int(m.group(1)) if m and 40 <= int(m.group(1)) <= 120 else None
    if ielts is None and "ielts" not in text.lower() and re.search(r"english (?:proficiency|language)\s*(?:requirement|test)?\s*(?:is )?(?:mandatory|required|needed)", text, re.IGNORECASE):
        ielts = 6.5  # required but band unstated — assume the modal minimum, flagged as estimated
    return ielts, toefl


def parse_degree_levels(text: str, *, head_lines: int = 0) -> str | None:
    if not text:
        return None
    if head_lines:
        text = "\n".join(text.splitlines()[:head_lines])
    hits: list[str] = []
    for level, pats in DEGREE_PATTERNS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in pats):
            hits.append(level)
    if not hits:
        return None
    order = ["Bachelor", "Master", "PhD", "Diploma"]
    ordered = [h for h in order if h in hits]
    if "PhD" in ordered and "Postdoc" not in ordered and re.search(r"post-?doc", text, re.IGNORECASE):
        ordered.append("Postdoc")
    return ",".join(ordered)


def parse_field(text: str, *, title: str | None = None, extra_text: str | None = None) -> str | None:
    """Classify the study field. Conservative: a broad match like "engineering"
    counts once in the title and needs corroboration in the body, so generic
    programmes are reported as "Any" (which the matcher scores correctly)."""
    if not text:
        return None
    hay = text.lower()
    title_hay = (title or "").lower()
    scored: list[tuple[int, str]] = []
    for name, syns in FIELD_SYNONYMS.items():
        score = 0
        for s in syns:
            score += len(re.findall(s, title_hay)) * 4
            if extra_text:
                score += min(len(re.findall(s, extra_text.lower())), 4) * 2
            score += min(len(re.findall(s, hay)), 6)
        if score >= 4:
            scored.append((score, name))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top = [n for s, n in scored if s >= 6][:4]
    return ", ".join(top or [scored[0][1]])


def parse_coverage(text: str) -> list[str]:
    if not text:
        return []
    out = []
    for name, pats in COVERAGE_KEYWORDS.items():
        if any(re.search(p, text, re.IGNORECASE) for p in pats):
            out.append(name)
    return out


def parse_funding_type(text: str, coverage: list[str] | None = None) -> tuple[str | None, bool]:
    """Return (funding_type, estimated)."""
    if not text:
        return None, False
    cov = set(coverage or [])
    broad = len({"tuition", "stipend", "airfare", "accommodation", "health_insurance"} & cov) >= 3
    if any(re.search(p, text, re.IGNORECASE) for p in FULL_FUNDING_HINTS) or broad:
        return "Full", False
    if any(re.search(p, text, re.IGNORECASE) for p in PARTIAL_HINTS):
        return "Partial", False
    if "tuition" in cov and not {"stipend", "airfare"} & cov:
        return "Tuition-only", True
    if cov:
        return "Partial", True
    return None, False


def _only_countries(items: list[str] | None) -> list[str]:
    """Keep a country list only if every entry is a recognisable country or a
    known group name. Aggregators emit prose ("International Students") where a
    list would be; a half-trusted list is worse than no list, because it gates."""
    if not items:
        return []
    from matching.countries import canon, expand_group

    out = []
    for item in items:
        s = str(item).strip(" .;")
        if not s or s.lower().startswith(("international", "students", "applicants", "any ", "all ")):
            return []
        if expand_group(s) is None and not canon(s):
            return []
        out.append(s)
    return out[:60]


def parse_countries(text: str, *, home_country: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Extract the host country and (when present) the eligibility country set.

    Aggregators label these explicitly (`Study in: UK`, `Host Country: …`),
    which is far more reliable than asking an LLM to infer it.
    """
    res: dict[str, Any] = {"country": None, "eligible": [], "excluded": [], "scope": None}
    if not text:
        return res
    for pat in (
        r"study in[:\s]+([^\n|]{2,60})",
        r"(?:host|destination) country[:\s]+([^\n|]{2,60})",
        r"location[:\s]+([^\n|]{2,60})",
        r"scholarships? (?:in|at|for study in)\s+([A-Z][A-Za-z .'-]{2,25})\b",
    ):
        m = _search(pat, text)
        if m:
            candidate = re.split(r"\s{2,}|\|", m.group(1).strip())[0][:70]
            candidate = re.sub(r"\s*(for|intended for|and other)\s+(international|developing|all|non)[-a-z]*.*$", "", candidate, flags=re.IGNORECASE)
            if candidate and len(candidate) > 2:
                res["country"] = candidate
                break
    # open-to list
    m = _search(r"(?:open to|eligible (?:countries?|nations?)|target(?:ed)? group)\s*:?\s*((?:[A-Z][A-Za-z' -]{2,25},\s*){1,12}[A-Z][A-Za-z' -]{2,25})", text)
    if m:
        items = [c.strip() for c in re.split(r",| and ", m.group(1)) if len(c.strip()) > 2]
        items = [c for c in items if re.fullmatch(r"[A-Z][A-Za-z' .-]{2,30}", c) and not c.lower().startswith(("the ", "students", "applicants"))]
        if items:
            res["eligible"] = items[:60]
    m = _search(r"(?:not eligible|not (?:open|available) to|excluded(?: countries)?:?)\s*:?\s*((?:[A-Z][A-Za-z' -]{2,25},\s*){0,8}[A-Z][A-Za-z' -]{2,25})", text)
    if m:
        from matching.countries import canon as _canon

        cands = [c.strip(" .;") for c in re.split(r",| and ", m.group(1)) if len(c.strip(" .;")) > 2]
        # keep only items that really are countries: "Chevening-eligible countries"
        # must not be mistaken for an exclusion list that gates a whole row
        res["excluded"] = [c for c in cands if _canon(c) and re.fullmatch(r"[A-Za-z][A-Za-z' .-]{1,24}", c)][:40]
    if _search(r"(?:developing countries|commonwealth|worldwide|all (?:nationalities|countries)|any nationality)", text, re.IGNORECASE):
        res["scope"] = "worldwide-ish"
    if res["country"] is None:
        # only trust an explicit "… in <Country>" / "… at <Place>" construction in
        # the title; scanning free text for country names produces false positives
        from matching.countries import canon

        head = "\n".join(text.splitlines()[:6])
        m = _search(r"(?:scholarships?|awards?|fellowships?|grant)\s+(?:in|for|at|to)\s+([A-Z][A-Za-z .'-]{2,24})", head)
        if m:
            cand = m.group(1).strip()
            cand = re.sub(r"\s+(for|intended for)\s+.*$", "", cand, flags=re.IGNORECASE)
            res["country"] = canon(cand) or cand
    if res["country"] is None:
        from matching.countries import ALIASES
        from matching.countries import canon as _canon

        probe = " ".join([title or "", *(text.splitlines()[:3])])[:400]
        best = None
        for alias in ALIASES:
            if re.search(rf"(?:^|[^A-Za-z]){re.escape(alias)}(?:[^A-Za-z]|$)", probe) and (best is None or len(alias) > len(best)):
                best = alias
        if best:
            res["country"] = _canon(best)
    return res


def parse_constraints(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not text:
        return out
    m = _search(r"(?:maximum|max\.?|not older than|age (?:limit|cap)|up to)\s*(?:of\s*)?(\d{2})\s*(?:years?\s*old|years?)?(?:\s*as of[^\n]{0,30})?", text, re.IGNORECASE)
    if m and 18 <= int(m.group(1)) <= 55:
        out["max_age"] = int(m.group(1))
    m = _search(r"(\d{1,2})\s*(?:years?)?\s*(?:of\s*)?(?:work|professional|employment|working)\s*experience", text, re.IGNORECASE)
    if not m:
        m = _search(r"(?:work|professional) experience\s*(?:of|min(?:imum)?)?[^0-9]{0,15}(\d{1,2})\s*years?", text, re.IGNORECASE)
    if m:
        out["min_work_experience_years"] = float(m.group(1))
        out["requires_work_experience"] = True
    m = _search(r"(?:within|not more than|in the last|past)\s+(\d{1,2})\s+years", text, re.IGNORECASE)
    if m and _search(r"(?:degree|graduat|bachelor|master|awarded|completed)", text, re.IGNORECASE):
        out["max_years_since_degree"] = int(m.group(1))
    if _search(r"\bgre\b", text, re.IGNORECASE) and _search(r"(gre[^.]{0,60}(required|mandatory|submit|score|needed)|(?:required|mandatory)[^.]{0,40}\bgre\b)", text, re.IGNORECASE):
        out["requires_gre"] = True
    m = _search(r"\b(fall|autumn|spring|summer)\s+((?:19|20)\d{2})\b", text, re.IGNORECASE)
    if m:
        out["intake"] = f"{m.group(1).title()} {m.group(2)}"
    m = _search(r"\b(20\d\d\s*[-–/]\s*(?:20)?\d\d)\b", text)
    if m and "intake" not in out:
        out["intake"] = m.group(1)
    return out


def parse_provider(text: str, title: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"provider": None, "university": None}
    if not text:
        return out
    m = _search(r"(?:host institution(?:s)?|offered by|provided by|sponsor|funded by|institute|organization)[:\s]+([^\n|]{3,80})", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        val = re.sub(r"\s*\|.*$", "", val)[:200]
        out["provider"] = val or None
    # a real institution name: capitalised words + institution keyword, or a quoted name
    m = _search(r"\b((?:[A-Z][A-Za-z&'\-]+\s+){1,5}(?:University|Institute|College|Polytechnic|Academy|School of \w+|Universit\w+))", text)
    if m:
        cand = re.sub(r"\s{2,}", " ", m.group(1)).strip()
        # reject generic phrases such as "Any university", "the university"
        if not re.match(r"^(any|the|every|each|all|this|one)\b", cand, re.IGNORECASE) and len(cand) > 6:
            out["university"] = cand[:200]
    if out["university"] is None and title:
        m = _search(r"at ([A-Z][A-Za-z&' .\-]{3,50})$", title.strip())
        if m:
            out["university"] = m.group(1).strip()[:200]
    if out["provider"] is None:
        out["provider"] = out["university"]
    return out


def extract_all(text: str, *, title: str | None = None, home_country: str | None = None) -> RawExtraction:
    """One-call entry point used by the pipeline.

    Order matters: labelled metadata first (literal, no guessing), whole-page
    regex only as a backfill for fields the page did not label.
    """
    clean = text if "\n" in text else clean_text(text)
    r = RawExtraction(title=title)

    from .labels import apply_labels, parse_labels

    labels = parse_labels(clean)
    vals = apply_labels(labels)
    meta: dict[str, Any] = {}
    for key, value in vals.items():
        if key.startswith("_"):
            meta[key.lstrip("_")] = value
        elif value not in (None, "", [], False):
            setattr(r, key, value)

    # ---- backfill anything the labels did not give us ----
    if r.deadline is None and not r.deadline_rolling:
        dl = parse_deadline(clean)
        r.deadline, r.deadline_text = dl["deadline"], r.deadline_text or dl["deadline_text"]
        r.deadline_rolling = r.deadline_rolling or dl["deadline_rolling"]
        r.deadline_estimated = dl["deadline_estimated"]
        r.round_label = r.round_label or dl["round_label"]

    amt = parse_amounts(clean)
    if r.amount_usd is None and amt["amount_usd"] is not None:
        r.amount_text, r.amount_usd, r.currency = amt["amount_text"], amt["amount_usd"], amt["currency"]
    if r.application_fee_usd is None:
        r.application_fee_usd = parse_application_fee(clean)

    if not r.degree_level:
        r.degree_level = parse_degree_levels(clean, head_lines=25) or parse_degree_levels(title or "")

    if not r.coverage:
        r.coverage = parse_coverage(clean)
    funding, _est = parse_funding_type(clean, r.coverage)
    if funding and funding != "Tuition-only" or funding and not r.funding_type:
        r.funding_type = funding
    if r.funding_type == "Full" and not r.amount_text:
        r.amount_text = "Full funding (see benefits list)"

    if not r.field:
        r.field = "Any" if meta.get("any_subject") else (
            parse_field(meta.get("field_label_text") or "", title=title) or parse_field(clean, title=title)
        )

    countries = parse_countries(clean, home_country=home_country, title=title)
    if not r.country:
        r.country = countries["country"]
    if not r.eligible_countries:
        r.eligible_countries = _only_countries(countries["eligible"])
    if not r.excluded_countries:
        r.excluded_countries = _only_countries(countries["excluded"])
    if not (r.min_gpa):
        gpa, scale, _ = parse_gpa(clean)
        r.min_gpa, r.gpa_scale = gpa, scale
    if r.min_ielts is None and r.min_toefl is None:
        ielts, toefl = parse_ielts(clean)
        r.min_ielts, r.min_toefl = ielts, toefl
    for k, v in parse_constraints(clean).items():
        if getattr(r, k, None) in (None, False, 0):
            setattr(r, k, v)
    prov = parse_provider(clean, title)
    r.provider = r.provider or prov["provider"]
    r.university = r.university or prov["university"]

    r.eligibility = _first_block(clean, ("eligibility", "who can apply", "who may apply", "requirements"))
    if not r.eligibility:
        m = _search(r"(?:brief description|description|summary)[:\s]+(.{60,600})", clean, re.IGNORECASE | re.DOTALL)
        r.eligibility = m.group(1).strip() if m else None
    if r.eligibility:
        r.eligibility = re.sub(r"\s+", " ", r.eligibility)[:1500]
    if meta.get("number_of_awards"):
        r.notes = f"awards={meta['number_of_awards']}"
    if meta.get("last_updated"):
        lu = _find_date(str(meta["last_updated"]), today=datetime.utcnow().date(), horizon_days=0)
        if lu and lu[1]:
            r.last_updated = lu[1]
    return r


def _first_block(text: str, headers: tuple[str, ...], max_chars: int = 900) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        low = line.strip().rstrip(":").lower()
        if low in headers or any(low.startswith(h) for h in headers):
            chunk: list[str] = []
            for nxt in lines[i + 1 :]:
                if re.fullmatch(r"[A-Z][A-Za-z /'’&-]{2,50}:?|\d+\.", nxt.strip()) and chunk:
                    break
                if len(chunk) and len(" ".join(chunk)) > max_chars:
                    break
                chunk.append(nxt.strip())
            body = " ".join(c for c in chunk if c)
            return body[:max_chars] or None
    return None
