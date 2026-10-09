"""Rule-based question categories and profile-backed factual values, ported from ResuSkill.

Keep the rules identical to ``resuskill_core.questions`` so both projects classify and fill
questions the same way. No AI is involved: a question is Factual, Sensitive-factual,
Sensitive, Open-ended or Unrecognized purely by keyword rules.

Differences from ResuSkill: the profile is our ``Profile`` model (or its dict), and the
graduation date comes from an education entry's ``end`` (there is no ``graduation`` field).

One rule is stricter than ResuSkill's:

- Work authorization looks for an explicit country name before an abbreviation, and only the
  capitalized ``US`` (or ``U.S.``, ``USA``) counts as one: "Tell us ..." names no country.
"""

from __future__ import annotations

import re

from ..schemas.profile import Profile
from . import profile as profile_service
from .countries import country_names, words
from .text import norm_text

FACTUAL, SENSITIVE_FACTUAL, SENSITIVE, OPEN, UNKNOWN = "factual", "sensitive_factual", "sensitive", "open", "unknown"
CATEGORIES = (FACTUAL, SENSITIVE_FACTUAL, SENSITIVE, OPEN, UNKNOWN)
CATEGORY_LABELS = {
    FACTUAL: "Factual",
    SENSITIVE_FACTUAL: "Sensitive-factual",
    SENSITIVE: "Sensitive",
    OPEN: "Open-ended",
    UNKNOWN: "Unrecognized",
}
# Categories whose answers the user gives or confirms themselves: never drafted, never banked.
SENSITIVE_CATEGORIES = (SENSITIVE, SENSITIVE_FACTUAL)

_SENSITIVE = re.compile(
    r"\b(gender|sex|pronouns?|race|racial|ethnic\w*|hispanic|latin[oax]|veterans?|military status|"
    r"disabilit\w*|disabled|sexual orientation|lgbtq?\w*|transgender|religio\w*|marital|date of birth|"
    r"age|how old|criminal|convicted|conviction|felony|misdemeanou?r|background check|salary|"
    r"compensation|pay expectations?|desired pay|expected pay|current pay|attest\w*|certify that|"
    r"acknowledge|declaration|signature|sign here|agree to|consent)\b"
)
_SPONSOR = re.compile(r"\b(sponsor\w*|visa|h-?1b|work permit)\b")
_AUTH = re.compile(
    r"(authori[sz]ed to work|work authori[sz]ation|eligible to work|right to work|"
    r"legally (?:able|permitted|allowed|entitled) to work)"
)
_FACTUAL = [
    ("name", re.compile(r"\b(full name|first name|last name|legal name|your name|preferred name)\b")),
    ("email", re.compile(r"\be-?mail\b")),
    ("phone", re.compile(r"\b(phone|mobile|telephone)\b")),
    ("linkedin", re.compile(r"\blinkedin\b")),
    ("github", re.compile(r"\bgithub\b")),
    ("portfolio", re.compile(r"\b(portfolio|personal website|website)\b")),
    ("graduation_date", re.compile(r"\bgraduat\w*\b")),
    ("start_date", re.compile(r"\b(start date|available to start|earliest start|when can you start|availability)\b")),
    ("gpa", re.compile(r"\b(gpa|grade point)\b")),
    ("major", re.compile(r"\b(major|field of study|area of study)\b")),
    ("degree", re.compile(r"\bdegree\b")),
    ("school", re.compile(r"\b(school|university|college|institution)\b")),
    ("location", re.compile(r"\b(where are you (?:currently )?(?:located|based)|current location|city|location)\b")),
]
_RELOCATE = re.compile(r"\breloca\w*\b")
_OPEN = re.compile(
    r"^(why|what|how|describe|tell us|explain|share|walk us|give an example|please describe)\b|"
    r"\bwhy\b|tell us about|describe|cover letter|anything else|interest(?:s|ed)? you"
)
# Places the country table would misread in a question. None marks a place that is not one
# country (a region, or a US state that shares a country's name): it makes the answer unknown.
_PLACES = country_names() | {
    "america": "US", "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "new mexico": "US", "new jersey": "US", "georgia": None,
    "north america": None, "south america": None, "latin america": None, "central america": None,
}
_LONGEST_PLACE = max(len(name.split()) for name in _PLACES)


def classify(text: str) -> tuple[str, str | None]:
    """Return (category, factual key) using explicit keyword rules."""
    q = norm_text(text)
    if _SENSITIVE.search(q):
        return SENSITIVE, None
    if _SPONSOR.search(q) and re.search(r"\b(require|need|will you|do you)\b", q):
        return SENSITIVE_FACTUAL, "sponsorship"
    if _AUTH.search(q):
        return SENSITIVE_FACTUAL, "authorization"
    if _SPONSOR.search(q):
        return SENSITIVE_FACTUAL, "sponsorship"
    if _RELOCATE.search(q):
        return UNKNOWN, None
    for key, regex in _FACTUAL:
        if regex.search(q):
            return FACTUAL, key
    if _OPEN.search(q):
        return OPEN, None
    return UNKNOWN, None


_NAMED_PLACE = re.compile(r"\bin\s+(?:the\s+)?[A-Z]")


def _named_countries(question: str) -> tuple[set[str | None], bool]:
    """Codes of the places the question names (None for a place that isn't one country), and
    whether an all-capitals "US" left it unclear if the US is named.

    Names are matched longest first, so "U.S. Virgin Islands" is not also the US. "U.S." and
    "USA" are names in the country table; a bare "US" counts only where no name matched and
    only capitalized: "Tell us ..." names no country.
    """
    original = words(question)
    lowered = [word.casefold() for word in original]
    shouting = not any(ch.islower() for ch in question)
    found: set[str | None] = set()
    unclear = False
    i = 0
    while i < len(lowered):
        for size in range(min(_LONGEST_PLACE, len(lowered) - i), 0, -1):
            phrase = " ".join(lowered[i:i + size])
            if phrase in _PLACES:
                found.add(_PLACES[phrase])
                i += size
                break
        else:
            if original[i] == "US":
                if shouting:
                    unclear = True
                else:
                    found.add("US")
            i += 1
    return found, unclear


def question_country(question: str) -> str | None | bool:
    """The one country a question names: its code, None when unknown, False when it names none.

    Several countries, or a place that maps to no single country, are unknown. So is a place
    the table can't map, rather than being answered from another country's record.
    """
    named, unclear = _named_countries(question)
    if not named:
        return None if unclear or _NAMED_PLACE.search(question) else False
    if len(named) > 1 or None in named:
        return None
    return next(iter(named))


def _country(question: str, prof: dict) -> dict | None:
    records = prof.get("authorization") or []
    code = question_country(question)
    if code is False:  # no country identified: only a single record answers
        return records[0] if len(records) == 1 else None
    if code is None:
        return None
    return next((r for r in records if r["country"] == code), None)


def _yes_no(value) -> str | None:
    return {True: "Yes", False: "No"}.get(value)


def factual_value(key: str, question: str, profile: Profile | dict) -> str | None:
    """The profile's value for a factual question, or None when missing."""
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    contact = prof.get("contact") or {}
    links = contact.get("links") or {}
    education = sorted(prof.get("education") or [], key=lambda e: e.get("end") or "", reverse=True)
    latest = education[0] if education else {}
    if key in ("name", "email", "phone", "location"):
        return contact.get(key) or None
    if key in ("linkedin", "github", "portfolio"):
        return links.get(key) or None
    if key == "graduation_date":
        return profile_service.graduation_date(prof)
    if key == "start_date":
        return (prof.get("availability") or {}).get("start_date") or None
    if key == "school":
        return latest.get("institution") or None
    if key == "degree":
        return latest.get("degree") or None
    if key == "major":
        return latest.get("field") or None
    if key == "gpa":
        return latest.get("gpa") or None
    if key == "authorization":
        record = _country(question, prof)
        return _yes_no(record.get("authorized")) if record else None
    if key == "sponsorship":
        record = _country(question, prof)
        return _yes_no(record.get("requires_sponsorship")) if record else None
    return None
