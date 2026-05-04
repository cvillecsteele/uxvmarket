"""Deterministic post-processing of free-text fields the agent emits.

Vendors describe themselves with wildly inconsistent strings: `US`, `USA`,
`U.S.A.`, `America`; `VA`, `Virginia`, `va`. Asking the LLM to normalize is
unreliable and burns tokens. Doing it here is cheap, testable, and
auditable.

Rules:
- Match against a regex dictionary; on hit, return the canonical full
  English form.
- On miss, return the input stripped of surrounding whitespace —
  unknown values pass through untouched (better to keep weird-but-real
  data than overwrite it).
"""

from __future__ import annotations

import re


# (regex matched against the lowercased+stripped input, canonical name).
# The first matching pattern wins.
_COUNTRY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(united\s+states(\s+of\s+america)?|usa?|u\.?s\.?a?\.?|america)$"), "United States"),
    (re.compile(r"^(united\s+kingdom|u\.?k\.?|britain|great\s+britain|england|scotland|wales|northern\s+ireland)$"), "United Kingdom"),
    (re.compile(r"^(germany|deutschland|federal\s+republic\s+of\s+germany)$"), "Germany"),
    (re.compile(r"^(france|république\s+française|french\s+republic)$"), "France"),
    (re.compile(r"^(italy|italia)$"), "Italy"),
    (re.compile(r"^(spain|españa|espana)$"), "Spain"),
    (re.compile(r"^(netherlands|holland|the\s+netherlands|nederland)$"), "Netherlands"),
    (re.compile(r"^(switzerland|schweiz|suisse|svizzera)$"), "Switzerland"),
    (re.compile(r"^(sweden|sverige)$"), "Sweden"),
    (re.compile(r"^(norway|norge)$"), "Norway"),
    (re.compile(r"^(denmark|danmark)$"), "Denmark"),
    (re.compile(r"^(finland|suomi)$"), "Finland"),
    (re.compile(r"^(poland|polska)$"), "Poland"),
    (re.compile(r"^(czech\s+republic|czechia)$"), "Czech Republic"),
    (re.compile(r"^(belgium|belgië|belgique)$"), "Belgium"),
    (re.compile(r"^(ireland|éire|eire|republic\s+of\s+ireland)$"), "Ireland"),
    (re.compile(r"^(portugal)$"), "Portugal"),
    (re.compile(r"^(austria|österreich|osterreich)$"), "Austria"),
    (re.compile(r"^(greece|hellas|hellenic\s+republic)$"), "Greece"),
    (re.compile(r"^(turkey|türkiye|turkiye|republic\s+of\s+türkiye)$"), "Turkey"),
    (re.compile(r"^(israel|state\s+of\s+israel)$"), "Israel"),
    (re.compile(r"^(estonia|eesti)$"), "Estonia"),
    (re.compile(r"^(latvia|latvija)$"), "Latvia"),
    (re.compile(r"^(lithuania|lietuva)$"), "Lithuania"),
    (re.compile(r"^(romania|românia)$"), "Romania"),
    (re.compile(r"^(slovakia|slovak\s+republic)$"), "Slovakia"),
    (re.compile(r"^(slovenia)$"), "Slovenia"),
    (re.compile(r"^(hungary|magyarország)$"), "Hungary"),
    (re.compile(r"^(bulgaria|българия)$"), "Bulgaria"),

    (re.compile(r"^(japan|日本|nihon|nippon)$"), "Japan"),
    (re.compile(r"^(china|prc|people'?s\s+republic\s+of\s+china)$"), "China"),
    (re.compile(r"^(south\s+korea|korea|republic\s+of\s+korea|rok)$"), "South Korea"),
    (re.compile(r"^(north\s+korea|dprk|democratic\s+people'?s\s+republic\s+of\s+korea)$"), "North Korea"),
    (re.compile(r"^(india|bharat)$"), "India"),
    (re.compile(r"^(taiwan|roc|chinese\s+taipei)$"), "Taiwan"),
    (re.compile(r"^(singapore|republic\s+of\s+singapore)$"), "Singapore"),
    (re.compile(r"^(malaysia)$"), "Malaysia"),
    (re.compile(r"^(thailand)$"), "Thailand"),
    (re.compile(r"^(vietnam|viet\s+nam)$"), "Vietnam"),
    (re.compile(r"^(indonesia)$"), "Indonesia"),
    (re.compile(r"^(philippines|republic\s+of\s+the\s+philippines)$"), "Philippines"),
    (re.compile(r"^(australia|aus|commonwealth\s+of\s+australia)$"), "Australia"),
    (re.compile(r"^(new\s+zealand|n\.?z\.?|aotearoa)$"), "New Zealand"),

    (re.compile(r"^(saudi\s+arabia|kingdom\s+of\s+saudi\s+arabia|ksa)$"), "Saudi Arabia"),
    (re.compile(r"^(united\s+arab\s+emirates|u\.?a\.?e\.?|emirates)$"), "United Arab Emirates"),
    (re.compile(r"^(iran|islamic\s+republic\s+of\s+iran|persia)$"), "Iran"),
    (re.compile(r"^(iraq|republic\s+of\s+iraq)$"), "Iraq"),
    (re.compile(r"^(egypt|arab\s+republic\s+of\s+egypt|misr)$"), "Egypt"),
    (re.compile(r"^(qatar|state\s+of\s+qatar)$"), "Qatar"),
    (re.compile(r"^(jordan|hashemite\s+kingdom\s+of\s+jordan)$"), "Jordan"),

    (re.compile(r"^(canada)$"), "Canada"),
    (re.compile(r"^(mexico|méxico|united\s+mexican\s+states)$"), "Mexico"),
    (re.compile(r"^(brazil|brasil)$"), "Brazil"),
    (re.compile(r"^(argentina|argentine\s+republic)$"), "Argentina"),
    (re.compile(r"^(chile|republic\s+of\s+chile)$"), "Chile"),
    (re.compile(r"^(colombia|republic\s+of\s+colombia)$"), "Colombia"),
    (re.compile(r"^(peru|perú|republic\s+of\s+peru)$"), "Peru"),

    (re.compile(r"^(south\s+africa|rsa|republic\s+of\s+south\s+africa)$"), "South Africa"),
    (re.compile(r"^(nigeria|federal\s+republic\s+of\s+nigeria)$"), "Nigeria"),
    (re.compile(r"^(kenya|republic\s+of\s+kenya)$"), "Kenya"),
    (re.compile(r"^(morocco|kingdom\s+of\s+morocco)$"), "Morocco"),

    (re.compile(r"^(russia|russian\s+federation|r\.?f\.?)$"), "Russia"),
    (re.compile(r"^(ukraine)$"), "Ukraine"),
    (re.compile(r"^(belarus|byelorussia)$"), "Belarus"),
]


def normalize_country(value: str) -> str:
    """Normalize a free-text country string to a canonical English name.

    Returns the input stripped of whitespace if no pattern matches —
    unknown values pass through.
    """
    if not value:
        return value
    s = value.strip().rstrip(".").lower()
    s = re.sub(r"\s+", " ", s)
    for pattern, canonical in _COUNTRY_PATTERNS:
        if pattern.fullmatch(s):
            return canonical
    return value.strip()


_US_STATE_ABBREV: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_STATE_NAMES_UPPER: dict[str, str] = {v.upper(): v for v in _US_STATE_ABBREV.values()}


def normalize_us_state(value: str | None) -> str | None:
    """Normalize a US state abbreviation or name to the canonical full name.

    Returns the input unchanged if it is not a recognised US state — leaves
    Canadian provinces, German Bundesländer, etc. untouched.
    """
    if not value:
        return value
    stripped = value.strip().rstrip(".")
    upper = stripped.upper()
    if upper in _US_STATE_ABBREV:
        return _US_STATE_ABBREV[upper]
    if upper in _US_STATE_NAMES_UPPER:
        return _US_STATE_NAMES_UPPER[upper]
    return stripped
