"""Country normalisation + eligibility group expansion.

The matcher needs to answer "can a citizen of X apply?" without an LLM, so
group names (Commonwealth, EU/EEA, OIC, Nordic, Developing countries…) are
expanded to member sets here.
"""

from __future__ import annotations

import re

# Display name -> canonical name. Include the spellings aggregators actually use.
ALIASES: dict[str, str] = {
    "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states": "United States", "united states of america": "United States", "america": "United States", "us": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "wales": "United Kingdom", "scotland": "United Kingdom", "northern ireland": "United Kingdom",
    "russia": "Russia", "russian federation": "Russia", "уfr": "Russia",
    "korea": "South Korea", "south korea": "South Korea", "republic of korea": "South Korea", "korea (south)": "South Korea",
    "north korea": "North Korea",
    "vietnam": "Vietnam", "viet nam": "Vietnam",
    "iran": "Iran", "islamic republic of iran": "Iran",
    "syria": "Syria", "syrian arab republic": "Syria",
    "turkiye": "Türkiye", "turkey": "Türkiye",
    "czechia": "Czechia", "czech republic": "Czechia",
    "myanmar": "Myanmar", "burma": "Myanmar",
    "eswatini": "Eswatini", "swaziland": "Eswatini",
    "tanzania": "Tanzania", "united republic of tanzania": "Tanzania",
    "south africa": "South Africa", "rsa": "South Africa",
    "pakistan": "Pakistan", "islamic republic of pakistan": "Pakistan",
    "india": "India", "republic of india": "India",
    "bangladesh": "Bangladesh", "netherlands": "Netherlands", "the netherlands": "Netherlands", "holland": "Netherlands",
    "germany": "Germany", "deutschland": "Germany", "france": "France", "spain": "Spain", "italy": "Italy",
    "portugal": "Portugal", "poland": "Poland", "hungary": "Hungary", "romania": "Romania", "bulgaria": "Bulgaria",
    "greece": "Greece", "austria": "Austria", "switzerland": "Switzerland", "sweden": "Sweden", "norway": "Norway",
    "denmark": "Denmark", "finland": "Finland", "iceland": "Iceland", "ireland": "Ireland",
    "belgium": "Belgium", "luxembourg": "Luxembourg", "germany ": "Germany",
    "canada": "Canada", "australia": "Australia", "new zealand": "New Zealand", "japan": "Japan", "china": "China",
    "prc": "China", "singapore": "Singapore", "malaysia": "Malaysia", "thailand": "Thailand", "indonesia": "Indonesia",
    "philippines": "Philippines", "saudi arabia": "Saudi Arabia", "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates", "qatar": "Qatar", "kuwait": "Kuwait", "oman": "Oman", "bahrain": "Bahrain",
    "jordan": "Jordan", "lebanon": "Lebanon", "iraq": "Iraq", "egypt": "Egypt", "morocco": "Morocco", "algeria": "Algeria",
    "tunisia": "Tunisia", "libya": "Libya", "sudan": "Sudan", "south sudan": "South Sudan", "ethiopia": "Ethiopia",
    "kenya": "Kenya", "uganda": "Uganda", "tanzania, united republic": "Tanzania", "nigeria": "Nigeria", "ghana": "Ghana",
    "senegal": "Senegal", "cameroon": "Cameroon", "zambia": "Zambia", "zimbabwe": "Zimbabwe", "mozambique": "Mozambique",
    "malawi": "Malawi", "namibia": "Namibia", "botswana": "Botswana", "lesotho": "Lesotho", "rwanda": "Rwanda",
    "mauritius": "Mauritius", "sierra leone": "Sierra Leone", "liberia": "Liberia", "gambia": "Gambia",
    "nepal": "Nepal", "sri lanka": "Sri Lanka", "bhutan": "Bhutan", "maldives": "Maldives", "afghanistan": "Afghanistan",
    "yemen": "Yemen", "palestine": "Palestine", "west bank": "Palestine", "gaza": "Palestine",
    "ukraine": "Ukraine", "belarus": "Belarus", "moldova": "Moldova", "georgia": "Georgia", "armenia": "Armenia",
    "azerbaijan": "Azerbaijan", "kazakhstan": "Kazakhstan", "uzbekistan": "Uzbekistan", "kyrgyzstan": "Kyrgyzstan",
    "tajikistan": "Tajikistan", "turkmenistan": "Turkmenistan", "mongolia": "Mongolia",
    "brazil": "Brazil", "mexico": "Mexico", "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia",
    "peru": "Peru", "venezuela": "Venezuela", "ecuador": "Ecuador", "uruguay": "Uruguay", "paraguay": "Paraguay",
    "bolivia": "Bolivia", "costa rica": "Costa Rica", "panama": "Panama", "cuba": "Cuba", "jamaica": "Jamaica",
    "trinidad and tobago": "Trinidad and Tobago", "barbados": "Barbados", "fiji": "Fiji", "samoa": "Samoa",
    "tonga": "Tonga", "vanuatu": "Vanuatu", "solomon islands": "Solomon Islands", "papua new guinea": "Papua New Guinea",
    "serbia": "Serbia", "croatia": "Croatia", "bosnia and herzegovina": "Bosnia and Herzegovina",
    "north macedonia": "North Macedonia", "albania": "Albania", "montenegro": "Montenegro", "slovenia": "Slovenia",
    "slovakia": "Slovakia", "lithuania": "Lithuania", "latvia": "Latvia", "estonia": "Estonia", "cyprus": "Cyprus",
    "malta": "Malta", "andorra": "Andorra", "liechtenstein": "Liechtenstein", "monaco": "Monaco", "san marino": "San Marino",
    "brunei": "Brunei", "timor-leste": "Timor-Leste", "laos": "Laos", "cambodia": "Cambodia",
}

_EU_EEA = {
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland", "France",
    "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands",
    "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden", "Iceland", "Liechtenstein", "Norway",
}

_COMMONWEALTH = {
    "United Kingdom", "India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal", "Maldives", "Nigeria", "Ghana", "Kenya",
    "Uganda", "Tanzania", "Zambia", "Zimbabwe", "Malawi", "Mozambique", "Namibia", "Botswana", "Lesotho", "Eswatini",
    "South Africa", "Cameroon", "Rwanda", "Sierra Leone", "Liberia", "Gambia", "Australia", "New Zealand", "Canada",
    "Ireland", "Cyprus", "Malta", "Fiji", "Samoa", "Tonga", "Vanuatu", "Solomon Islands", "Papua New Guinea", "Jamaica",
    "Trinidad and Tobago", "Barbados", "Guyana", "Bahamas", "Belize", "Brunei", "Singapore", "Montserrat",
}

_OIC = {
    "Pakistan", "Bangladesh", "Indonesia", "Malaysia", "Türkiye", "Saudi Arabia", "UAE", "Qatar", "Kuwait", "Oman",
    "Bahrain", "Jordan", "Lebanon", "Syria", "Iraq", "Egypt", "Morocco", "Algeria", "Tunisia", "Libya", "Sudan",
    "Somalia", "Djibouti", "Comoros", "Senegal", "Gambia", "Guinea", "Sierra Leone", "Mali", "Niger", "Nigeria",
    "Chad", "Cameroon", "Gabon", "Azerbaijan", "Kazakhstan", "Uzbekistan", "Turkmenistan", "Kyrgyzstan", "Tajikistan",
    "Afghanistan", "Iran", "Palestine", "Yemen", "Brunei", "Maldives", "Albania", "Turkey",
}

_NORDIC = {"Sweden", "Norway", "Denmark", "Finland", "Iceland"}

_DEVELOPING = {
    "Pakistan", "India", "Bangladesh", "Sri Lanka", "Nepal", "Afghanistan", "Myanmar", "Cambodia", "Laos", "Vietnam",
    "Philippines", "Indonesia", "Timor-Leste", "Papua New Guinea", "Fiji", "Samoa", "Tonga", "Vanuatu", "Solomon Islands",
    "Nigeria", "Ghana", "Kenya", "Uganda", "Tanzania", "Ethiopia", "Rwanda", "Zambia", "Zimbabwe", "Malawi", "Mozambique",
    "Namibia", "Botswana", "Lesotho", "Eswatini", "South Africa", "Cameroon", "Senegal", "Gambia", "Sierra Leone", "Liberia",
    "Mali", "Niger", "Chad", "Burkina Faso", "Guinea", "Egypt", "Morocco", "Tunisia", "Algeria", "Libya", "Sudan", "Somalia",
    "Jordan", "Lebanon", "Syria", "Iraq", "Yemen", "Palestine", "Iran", "Pakistan ", "Egypt ", "Morocco ",
    "Brazil", "Mexico", "Argentina", "Chile", "Colombia", "Peru", "Venezuela", "Ecuador", "Bolivia", "Paraguay", "Uruguay",
    "Costa Rica", "Panama", "Cuba", "Jamaica", "Haiti", "Dominican Republic", "Trinidad and Tobago", "Guyana", "Suriname",
    "China", "Mongolia", "Uzbekistan", "Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan", "Azerbaijan", "Georgia",
    "Armenia", "Moldova", "Ukraine", "Belarus", "Albania", "Bosnia and Herzegovina", "North Macedonia", "Serbia",
    "Montenegro", "Kosovo", "Thailand", "Malaysia", "Bhutan", "Maldives",
}

_GROUPS: dict[str, set[str]] = {
    "eu": _EU_EEA, "european union": _EU_EEA, "eea": _EU_EEA | _NORDIC - {"United Kingdom"},
    "europe": _EU_EEA | {"United Kingdom", "Switzerland", "Norway", "Iceland", "Ukraine", "Belarus", "Moldova", "Serbia",
                          "Bosnia and Herzegovina", "Albania", "North Macedonia", "Montenegro", "Slovenia", "Croatia",
                          "Russia", "Turkey", "Georgia", "Armenia", "Azerbaijan", "Cyprus", "Malta"},
    "commonwealth": _COMMONWEALTH,
    "commonwealth countries": _COMMONWEALTH,
    "oic": _OIC, "developing": _DEVELOPING, "developing countries": _DEVELOPING, "lMIC": _DEVELOPING,
    "low-income": _DEVELOPING, "nordic": _NORDIC, "scandinavia": _NORDIC,
    "g20": _EU_EEA | {"United States", "United Kingdom", "China", "India", "Japan", "South Korea", "Australia",
                      "Canada", "Russia", "Brazil", "Mexico", "Argentina", "Indonesia", "Saudi Arabia", "Türkiye",
                      "South Africa", "Pakistan"},
    "asean": {"Indonesia", "Malaysia", "Thailand", "Vietnam", "Philippines", "Singapore", "Myanmar", "Cambodia", "Laos", "Brunei"},
    "south asia": {"India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal", "Bhutan", "Maldives", "Afghanistan"},
    "sub-saharan africa": {c for c in _DEVELOPING if c in {
        "Nigeria", "Ghana", "Kenya", "Uganda", "Tanzania", "Ethiopia", "Rwanda", "Zambia", "Zimbabwe", "Malawi",
        "Mozambique", "Namibia", "Botswana", "Lesotho", "Eswatini", "South Africa", "Cameroon", "Senegal", "Gambia",
        "Sierra Leone", "Liberia", "Mali", "Niger", "Chad", "Burkina Faso", "Guinea", "Somalia"}},
    "arab": {"Egypt", "Morocco", "Algeria", "Tunisia", "Libya", "Sudan", "Jordan", "Lebanon", "Syria", "Iraq",
             "Yemen", "Palestine", "Saudi Arabia", "UAE", "Qatar", "Kuwait", "Oman", "Bahrain", "Somalia", "Djibouti",
             "Comoros", "Mauritania"},
    "latin america": {"Brazil", "Mexico", "Argentina", "Chile", "Colombia", "Peru", "Venezuela", "Ecuador", "Bolivia",
                      "Paraguay", "Uruguay", "Costa Rica", "Panama", "Cuba", "Jamaica", "Dominican Republic",
                      "Trinidad and Tobago", "Guyana", "Suriname", "Haiti"},
    "caribbean": {"Cuba", "Jamaica", "Trinidad and Tobago", "Barbados", "Bahamas", "Belize", "Guyana", "Suriname",
                  "Dominican Republic", "Haiti"},
    "pacific islands": {"Fiji", "Samoa", "Tonga", "Vanuatu", "Solomon Islands", "Papua New Guinea", "Kiribati", "Nauru",
                        "Tuvalu", "Palau", "Marshall Islands", "Micronesia"},
    "worldwide": set(), "world": set(), "global": set(), "international": set(), "all countries": set(),
    "any": set(), "any country": set(), "every country": set(), "all": set(),
}

# Country -> "is this jurisdiction the whole programme closed to non-residents?"
RESTRICTIVE_SINGLE_COUNTRY = {
    "India": {"india only", "only for indian", "indian nationals only"},
}

_WORD_CHARS = re.compile(r"[^a-zéëüöñçàáíóúé ]+")


def canon(name: str | None) -> str | None:
    if not name:
        return None
    raw = _WORD_CHARS.sub("", str(name).strip().lower()).strip()
    raw = raw.replace("’", "'")
    raw2 = re.sub(r"[^a-z0-9 ]+", "", str(name).strip().lower()).strip()
    for key in (str(name).strip(), str(name).strip().lower(), raw, raw2):
        if key in ALIASES:
            return ALIASES[key]
    # Title-case fallback for names we have not aliased — keep the original
    # letters so "Türkiye" doesn't become "Trkiye" through an ASCII filter.
    cleaned = re.sub(r"\s+", " ", str(name).strip())
    cleaned = re.sub(r"^(?:the|study in|study at|in|at|located in)\s*:?\s*", "", cleaned, flags=re.IGNORECASE)
    end = len(cleaned)
    while end > 0 and not (cleaned[end - 1].isalpha() or cleaned[end - 1] in "&'"):
        end -= 1
    cleaned = cleaned[:end]
    if not cleaned:
        return None
    if cleaned.isupper() and len(cleaned) <= 6:  # "UK", "USA", "UAE"
        return cleaned
    small = {"de", "del", "la", "le", "du", "d'", "van", "von", "of", "and", "the", "is", "di", "da"}
    words = cleaned.split(" ")
    out_words = []
    for i, w in enumerate(words):
        if not w:
            continue
        low = w.lower()
        if i and (low in small or low.startswith(("d'", "l'", "mc", "mac")) is False and low in small):
            out_words.append(low)
        elif low in {"islamic", "republic", "emirates", "kingdom", "states", "united"}:
            out_words.append(w[:1].upper() + w[1:].lower())
        elif len(w) > 2 and w[1] == "'":       # d'Ivoire, l'Émirats
            out_words.append(w[:2].lower() + w[2:].title())
        else:
            out_words.append(w[:1].upper() + w[1:].lower() if w[0].isalpha() else w)
    return " ".join(out_words)


def expand_group(name: str) -> set[str] | None:
    key = re.sub(r"[^a-z ]+", "", name.strip().lower()).strip()
    if not key:
        return None
    for needle, members in _GROUPS.items():
        if key == needle or needle in key:
            return set(members)
    return None


def resolve_list(items: list[str] | None) -> tuple[set[str] | None, str]:
    """Return (allowed_set_or_None_if_universal, scope_label).

    scope_label: "worldwide" | "group:…" | "list" | "unknown"
    None set means: everyone can apply.
    """
    if not items:
        return None, "unknown"
    out: set[str] = set()
    scopes: list[str] = []
    for item in items:
        group = expand_group(item)
        if group is not None:
            if group:
                out |= group
            scopes.append(f"group:{re.sub(r'[^a-z ]+','',str(item).lower()).strip()}")
            continue
        c = canon(item)
        if c:
            out.add(c)
    if not out:
        return None, "worldwide"
    if any(s.startswith("group:") for s in scopes):
        return out, scopes[0]
    return out, "list"
