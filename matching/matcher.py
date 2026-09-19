"""Filter & score.

The blueprint added points for nice-to-haves and never checked whether the
user could legally apply — so it happily emailed "USA government" awards a
Pakistani applicant can't get, and full-funding scholarships whose deadline
was 3 months after graduation-year limits.  Here scoring is two-stage:

  1. hard gates  → disqualifiers (nationality, degree level, age, GPA, IELTS,
                   work experience, years-since-degree, fee). Explicit "not
                   eligible" never becomes a low score; it becomes excluded
                   with a printed reason.
  2. soft score  → weighted preferences (0..100), scaled by a confidence
                   factor so half-parsed rows cannot outrank clean rows.
  3. urgency     → a separate nudge, plus a deadline-soon multiplier on the
                   tier so "apply now" items surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from db.models import utcnow
from matching.countries import canon, resolve_list

DEFAULT_WEIGHTS = {
    "country": 22,
    "field": 16,
    "funding": 18,
    "amount": 12,
    "degree": 10,
    "competition": 8,
    "english_ready": 6,
    "research": 8,
}
TIERS = (("must_apply", 78), ("strong", 62), ("worth_a_look", 45), ("low", 0))


@dataclass
class Gate:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    score: float
    tier: str
    gate: Gate
    breakdown: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    days_left: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps(
            {
                "score": self.score,
                "tier": self.tier,
                "confidence": self.confidence,
                "days_left": self.days_left,
                "breakdown": self.breakdown,
                "reasons": self.gate.reasons,
                "warnings": self.gate.warnings,
                "notes": self.notes,
            },
            ensure_ascii=False,
        )


# --------------------------------------------------------------------------- #
# hard gates
# --------------------------------------------------------------------------- #
def _as_dt(value: Any) -> datetime | None:
    """Accept datetime, date or ISO string — records move through several shapes."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value[:19])
        except ValueError:
            return None
    return None


def _gpa_scale(value: Any, default: float = 4.0) -> float:
    """`gpa_scale` can hold prose ("UK upper second") — never let it crash scoring."""
    try:
        v = float(value)
        return v if 1.0 <= v <= 10.0 else default
    except (TypeError, ValueError):
        m = re.search(r"(\d(?:\.\d)?)", str(value or ""))
        if m:
            v = float(m.group(1))
            return v if 1.0 <= v <= 10.0 else default
        return default


def _degree_match(record_degrees: str | None, profile_degree: str) -> bool | None:
    """True/False when decidable, None when the data can't answer."""
    if not record_degrees:
        return None
    wanted = {d.strip().lower() for d in str(profile_degree).split(",") if d.strip()}
    have = {d.strip().lower() for d in str(record_degrees).split(",") if d.strip()}
    if not have or {"any", "all"} & have:
        return True
    # "Bachelor,Master" satisfies a Master applicant; PhD-only does not
    if wanted & have:
        return True
    if {"master", "phd", "postdoc"} & wanted and "diploma" in have:
        return False
    return False


def check_gates(sch: Any, profile: Any) -> Gate:
    reasons: list[str] = []
    warnings: list[str] = []
    passed = True

    # ---- nationality -----------------------------------------------------
    home = canon(profile.home_country) if profile.home_country else None
    country = canon(sch.country) if sch.country else None
    eligible = _country_list(sch)
    excluded = _excluded_list(sch)
    if home:
        if excluded and home in excluded:
            passed = False
            reasons.append(f"programme excludes {home} citizens/nationals")
        elif eligible is not None and home not in eligible:
            passed = False
            reasons.append(f"open to {len(eligible)} listed countries; {home} not among them")
        elif eligible is None and excluded:
            warnings.append("eligibility list unclear — verify nationality before applying")
    if country and profile.countries_blocked and country in {canon(c) for c in profile.countries_blocked}:
        passed = False
        reasons.append(f"{country} blocked in your profile")

    # ---- degree ----------------------------------------------------------
    dm = _degree_match(sch.degree_level, profile.degree)
    if dm is False:
        passed = False
        reasons.append(f"for {sch.degree_level}; you want {profile.degree}")
    elif dm is None:
        warnings.append("degree level unknown")

    # ---- money -----------------------------------------------------------
    funding = (sch.funding_type or "").lower()
    if profile.needs_full_funding and funding in {"partial", "tuition-only"} and (sch.amount_usd or 0) < max(profile.min_award_usd, 1):
        warnings.append(f"{sch.funding_type} funding — check it covers your costs")
    fee = sch.application_fee_usd
    if fee is not None and fee > profile.max_application_fee_usd:
        warnings.append(f"application fee ${fee:,.0f} exceeds your ${profile.max_application_fee_usd:,.0f} cap")
    if profile.budget_usd_per_year and funding in {"tuition-only"} and (sch.amount_usd or 0) <= 0:
        warnings.append("tuition-only while you need living costs covered")

    # ---- academic minimums ----------------------------------------------
    if sch.min_gpa is not None and profile.gpa is not None:
        scale = _gpa_scale(sch.gpa_scale)
        need = sch.min_gpa
        my_scale = _gpa_scale(profile.gpa_scale)
        mine = profile.gpa if abs(my_scale - scale) < 0.01 else profile.gpa * scale / my_scale
        if mine + 1e-9 < need:
            passed = False
            reasons.append(f"GPA {mine:.2f}/{scale:g} below required {need:g}")
    if sch.min_ielts is not None and profile.ielts is not None and profile.ielts + 1e-9 < sch.min_ielts:
        passed = False
        reasons.append(f"IELTS {profile.ielts:g} below required {sch.min_ielts:g}")
    if sch.min_toefl is not None and profile.toefl is not None and profile.toefl < sch.min_toefl:
        passed = False
        reasons.append(f"TOEFL {profile.toefl} below required {sch.min_toefl}")

    # ---- demographic / timing -------------------------------------------
    if sch.max_age is not None and profile.age is not None and profile.age > sch.max_age:
        passed = False
        reasons.append(f"age limit {sch.max_age}; you are {profile.age}")
    if sch.min_work_experience_years and profile.work_experience_years + 1e-9 < sch.min_work_experience_years:
        passed = False
        reasons.append(f"needs {sch.min_work_experience_years:g} yrs experience; you have {profile.work_experience_years:g}")
    if sch.max_years_since_degree is not None and profile.graduation_year:
        since = utcnow().year - int(profile.graduation_year)
        if since > sch.max_years_since_degree:
            passed = False
            reasons.append(f"degree must be ≤{sch.max_years_since_degree} yrs old; yours is {since} yrs")
    if sch.requires_gre and profile.gre_percentile is None:
        warnings.append("GRE required but not in your profile")

    # ---- deadline sanity -------------------------------------------------
    days = None
    dl = _as_dt(getattr(sch, "deadline", None))
    if dl is not None:
        days = (dl - utcnow()).days
        if days < 0:
            passed = False
            reasons.append(f"deadline passed {abs(days)} days ago")
        elif days > profile.apply_window_days:
            warnings.append(f"opens far out ({days} days) — re-check later")
    elif not getattr(sch, "deadline_rolling", False):
        warnings.append("no deadline parsed")

    return Gate(passed, reasons, warnings)


def _country_list(sch: Any) -> set[str] | None:
    import json

    raw = getattr(sch, "eligible_countries", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [p.strip() for p in raw.split(",") if p.strip()]
    if not raw:
        return None
    allowed, scope = resolve_list(list(raw))
    if scope in {"worldwide"} or not allowed:
        # "International Students", "Commonwealth countries", "see website" … — a
        # phrase, not a country set. A wrong "not eligible" here hides the best
        # scholarships, so an unparseable list must never gate.
        return None
    joined = "".join(str(x) for x in raw)
    if sum(len(c) for c in allowed) < 0.45 * len(joined.replace(" ", "")):
        # mostly prose with a country name inside it (e.g. "Chevening-eligible
        # countries (Pakistan is eligible in most cycles)") — record it as a hint,
        # not a restriction.
        return None
    return allowed


def _excluded_list(sch: Any) -> set[str]:
    import json

    raw = getattr(sch, "excluded_countries", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [p.strip() for p in raw.split(",") if p.strip()]
    return {canon(c) for c in (raw or []) if canon(c)}


# --------------------------------------------------------------------------- #
# soft score
# --------------------------------------------------------------------------- #
def score_soft(sch: Any, profile: Any, weights: dict[str, float] | None = None) -> tuple[float, dict[str, float], list[str]]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    out: dict[str, float] = {}
    notes: list[str] = []

    # country
    country = canon(sch.country) if sch.country else None
    preferred = {canon(c) for c in profile.countries_preferred} if profile.countries_preferred else set()
    if country and country in preferred:
        out["country"] = w["country"]
        notes.append(f"preferred country ({country})")
    elif country:
        out["country"] = w["country"] * 0.25
    else:
        out["country"] = w["country"] * 0.4  # unknown → mild credit, flagged in confidence

    # field
    field_txt = (sch.field or "").lower()
    if field_txt:
        wanted = [f.lower() for f in profile.fields]
        if any(any(tok in field_txt for tok in _tokens(f)) for f in wanted) or any(f in field_txt for f in wanted):
            out["field"] = w["field"]
            notes.append("field matches your targets")
        elif "any" in field_txt:
            out["field"] = w["field"] * 0.85
            notes.append("open to all fields")
        elif any(x in field_txt for x in ("stem", "engineering", "comput", "science")):
            out["field"] = w["field"] * 0.5
        else:
            out["field"] = w["field"] * 0.05
    else:
        out["field"] = w["field"] * 0.4

    # excluded fields
    for bad in profile.exclude_fields or ():
        if bad.lower() in field_txt:
            out["field"] = 0.0
            notes.append(f"excluded field: {bad}")

    # funding
    funding = (sch.funding_type or "").lower()
    out["funding"] = {"full": w["funding"], "tuition-only": w["funding"] * 0.45, "partial": w["funding"] * 0.35}.get(
        funding, w["funding"] * 0.2
    )
    if funding == "full":
        notes.append("fully funded")

    # amount vs need
    amount = float(sch.amount_usd or 0)
    need = profile.budget_usd_per_year or 25000
    if amount > 0:
        ratio = min(1.0, amount / max(need, 1))
        out["amount"] = w["amount"] * (0.35 + 0.65 * ratio)
        notes.append(f"award ≈ ${amount:,.0f}/yr")
    else:
        out["amount"] = w["amount"] * (0.8 if funding == "full" else 0.3)

    # degree level
    if not sch.degree_level or "any" in str(sch.degree_level).lower():
        out["degree"] = w["degree"] * 0.9
    elif canon_degree(sch.degree_level, profile.degree):
        out["degree"] = w["degree"]
    else:
        out["degree"] = w["degree"] * 0.2

    # competition signals
    comp = w["competition"] * 0.5
    extras = getattr(sch, "extras", None) or {}
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except Exception:
            extras = {}
    n_awards = extras.get("number_of_awards")
    if isinstance(n_awards, (int, float)):
        comp = w["competition"] * (0.35 if n_awards < 30 else 0.65 if n_awards < 200 else 0.95)
        notes.append(f"{int(n_awards)} awards")
    out["competition"] = comp

    # english readiness
    if sch.min_ielts is None and sch.min_toefl is None:
        out["english_ready"] = w["english_ready"]
        notes.append("no stated English test minimum")
    elif profile.ielts and sch.min_ielts and profile.ielts >= sch.min_ielts + 0.5:
        out["english_ready"] = w["english_ready"] * 0.85
    else:
        out["english_ready"] = w["english_ready"] * 0.4

    # research alignment
    res = 0.0
    if profile.has_research and re.search(r"research|thesis|phd|publication", (sch.description or "") + (sch.field or ""), re.IGNORECASE):
        res = w["research"] * 0.8
        notes.append("research-friendly")
    if profile.publications >= 3 and funding == "full":
        res = min(w["research"], res + w["research"] * 0.2)
    out["research"] = res or w["research"] * 0.3

    total = round(sum(out.values()), 2)
    return total, {k: round(v, 2) for k, v in out.items()}, notes


def canon_degree(record: str | None, wanted: str) -> bool:
    if not record:
        return False
    have = {d.strip().lower() for d in record.split(",")}
    return "any" in have or wanted.strip().lower() in have


def _tokens(field_name: str) -> list[str]:
    key = {
        "AI": ("ai", "artificial intelligence"),
        "Machine Learning": ("machine learning", "ml"),
        "NLP": ("nlp", "natural language"),
        "Deep Learning": ("deep learning",),
        "Computer Science": ("computer science", "computing", "software"),
        "Data Science": ("data science", "data"),
        "Robotics": ("robotic",),
        "Cybersecurity": ("security", "cyber"),
    }.get(field_name.strip(), (field_name.strip().lower(),))
    return [t for t in key if t]


# --------------------------------------------------------------------------- #
# confidence + urgency
# --------------------------------------------------------------------------- #
def confidence_of(sch: Any) -> float:
    """How much of the record is actually populated with real values."""
    essentials = (sch.title, sch.deadline or getattr(sch, "deadline_rolling", False), sch.country, sch.degree_level, sch.funding_type)
    score = sum(1 for v in essentials if v not in (None, "", False)) / len(essentials)
    bonus = 0.06 * bool(sch.amount_text) + 0.06 * bool(sch.eligibility) + 0.04 * bool(sch.field)
    parse = getattr(sch, "parse_method", None) or ""
    if "regex" in parse and score >= 1.0:
        bonus += 0.08  # literal label read — no model involved
    elif parse in ("", "none"):
        bonus -= 0.05
    return round(min(1.0, max(0.2, 0.45 + 0.45 * score + bonus)), 3)


def apply_urgency(score: float, days_left: int | None) -> tuple[float, list[str]]:
    notes: list[str] = []
    if days_left is None:
        return score, notes
    if days_left < 0:
        return score, notes
    if days_left <= 14:
        notes.append(f"closes in {days_left} days")
        return score * 1.12, notes
    if days_left <= 45:
        notes.append(f"closes in {days_left} days")
        return score * 1.05, notes
    if days_left > 240:
        notes.append(f"{days_left} days out")
        return score * 0.97, notes
    return score, notes


def tier_for(score: float, gate: Gate, thresholds: dict[str, int] | None = None) -> str:
    th = {t: v for t, v in TIERS}
    th.update(thresholds or {})
    if not gate.passed:
        return "ineligible"  # scored but suppressed from digests/reports
    if score >= th["must_apply"]:
        return "must_apply"
    if score >= th["strong"]:
        return "strong"
    if score >= th["worth_a_look"]:
        return "worth_a_look"
    return "low"


def evaluate(sch: Any, profile: Any, *, weights: dict[str, float] | None = None, thresholds: dict[str, int] | None = None) -> MatchResult:
    gate = check_gates(sch, profile)
    raw, breakdown, notes = score_soft(sch, profile, weights)
    conf = confidence_of(sch)
    scaled = round(raw * (0.7 + 0.3 * conf), 2)
    if not gate.passed:
        # a disqualified programme must never outrank an eligible one; the raw
        # soft score is kept in the breakdown for debugging "why was this dropped"
        scaled = 0.0
    days = (_as_dt(getattr(sch, "deadline", None)) - utcnow()).days if getattr(sch, "deadline", None) else None
    scaled, urgency_notes = apply_urgency(scaled, days)
    scaled = round(min(100.0, scaled), 2)
    tier = tier_for(scaled, gate, thresholds)
    if not gate.passed:
        scaled = round(min(40.0, raw * 0.4), 2)  # visible-but-last, never notified
    all_notes = (["disqualified: " + r for r in gate.reasons] if not gate.passed else []) + notes + urgency_notes
    warnings = list(gate.warnings)
    if conf < 0.55:
        warnings.append(f"low data confidence ({conf:.0%})")
    # the *why* strings must reach the user, not just the logs: the digest prints
    # `r.warnings` and the dashboard renders `gate_reasons.warnings`.
    warnings += [n for n in all_notes if n not in warnings][:3]
    return MatchResult(
        score=scaled,
        tier=tier,
        gate=Gate(gate.passed, gate.reasons, warnings),
        breakdown=breakdown,
        confidence=conf,
        days_left=days,
        notes=all_notes,
    )


def filter_and_score(rows: Iterable[Any], profile: Any, *, min_score: float = 45, weights=None, thresholds=None) -> list[Any]:
    """In-place scoring + the ranked shortlist (used by the pipeline and tests)."""
    kept: list[Any] = []
    for sch in rows:
        res = evaluate(sch, profile, weights=weights, thresholds=thresholds)
        sch.match_score = res.score
        sch.tier = res.tier
        sch.confidence = res.confidence
        sch.gate_pass = res.gate.passed
        sch.gate_reasons = json.dumps({"reasons": res.gate.reasons, "warnings": res.gate.warnings}, ensure_ascii=False)
        sch.score_breakdown = res.as_json()
        if res.gate.passed and res.score >= min_score and (sch.days_left is None or sch.days_left >= 0):
            kept.append(sch)
    return sorted(kept, key=lambda r: (-r.match_score, r.deadline or datetime.max))
