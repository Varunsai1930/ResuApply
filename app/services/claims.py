"""Deterministic fabrication checks ported from ResuSkill's claim validator.

Numbers, technologies and credential categories must be supported by cited sources.
These checks do not establish semantic accuracy; the user reviews every draft.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from ..models import Job
from ..schemas.profile import Profile
from .skills import display, find_terms
from .text import norm_text

_SCALES = {"k": 3, "thousand": 3, "m": 6, "million": 6, "b": 9, "billion": 9, "hundred": 2}
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9.])[$€£₹]?(?P<number>\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:\s*(?P<scale>thousand|million|billion|hundred|[kmb])(?![A-Za-z]))?",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "zero": "0", "one": "1",
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "dozen": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000", "million": "1000000", "billion": "1000000000",
    "twice": "2", "doubled": "2", "tripled": "3", "quadrupled": "4", "halved": "0.5", "tenfold": "10",
}
_WORD_RE = re.compile(
    r"\b(?P<number>" + "|".join(_NUMBER_WORDS) + r")s?\b"
    r"(?:[\s-]+(?P<scale>hundred|thousand|million|billion)s?\b)?",
    re.IGNORECASE,
)


def _degree_abbreviation(abbreviations: str) -> str:
    # Uppercase short forms need nearby degree context; an explicit prefix also
    # permits lowercase forms in facts such as an education entry's degree field.
    return (
        rf"(?-i:{abbreviations})(?=\s+(?:graduates?|graduation|degree|students?|candidates?|program|in\b|of\b)|\s*(?:[·,;/)]|$))"
        rf"|(?:pursuing|earned|completed|holding|holds|degree)\s*:?\s+(?:(?:an?|the)\s+)?(?:{abbreviations})\b"
    )


_CREDENTIALS = {
    "patent": r"patent(?:s|ed)?", "award": r"award(?:s|ed|-winning)?",
    "doctorate": r"ph\.?\s?d\.?|doctorate|doctoral",
    "associate's degree": r"associate'?s|associates?\s+(?:of|degree)|a\.(?:a|s|as|sc)\.?|aas|" + _degree_abbreviation("AA|AS"),
    "bachelor's degree": r"bachelor'?s|bachelors?\s+of|b\.(?:s|a|sc|e|eng|tech)\.?|bsc|b\.?tech|beng|" + _degree_abbreviation("BS|BA|BE"),
    "master's degree": r"master'?s|masters?\s+of|m\.(?:s|a|sc|eng|tech)\.?|msc|m\.?tech|meng|" + _degree_abbreviation("MS|MA|ME"),
    "mba": r"mba", "degree": r"degree",
    "charterholder": r"charter\s?holders?|cfa charter",
    "publication": r"publish(?:ed|ing)?|publications?|peer[- ]reviewed",
    "honor": r"honou?rs?|honou?red|cum laude|dean'?s list|scholarship|fellowship|valedictorian",
}
_CREDENTIAL_RES = {name: re.compile(rf"(?<!\w)(?:{pat})(?!\w)", re.IGNORECASE) for name, pat in _CREDENTIALS.items()}
# Distinct professional qualifications never back each other, as an acronym or spelled out (ResuSkill's rule).
_PROFESSIONAL = {
    "CPA credential": r"cpa|certified public accountant",
    "PMP credential": r"pmp|project management professional",
    "CFA credential": r"cfa|chartered financial analyst",
    "CISSP credential": r"cissp|certified information systems security professional",
    "CCNA credential": r"cisco certified network associate|ccna",
}
# These and the generic certification and license claims are held only without progress wording.
_PROGRESS_AWARE = {**_PROFESSIONAL, "certification": r"certif(?:ied|ication|icate)s?",
                   "license": r"licen[cs](?:e|ed|es|ing)"}
_PROGRESS_AWARE_RES = {name: re.compile(rf"(?<!\w)(?:{pat})(?!\w)", re.IGNORECASE)
                       for name, pat in _PROGRESS_AWARE.items()}
# Wording that marks a credential as not yet held, when it sits beside the credential (``_credential_scopes``):
# "CPA, expected 2025" stays in progress and can't back a claim of holding it.
_PROGRESS_RE = re.compile(
    r"(?<!\w)(?:pursu\w*|candidates?|candidacy|preparing|studying|working toward\w*|enrolled|"
    r"in progress|expected|sitting for|registered for|eligib\w*|pending|awaiting|plann\w*|"
    r"level (?:iii|ii|i|[123]))(?!\w)", re.IGNORECASE)
_SHORT_REMARK_WORDS = 3
_CFA_LEVEL_RE = re.compile(r"(?<!\w)level (iii|ii|i|[123])(?!\w)", re.IGNORECASE)
_LEVELS = {"1": "I", "2": "II", "3": "III", "i": "I", "ii": "II", "iii": "III"}
# A sentence break: ; ! ? a line break, or a full stop before a capital or the end. A full stop after a
# single letter or a short title ("U.S.", "R.N.", "Sr.") is an abbreviation, not a break.
_CLAUSE_BREAK_RE = re.compile(r"[;!?\n]|(?<!\b[A-Za-z])(?<!\b(?:Sr|Jr|St|Dr|Mr|Ms|No|vs))\.(?=\s+[A-Z]|\s*$)")


def numbers(text: str) -> set[str]:
    found = set()
    spans = []
    for match in _NUMBER_RE.finditer(text or ""):
        found.add(_number_key(match.group("number"), match.group("scale")))
        spans.append(match.span())
    for match in _WORD_RE.finditer(text or ""):
        if any(start <= match.start() < end for start, end in spans):
            continue  # the numeric amount already includes its scale word
        found.add(_number_key(_NUMBER_WORDS[match.group("number").lower()], match.group("scale")))
    return found


def _number_key(raw: str, scale: str | None = None) -> str:
    value = Decimal(raw.replace(",", ""))
    if scale:
        parts = value.as_tuple()
        value = Decimal((parts.sign, parts.digits, parts.exponent + _SCALES[scale.lower()]))
    key = format(value, "f")
    return key.rstrip("0").rstrip(".") if "." in key else key


def _credential_scopes(clause: str):
    """Each comma-separated part of a clause (where a credential is named) with the wording that speaks of it.

    Progress wording counts in the part itself and in a short part right after it ("CPA, expected May
    2025"), not elsewhere in the sentence ("PMP certified, cut costs ahead of expected timelines").
    """
    parts = clause.split(",")
    for index, part in enumerate(parts):
        after = parts[index + 1] if index + 1 < len(parts) else ""
        short = len(re.findall(r"\w+", after)) <= _SHORT_REMARK_WORDS
        yield (f"{part},{after}" if short else part), part


def credentials(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'")
    found = {name for name, regex in _CREDENTIAL_RES.items() if regex.search(normalized)}
    # A credential, license or certification counts as held only when its clause has no progress
    # wording; a CFA claim also names its level, so "CFA Level I candidate" can't back "passed CFA Level II".
    for clause in _CLAUSE_BREAK_RE.split(normalized):
        for scope, mention in _credential_scopes(clause):
            in_progress = bool(_PROGRESS_RE.search(scope))
            for name, regex in _PROGRESS_AWARE_RES.items():
                if not regex.search(mention):
                    continue
                found.add(f"{name} (in progress)" if in_progress else name)
                if name == "CFA credential":
                    found.update(f"CFA Level {_LEVELS[level.lower()]}" for level in _CFA_LEVEL_RE.findall(scope))
    if found & {"associate's degree", "bachelor's degree", "master's degree", "doctorate", "mba"}:
        found.add("degree")
    return found


def claim_problems(text: str, source_texts: list[str], allowed_tech: set[str], extra_terms=()) -> list[str]:
    joined = "\n".join(source_texts)
    problems = []
    extra_numbers = numbers(text) - numbers(joined)
    if extra_numbers:
        problems.append(f"numbers/years not in its sources: {', '.join(sorted(extra_numbers))}")
    extra_tech = find_terms(text, extra_terms) - find_terms(joined, extra_terms) - allowed_tech
    if extra_tech:
        problems.append(f"technologies not in its sources: {', '.join(display(t) for t in sorted(extra_tech))}")
    extra_credentials = credentials(text) - credentials(joined)
    if extra_credentials:
        problems.append(f"credential claims not in its sources: {', '.join(sorted(extra_credentials))}")
    return problems


def detection_terms(profile: Profile | dict, job: Job | dict | None = None) -> tuple[str, ...]:
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    terms = [s["name"] for s in prof.get("skills") or []] + list(prof.get("skills_absent") or [])
    for section in ("experience", "projects"):
        for entry in prof.get(section) or []:
            terms.extend(entry.get("technologies") or [])
    requirements = job.requirements if isinstance(job, Job) else (job or {}).get("requirements", [])
    for req in requirements:
        crit = req.criterion_dict() if hasattr(req, "criterion_dict") else req.get("criterion")
        if crit and crit.get("type") == "skill":
            terms.extend(crit["skills"])
    return tuple(sorted(set(terms)))
