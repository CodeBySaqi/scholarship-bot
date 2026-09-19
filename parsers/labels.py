"""Label-aware extraction.

Many scholarship aggregators publish *labelled* metadata lines
(`Deadline: 6 Oct 2026 (annual)`, `Level/Field(s) of study: …`,
`Study in: UK`, `Scholarship value/inclusions: …`).  Reading the labels is far
more accurate than regex-scanning the whole page — and free.

`parse_labels` builds a label → value map; `apply_labels` converts the subset
that matters into typed extraction results.  `fields.extract_all` then fills
whatever is still missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

LABELS: dict[str, tuple[str, ...]] = {
    "deadline": ("deadline", "closing date", "closing date for applications", "application deadline",
                 "last date for applications", "apply by", "deadline for applications"),
    "open_date": ("opening date", "applications open", "applications start", "start of applications"),
    "level": ("level/field(s) of study", "level of study", "level", "degree", "study level", "academic level",
              "eligibility level", "level/field of study"),
    "field": ("field", "fields of study", "field(s) of study", "subject", "subjects", "discipline", "disciplines",
              "area of study", "areas of study", "courses", "courses offered"),
    "country": ("study in", "host country", "destination country", "country", "country of study", "location",
                "country/region"),
    "institution": ("host institution(s)", "host institution", "host university", "host universities",
                    "institutions", "university", "institution", "universities"),
    "value": ("scholarship value/inclusions", "scholarship value", "benefits", "what the scholarship covers",
              "amount", "value of award", "scholarship covers", "value and inclusions", "financial support",
              "scholarship award", "award value"),
    "eligibility": ("eligibility", "who can apply", "who may apply", "eligibility criteria", "requirements",
                    "target group", "criteria"),
    "number_of_awards": ("number of awards", "awards available", "how many scholarships", "no. of awards"),
    "language": ("english language requirements", "language requirements", "english requirements",
                 "english language requirement"),
    "nationality": ("nationality", "target countries", "eligible countries", "open to", "who is eligible from",
                    "citizenship"),
    "intake": ("course starts", "intake", "starting", "start date", "tenure", "duration", "period"),
    "updated": ("last updated", "updated"),
    "official": ("official website", "official link", "apply online", "website", "more info", "further information"),
    "description": ("brief description", "description", "summary", "about"),
    "procedure": ("application procedure", "application procedures", "how to apply", "application process",
                  "selection procedure", "selection criteria", "application requirements", "documents required"),
    "contact": ("contact", "more information", "enquiries"),
}

# any of these, even if not a tracked field, terminates the previous value
_STOP_LABELS = {
    "course starts", "starting", "closing", "status", "type", "region", "eligibility criteria", "required",
    "read more", "read", "more", "share", "print", "email", "apply now", "register", "link", "website",
    "follow us", "categories", "tags", "related", "comments", "subscribe", "skip to content",
}

_ALIASED: dict[str, str] = {alias: key for key, aliases in LABELS.items() for alias in aliases}
_LABEL_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z /'()&,\-.]{1,44}?)\s*[:\-–—]\s*(.*)$")
# labels whose value legitimately wraps to the next line on these sites
_WRAP_OK = {"deadline", "open_date"}
_NEW_SENTENCE = re.compile(r"^(?:[A-Z][a-z]+\s+(?:[A-Za-z0-9/&'().-]+\s+){1,}|Last updated:|Read More$)")

_MAX_CONT = 14
_MAX_CONT_CHARS = 1200
_INLINE_MAX = 260


def _norm_label(raw: str) -> str:
    lab = re.sub(r"[^a-z()0-9 /,'&\-]", "", raw.lower()).strip()
    lab = re.sub(r"\s+", " ", lab).strip(" -")
    lab = re.sub(r"\bs\b(?=\s|$)", "", lab).strip()  # trailing plural 's' noise
    return lab


def _lookup(raw_label: str) -> str | None:
    lab = _norm_label(raw_label)
    if not lab:
        return None
    if lab in _ALIASED:
        return _ALIASED[lab]
    for alias, key in _ALIASED.items():
        if len(lab) > 6 and (alias.startswith(lab + " ") or lab.startswith(alias + " ") or lab == alias):
            return key
    if lab.startswith("scholarship value"):
        return "value"
    if lab.startswith("level/field"):
        return "level"
    if lab.startswith("last updated"):
        return "updated"
    return None


@dataclass
class LabelData:
    raw: dict[str, str] = dc_field(default_factory=dict)
    multi: dict[str, list[str]] = dc_field(default_factory=dict)
    found: list[str] = dc_field(default_factory=list)

    def get(self, key: str) -> str | None:
        val = self.raw.get(key)
        val = re.sub(r"\s+", " ", val or "").strip(" -|")
        return val or None

    def join(self, *keys: str) -> str:
        return "\n".join(self.raw[k] for k in keys if self.raw.get(k))

    def all_text(self) -> str:
        return "\n".join(f"{k}: {v}" for k, v in self.raw.items() if k != "updated")


def parse_labels(text: str) -> LabelData:
    data = LabelData()
    if not text:
        return data
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # pass 1 — classify every line: tracked label / untracked label (stop) / value
    entries: list[tuple[int, str, str] | None] = []
    stops: list[int] = []
    for i, line in enumerate(lines):
        m = _LABEL_LINE.match(line)
        if not m or len(line) > 120:
            entries.append(None)
            continue
        key = _lookup(m.group(1))
        if key and key != "official":
            entries.append((i, key, m.group(2).strip()))
        else:
            entries.append(None)
            stops.append(i)

    # pass 2 — value = inline remainder (+ following lines only for multi-line labels)
    label_pos = [i for i, e in enumerate(entries) if e]
    for pos, cur in enumerate(label_pos):
        i, key, inline = entries[cur]  # type: ignore[misc]
        nxt_label = next((entries[j][0] for j in label_pos[pos + 1 :]), len(lines))  # type: ignore[index]
        nxt_stop = next((st for st in stops if st > i), len(lines))
        end = min(nxt_label, nxt_stop)
        if inline:
            chunk = [inline]
            # A short inline value may wrap onto the next line ("Deadline:" then
            # "6 Oct 2026"), but a *labelled* value that already reads as a value
            # must not swallow the next paragraph ("Study in: UK" + "Course starts…").
            if len(inline) < 24 and key in _WRAP_OK:
                for j in range(i + 1, end):
                    nxt_line = lines[j]
                    if nxt_line in ("", "|") or re.fullmatch(r"[|·•\-]", nxt_line):
                        continue
                    if _NEW_SENTENCE.match(nxt_line):
                        break
                    chunk.append(nxt_line)
                    if len(" ".join(chunk)) > 90:
                        break
        else:
            chunk = []
            for j in range(i + 1, end):
                chunk.append(lines[j])
                if len(chunk) >= _MAX_CONT or len(" ".join(chunk)) > _MAX_CONT_CHARS:
                    break
        value = re.sub(r"\s{2,}", " ", " ".join(c for c in chunk if c)).strip(" -|")
        value = re.sub(r"\s*\|\s*$", "", value)
        if value:
            data.raw[key] = (data.raw.get(key, "") + " " + value).strip()
            data.multi.setdefault(key, []).append(value)
            if key not in data.found:
                data.found.append(key)
    return data


# --------------------------------------------------------------------------- #
# label → typed values
# --------------------------------------------------------------------------- #
_DEGREE_MAP = (
    (r"bachelor|undergraduate|b\.?sc|b\.?a\b|bba|btech|laurea", "Bachelor"),
    (r"master|postgraduate|post-graduate|\bmba\b|m\.?sc|\bmtech\b|\bm\.eng", "Master"),
    (r"ph\.?d|doctoral|doctorate|phd|dphil", "PhD"),
    (r"post-?doc|postdoctoral", "Postdoc"),
    (r"\bany\b|all (?:levels|degrees)|any level", "Any"),
)
_SPLIT = re.compile(r"[\s,/&+]| and ")


def degree_from_label(value: str | None) -> str | None:
    if not value:
        return None
    hits: list[str] = []
    low = value.lower()
    for pat, name in _DEGREE_MAP:
        if re.search(pat, low) and name not in hits:
            hits.append(name)
    order = ["Bachelor", "Master", "PhD", "Postdoc", "Any"]
    ordered = [h for h in order if h in hits]
    # "any" wins only when nothing else is named
    if "Any" in ordered and len(ordered) > 1:
        ordered = [h for h in ordered if h != "Any"]
    return ",".join(ordered) or None


def country_from_label(value: str | None) -> tuple[str | None, list[str]]:
    """`Study in: UK`, `Study in: Belgium, Netherlands` → country + list."""
    if not value:
        return None, []
    value = re.split(r"\|{2,}|\bRead more\b", value)[0]
    value = re.sub(r"\s*(for|intended for|and)\s+(international|developing|all)\b.*$", "", value, flags=re.IGNORECASE)
    parts = [re.sub(r"\s{2,}", " ", p).strip(" .,-") for p in re.split(r",| and ", value)]
    parts = [p for p in parts if 2 < len(p) <= 64 and re.fullmatch(r"[A-Za-z][A-Za-z .'\-()]*", p)]
    return (parts[0] if parts else None), parts


def any_field_signal(*values: str | None) -> bool:
    joined = " ".join(v for v in values if v).lower()
    return bool(
        re.search(r"any (?:subject|field|discipline|course|program|degree)", joined)
        or re.search(r"all (?:subjects|fields|disciplines)", joined)
        or re.fullmatch(r"[^a-z]*any[^a-z]*", joined or " ")
    )


def apply_labels(labels: LabelData) -> dict[str, object]:
    """Convert the label map into typed values for RawExtraction."""
    from .fields import (  # local to avoid cycle
        parse_amounts,
        parse_deadline,
        parse_ielts,
    )

    out: dict[str, object] = {}
    if (val := labels.get("deadline")):
        dl = parse_deadline(val if "\n" in val else f"Deadline: {val}")
        out["deadline"] = dl["deadline"]
        out["deadline_text"] = val[:180]
        out["deadline_rolling"] = dl["deadline_rolling"]
        out["deadline_estimated"] = dl["deadline_estimated"]
        if dl["round_label"]:
            out["round_label"] = dl["round_label"]
    country, country_list = country_from_label(labels.get("country"))
    if country:
        out["country"] = country
        if len(country_list) > 1:
            out["eligible_countries"] = country_list
    if (deg := degree_from_label(labels.get("level"))):
        out["degree_level"] = deg
    inst = labels.get("institution")
    if inst and re.search(r"university|institute|college|school|any ", inst, re.IGNORECASE):
        out["provider"] = inst[:200]
    val_blob = labels.get("value")
    if val_blob:
        amt = parse_amounts(val_blob)
        if amt["amount_text"]:
            out["amount_text"] = amt["amount_text"]
            out["amount_usd"] = amt["amount_usd"]
            out["currency"] = amt["currency"]
        if re.search(r"full|all|complete|covers? the", val_blob, re.IGNORECASE) and re.search(r"tuition", val_blob, re.IGNORECASE):
            out["funding_type"] = "Partial"  # refined by coverage below
    eng = labels.get("language")
    if eng:
        ielts, toefl = parse_ielts(eng)
        if ielts:
            out["min_ielts"] = ielts
        if toefl:
            out["min_toefl"] = toefl
    nat = labels.get("nationality")
    if nat:
        items = [re.sub(r"\s{2,}", " ", p).strip(" .,-") for p in re.split(r",| and ", nat)]
        items = [p for p in items if 2 < len(p) <= 40 and re.fullmatch(r"[A-Za-z][A-Za-z .'\-()]*", p)]
        if items:
            out["eligible_countries"] = items
    if (n := labels.get("number_of_awards")):
        m = re.search(r"(\d[\d,]{0,9})", n)
        if m:
            out["_meta_number_of_awards"] = int(m.group(1).replace(",", ""))
    if (it := labels.get("intake")):
        m = re.search(r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?)\s*((?:19|20)\d{2})\b", it, re.IGNORECASE)
        if m:
            out["intake"] = f"{m.group(1).title()} {m.group(2)}"
        else:
            m = re.search(r"\b(20\d\d\s*[-–/]\s*(?:20)?\d\d)\b", it)
            if m:
                out["intake"] = m.group(1)
    if any_field_signal(labels.get("field"), labels.get("level"), val_blob):
        out["field"] = "Any"
        out["_any_subject"] = True
    elif (f := labels.get("field")):
        out["_field_label_text"] = f[:400]
    return out
