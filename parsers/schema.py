"""Pydantic schema shared by the regex extractor and the LLM.

One schema, one validation path: whatever the LLM returns is checked here, so a
hallucinated degree level or a date like "31st Feb" can never reach the DB.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FundingType = Literal["Full", "Partial", "Tuition-only", "Unknown"]
DegreeLevel = Literal["Bachelor", "Master", "PhD", "Postdoc", "Diploma", "Any"]
CountryScope = Literal["worldwide", "developing", "group", "list", "single-country", "unknown"]


class ScholarshipRecord(BaseModel):
    """Validated, normalised view of one scholarship."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    title: str = Field(min_length=2, max_length=400)
    provider: str | None = None
    university: str | None = None
    country: str | None = None
    degree_level: DegreeLevel | str | None = None
    field: str | None = None
    funding_type: FundingType | None = None
    coverage: list[str] = Field(default_factory=list)
    amount_text: str | None = None
    amount_usd: float | None = None
    currency: str | None = None
    application_fee_usd: float | None = None
    deadline: date | None = None
    deadline_text: str | None = None
    deadline_rolling: bool = False
    deadline_estimated: bool = False
    open_date: date | None = None
    intake: str | None = None
    round_label: str | None = None
    eligible_countries: list[str] = Field(default_factory=list)
    excluded_countries: list[str] = Field(default_factory=list)
    country_scope: CountryScope = "unknown"
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
    notes: str | None = None

    # ------------------------------------------------------------ validators
    @field_validator("deadline", "open_date", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> Any:
        if v in (None, "", "null", "N/A", "unknown"):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            v = v.strip()
            m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
                try:
                    return datetime.strptime(v[:16], fmt).date()
                except ValueError:
                    continue
        raise ValueError(f"unparseable date: {v!r}")

    @field_validator("degree_level", mode="before")
    @classmethod
    def _norm_degree(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        text = str(v)
        found: list[str] = []
        mapping = (
            ("Bachelor", r"bachelor|undergraduate|\bb\.?sc\b"),
            ("Master", r"master|postgraduate|\bmba\b|m\.?sc\b"),
            ("PhD", r"ph\.?d|doctoral|doctorate"),
            ("Postdoc", r"post-?doc"),
            ("Diploma", r"diploma|foundation"),
            ("Any", r"^\s*any\b|all levels"),
        )
        for name, pat in mapping:
            if re.search(pat, text, re.IGNORECASE) and name not in found:
                found.append(name)
        if "Any" in found and len(found) > 1:
            found.remove("Any")
        if not found:
            return "Any"
        return ",".join(found)

    @field_validator("funding_type", mode="before")
    @classmethod
    def _norm_funding(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        text = str(v).lower()
        if "full" in text or "fully" in text:
            return "Full"
        if "tuition only" in text or "tuition-only" in text or "waiver" in text:
            return "Tuition-only"
        if "partial" in text:
            return "Partial"
        return "Unknown"

    @field_validator("coverage", "eligible_countries", "excluded_countries", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            v = re.split(r"[,;|]", v)
        if isinstance(v, (list, tuple, set)):
            out, seen = [], set()
            for item in v:
                s = re.sub(r"\s+", " ", str(item)).strip(" .-")
                if not s or s.lower() in {"null", "n/a", "none", "unknown"}:
                    continue
                if s.lower() in seen:
                    continue
                seen.add(s.lower())
                out.append(s[:80])
            return out[:40]
        return []

    @field_validator("amount_usd", "application_fee_usd", "min_gpa", "min_ielts", "min_work_experience_years", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any) -> Any:
        if v in (None, "", "null", "unknown"):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        m = re.search(r"[\d]+(?:[.,]\d+)?", str(v))
        return float(m.group(0).replace(",", ".")) if m else None

    @field_validator("min_toefl", "max_age", "max_years_since_degree", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        m = re.search(r"\d{1,3}", str(v))
        return int(m.group(0)) if m else None

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: Any) -> Any:
        v = re.sub(r"\s+", " ", str(v or "")).strip()
        v = re.sub(r"\s*[|•]\s*(Read more|Apply now|Scholarships?\s*4?\s*dev).*", "", v, flags=re.IGNORECASE)
        return v[:400]

    @field_validator("eligibility", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> Any:
        if v in (None, ""):
            return None
        return re.sub(r"\s+", " ", str(v)).strip()[:1500]

    # -------------------------------------------------------------- helpers
    def merged_over(self, other: ScholarshipRecord) -> ScholarshipRecord:
        """Fill my missing values from `other` (other = lower-confidence source)."""
        data = self.model_dump()
        for key, value in other.model_dump().items():
            cur = data.get(key)
            empty = cur is None or cur == [] or cur == "" or cur is False and value is True
            if empty and value not in (None, "", [], {}, False):
                data[key] = value
        return ScholarshipRecord.model_validate(data)

    def db_fields(self, **extra: Any) -> dict[str, Any]:
        from db.models import (  # local import avoids cycle
            canonical_key,
            normalize_title,
            normalize_url,
        )

        data = self.model_dump()
        for key in ("deadline", "open_date"):
            if isinstance(data.get(key), date):
                data[key] = datetime.combine(data[key], datetime.min.time().replace(hour=23, minute=59))
        if data.get("deadline") and not isinstance(data["deadline"], datetime):
            data["deadline"] = None
        data["title_norm"] = normalize_title(data["title"])
        data = {k: v for k, v in data.items() if (v is not None or k in {"deadline_rolling", "requires_gre", "requires_work_experience"}) and k not in {"notes", "model_version"}}
        if data.get("url"):
            data["normalized_url"] = normalize_url(data["url"])
            data.setdefault("canonical_key", canonical_key(data["title"], data.get("deadline"), data["url"]))
        data["eligible_countries"] = json.dumps(data.get("eligible_countries") or [])
        data["excluded_countries"] = json.dumps(data.get("excluded_countries") or [])
        data["coverage"] = json.dumps(data.get("coverage") or [])
        for k in ("_meta", "model_version"):
            data.pop(k, None)
        data.update(extra)
        return data
