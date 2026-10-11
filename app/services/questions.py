"""Rule-based question categories and profile-backed factual values, ported from ResuSkill.

Keep the rules identical to ``resuskill_core.questions`` so both projects classify and fill
questions the same way. No AI is involved: a question is Factual, Sensitive-factual,
Sensitive, Open-ended or Unrecognized purely by keyword rules.

Differences from ResuSkill: the profile is our ``Profile`` model (or its dict), and the
graduation date comes from an education entry's ``end`` (there is no ``graduation`` field).

These rules are stricter than ResuSkill's:

- First-name and last-name questions have their own keys and no profile value. The profile
  stores one full name, and splitting it is guesswork, so the user types name parts. Questions
  saved earlier with the key ``name`` are read through their text the same way.
- Work authorization looks for an explicit country name before an abbreviation, and only the
  capitalized ``US`` (or ``U.S.``, ``USA``) counts as one: "Tell us ..." names no country.
- Factual fields require an explicit field request. Mentioning email, school or location
  inside a narrative or a commute question never supplies a profile value. Questions saved
  under the older keyword rules are re-read at startup (``answers.upgrade_stored_questions``).
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
_NEGATED = re.compile(r"\b(?:not|no|never|without)\b|n['’]t\b")
# Match the whole request, rather than a field word anywhere in it. In particular,
# "email marketing experience" and "a project at university" ask for narratives.
_FIELD_PREFIX = (
    r"(?:please )?(?:(?:enter|provide|list|give(?: us)?|share|tell us|what is|what's|"
    r"what are|which is)(?: your| the)? )?(?:your )?"
)
_OR = r" ?(?:/|or|and|&|,) ?"  # "Portfolio / Website", "City, State", "Major or field of study"
_SITE = r"(?:portfolio|(?:personal )?website)"
_STUDY = r"(?:major|field of study|area of study|concentration)"
# A degree level a graduation question may name: "Bachelor's degree graduation date", "... (undergraduate)".
_LEVEL = r"(?:bachelor'?s?|master'?s?|undergraduate|graduate|associate'?s?|ph\.?d\.?|doctoral|doctorate)"
_LEVEL_BEFORE = rf"(?:{_LEVEL} (?:degree )?)?"
_LEVEL_AFTER = rf"(?: \({_LEVEL}(?: degree)?\))?"
_FIELD_LABELS = [
    ("name", r"(?:(?:legal|preferred) )?(?:name|full name|first name|last name|given name|"
             r"family name|surname|forename|first (?:and|&|/) last name|first name / last name|"
             r"full legal name|legal full name)"),
    ("email", r"(?:(?:primary|personal|contact|work) )?e-?mail(?: address)?"),
    ("phone", r"(?:(?:primary|contact|mobile|cell|home) )?(?:phone|mobile|telephone|cell)(?: number| no)?"),
    ("linkedin", r"linkedin(?: profile)?(?: url| link)?"),
    ("github", r"github(?: profile)?(?: url| link)?"),
    ("portfolio", rf"(?:portfolio website|{_SITE}(?:{_OR}{_SITE})?)(?: url| link)?"),
    ("graduation_date", rf"{_LEVEL_BEFORE}(?:(?:expected|anticipated) )?(?:graduation(?: date| month(?: and | ?/ ?)year)?|"
                        rf"(?:date|month and year) of graduation){_LEVEL_AFTER}"),
    ("graduation_year", rf"{_LEVEL_BEFORE}(?:(?:expected|anticipated) )?(?:graduation year|year of graduation){_LEVEL_AFTER}"),
    ("start_date", r"(?:start|(?:earliest |available )?start date|availability(?: date)?)"),
    ("gpa", r"(?:(?:current|cumulative) )?(?:gpa|grade point(?: average)?)"),
    ("major", rf"(?:major|field of study|area of study)(?:{_OR}{_STUDY})?"),
    ("degree", r"(?:degree|degree level)"),
    ("school", r"(?:(?:school|university|college|institution)(?: name)?|"
               r"name of (?:your |the )?(?:school|university|college|institution))"),
    ("location", rf"(?:(?:current|home|residential) )?(?:location|city(?:{_OR}(?:state|province|country))?)"
                 r"(?: of residence)?"),
]
# Format hints employers append to a field label: "(MM/YYYY)", "(optional)", "(with country
# code)". Only these are removed before matching; any other parenthetical keeps the request
# unmatched, because "Name (of your reference)" is not asking for the candidate's name.
_LOCATION_PART = r"(?:city|state|province|country|region|zip(?: code)?|postal code)"
_TRAILING_HINT = re.compile(
    r"\s*\((?:optional|required|if (?:any|applicable)|url|link|full url|"
    r"(?:e\.g\.?|i\.e\.?|eg|ie|example|format)[:,]? [^()]*|"
    r"[mdy]{1,4}(?: ?[/.-] ?[mdy]{1,4}){1,2}|y{4}|y{2}|month ?(?:/|and) ?year|"
    r"(?:with|including|include) (?:the |your )?(?:country|area) code|"
    r"(?:out of|on a) [\d.]+(?: scale)?|"
    rf"{_LOCATION_PART}(?: ?(?:,|/|and|&) ?{_LOCATION_PART})+)\)$"
)
_FACTUAL = [(key, re.compile(_FIELD_PREFIX + label)) for key, label in _FIELD_LABELS]
_FACTUAL.extend([
    ("graduation_date", re.compile(
        r"when (?:(?:do|will) you (?:expect to )?graduate|did you graduate|"
        r"(?:is|was) your (?:expected )?graduation(?: date)?)"
    )),
    ("graduation_year", re.compile(r"(?:what|which) year (?:will|do|did) you (?:expect to )?graduate")),
    ("start_date", re.compile(
        r"(?:when (?:can|could|are) you (?:available to )?start|what is the earliest (?:date )?you can start)"
    )),
    ("school", re.compile(
        r"(?:which|what) (?:school|university|college|institution) "
        r"(?:do you attend|are you attending|did you attend|did you graduate from)"
    )),
    ("degree", re.compile(r"what degree (?:are you pursuing|do you hold|have you earned)")),
    ("location", re.compile(r"where are you (?:currently )?(?:located|based|living)")),
])
_RELOCATE = re.compile(r"\breloca\w*\b")
_OPEN = re.compile(
    r"^(why|what|how|describe|tell us|explain|share|walk us|give an example|please describe)\b|"
    r"\bwhy\b|tell us about|describe|cover letter|anything else|interest(?:s|ed)? you"
)
# Name parts: the profile has only the full name, so these are always answered by the user.
NAME_PARTS = ("first_name", "last_name")
_FIRST_NAME = re.compile(r"\b(?:first|given)\s+name\b|\bforenames?\b")
_LAST_NAME = re.compile(r"\b(?:last|family)\s+name\b|\bsurname\b")
_BOTH_NAMES = re.compile(r"\bfirst\s+(?:and|&|/)\s+(?:last|family)\s+names?\b")

# Places the country table would misread in a question. None marks a place that is not one
# country (a region, or a US state that shares a country's name): it makes the answer unknown.
_PLACES = country_names() | {
    "america": "US", "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "new mexico": "US", "new jersey": "US", "georgia": None,
    "north america": None, "south america": None, "latin america": None, "central america": None,
}
_LONGEST_PLACE = max(len(name.split()) for name in _PLACES)


def name_part(key: str, question: str) -> str | None:
    """``first_name`` or ``last_name`` when a name question asks for only that part, else None.

    Applies to the ``name`` key too, so questions saved before name parts had their own keys
    are read by their text and never answered with the full name.
    """
    if key in NAME_PARTS:
        return key
    if key != "name":
        return None
    q = norm_text(question)
    first, last = _FIRST_NAME.search(q), _LAST_NAME.search(q)
    if _BOTH_NAMES.search(q) or (first and last):
        return None  # asks for both parts together: the full name
    if first:
        return "first_name"
    if last:
        return "last_name"
    return None


def _field_request(q: str) -> str:
    """A normalized question without trailing punctuation, required markers or format hints."""
    q = q.rstrip(" ?.!:*")
    while match := _TRAILING_HINT.search(q):
        q = q[:match.start()].rstrip(" ?.!:*")
    return q


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
    request = _field_request(q)
    for key, regex in _FACTUAL:
        if regex.fullmatch(request):
            return FACTUAL, name_part(key, text) or key
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


GRADUATION_KEYS = ("graduation_date", "graduation_year")
# A date format a graduation question's label asks for: "(MM/YYYY)", "(DD.MM.YYYY)", "(YY)", "(Month/Year)".
_FORMAT_HINT = re.compile(r"\(\s*([mdy]{1,4}(?:\s*[/.-]\s*[mdy]{1,4}){0,2}|month\s*(?:/|and)\s*year)\s*\)")
_DEFAULT_FORMS = {"graduation_date": ["yyyy", "-", "mm"], "graduation_year": ["yyyy"]}
_UNREADABLE = ("This question asks for its date in a format ResuApply can't fill in. "
               "Answer it yourself, in the format the employer's form asks for.")
_PRECISION = ("This question asks for more than your profile holds: the end date of your education has no "
              "{part}. Add it to your profile, or answer the question yourself.")


# Degree levels, read in a question and in an education entry's degree.
_LEVELS = [
    re.compile(r"\bbachelor|\bundergrad|\bb\.? ?(?:s|a|sc|e|eng|tech|com|fa|arch|ed|sw|ba|bus)\b|\bbba\b|"
               r"\bll\.? ?b\b"),
    re.compile(r"\bmaster|\bm\.? ?(?:s|a|sc|e|eng|tech|fa|arch|ed|sw|ba|pa|ph|phil|com)\b|\bll\.? ?m\b|"
               r"\bgraduate (?:degree|program|school|studies)|\(graduate\)"),
    re.compile(r"\bph\.? ?d\b|\bd\.? ?phil\b|\bdoctor|\bed\.? ?d\b"),
    re.compile(r"\bassociate|\ba\.? ?a\.? ?s\b"),
]
_IN_PROGRESS = ("This question may be about a degree your profile shows as still in progress, with no end date. "
                "Add its expected end date to your profile, or answer the question yourself.")
_NO_LEVEL = "This question asks about a kind of degree your profile doesn't list. Answer it yourself."
_SEVERAL = ("Your profile has several degrees, and this question doesn't say which one it means. "
            "Answer it yourself.")


def _graduation(key: str, question: str, prof: dict) -> tuple[str | None, str | None]:
    """A graduation question's answer from the education end dates, or None with why (None: no date at all).

    A year question gets the year, which every stored date has; a date question the month and year,
    as "2027-05". A format hint in the label ("MM/YYYY", "DD.MM.YYYY", "YY") is followed; a date the
    profile holds less precisely than the format asks (no month, no day) is left to the user. With
    several degrees, only a question that names a degree level matching one of them, or degrees
    that all give the same answer, are answered: otherwise the question doesn't say which it means.
    """
    # A degree still in progress ("present") has no date to give, but may be the one asked about.
    dated = [e for e in prof.get("education") or [] if e.get("end")]
    if not dated:
        return None, None
    named = [level for level in _LEVELS if level.search(norm_text(question))]
    if len(named) == 1:
        matching = [e for e in dated if named[0].search(norm_text(e.get("degree") or ""))]
        unread = [e for e in dated if not any(level.search(norm_text(e.get("degree") or "")) for level in _LEVELS)]
        if matching:
            dated = matching
        elif not unread:
            return None, _NO_LEVEL  # every degree is clearly of another level
        # Otherwise a degree whose level can't be read ("Diploma", none given) may be the one asked
        # about: an only degree answers the question, and several are ambiguous unless they agree.
    if any(entry["end"] == "present" for entry in dated):
        return None, _IN_PROGRESS
    answers = {_graduation_answer(key, question, entry["end"]) for entry in dated}
    if len(answers) != 1:
        return None, _SEVERAL
    return answers.pop()


def _date_form(key: str, question: str) -> list[str]:
    """The parts of the date format a graduation question asks for: ["dd", "/", "mm", "/", "yy"].

    The last format hint in its label counts ("Month/Year" is "mm/yyyy"); without one, a year
    question asks for "yyyy" and a date question for "yyyy-mm".
    """
    hints = list(_FORMAT_HINT.finditer(norm_text(question)))
    if not hints:
        return _DEFAULT_FORMS[key]
    hint = re.sub(r"\s+", "", hints[-1].group(1))
    return re.findall(r"[mdy]+|[/.-]", "mm/yyyy" if hint.startswith("month") else hint)


def _graduation_answer(key: str, question: str, end: str) -> tuple[str | None, str | None]:
    """One end date written in the format the question asks for, or None with why."""
    year, month, day = (end.split("-") + ["", ""])[:3]
    written = []
    for part in _date_form(key, question):
        if part in "/.-":
            written.append(part)
        elif part in ("yyyy", "yy"):
            written.append(year if part == "yyyy" else year[2:])
        elif part in ("mm", "m", "dd", "d"):
            value = month if part[0] == "m" else day
            if not value:
                return None, _PRECISION.format(part="month" if part[0] == "m" else "day")
            written.append(value if len(part) == 2 else str(int(value)))
        else:  # "mmm", "yyy" and the like: not a format to guess at
            return None, _UNREADABLE
    return "".join(written), None


def graduation_note(key: str, question: str, profile: Profile | dict) -> str | None:
    """Why a graduation question isn't answered from a profile that has an education end date, or None."""
    if key not in GRADUATION_KEYS:
        return None
    return _graduation(key, question, profile.model_dump() if isinstance(profile, Profile) else profile)[1]


def factual_value(key: str, question: str, profile: Profile | dict) -> str | None:
    """The profile's value for a factual question, or None when missing."""
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    contact = prof.get("contact") or {}
    links = contact.get("links") or {}
    education = sorted(prof.get("education") or [], key=lambda e: e.get("end") or "", reverse=True)
    latest = education[0] if education else {}
    if name_part(key, question):
        return None  # typed by the user: the stored full name is never split
    if key in ("name", "email", "phone", "location"):
        return contact.get(key) or None
    if key in ("linkedin", "github", "portfolio"):
        return links.get(key) or None
    if key in GRADUATION_KEYS:
        return _graduation(key, question, prof)[0]
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
    if key in ("authorization", "sponsorship"):
        q = norm_text(question)
        # A stored Boolean answers only the positive, single-fact question.
        # Negation and combined authorization/sponsorship conditions need the
        # user's answer rather than a keyword-based guess at their meaning.
        if _NEGATED.search(q) or (_AUTH.search(q) and _SPONSOR.search(q)):
            return None
    if key == "authorization":
        record = _country(question, prof)
        return _yes_no(record.get("authorized")) if record else None
    if key == "sponsorship":
        record = _country(question, prof)
        return _yes_no(record.get("requires_sponsorship")) if record else None
    return None


def profile_gap(key: str, question: str, profile: Profile | dict) -> str | None:
    """The profile fact a factual question needs but the profile lacks, or None.

    Returns the factual key ("start_date"), or for work authorization and sponsorship the key
    and the country code ("authorization:CA"; the bare key when the question names no country
    and the profile has no country at all). None means the profile answers the question, or
    the question is never answered from the profile (a first or last name, a negated or
    combined authorization question, an unclear country), so adding a fact wouldn't help.
    """
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    if name_part(key, question) or factual_value(key, question, prof) is not None:
        return None
    if key in GRADUATION_KEYS:
        if _graduation(key, question, prof)[1] == _SEVERAL:
            return None  # the profile has the dates; the question must say which degree
        return "graduation_date"  # year and date questions both need the education end date
    if key not in ("authorization", "sponsorship"):
        return key
    q = norm_text(question)
    if _NEGATED.search(q) or (_AUTH.search(q) and _SPONSOR.search(q)):
        return None
    records = prof.get("authorization") or []
    code = question_country(question)
    if code is False:  # no country named: only a single record, or none at all, says which
        if not records:
            return key
        return f"{key}:{records[0]['country']}" if len(records) == 1 else None
    return f"{key}:{code}" if code else None
