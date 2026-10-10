"""Text normalization, partial dates and hashing, ported from ResuSkill's ``resuskill_core.util``.

Keep these identical to ResuSkill so both projects apply the same rules.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime, timezone

from pydantic import BaseModel

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", " ": " ", "•": " ", "·": " ",
}


def norm_text(text: str) -> str:
    """Casefold, unify quotes/dashes and collapse whitespace for substring checks."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(_QUOTES.get(ch, ch) for ch in text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# Partial dates are stored as "YYYY", "YYYY-MM" or "YYYY-MM-DD"; "present" marks ongoing.
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def is_valid_date(value) -> bool:
    if value in (None, ""):
        return True
    if value == "present":
        return True
    match = _DATE_RE.match(str(value))
    if not match:
        return False
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else 1
    day = int(match.group(3)) if match.group(3) else 1
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def date_key(value, end_of_period: bool = False) -> tuple[int, int, int] | None:
    """Comparable tuple for a partial date. Missing parts fill to the start or end of the period."""
    if value in (None, ""):
        return None
    if value == "present":
        return today_key()
    match = _DATE_RE.match(str(value))
    if not match or not is_valid_date(value):
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else (12 if end_of_period else 1)
    day = int(match.group(3)) if match.group(3) else (monthrange(year, month)[1] if end_of_period else 1)
    return (year, month, day)


def ends_before_start(start, end) -> bool:
    """Whether a range of partial dates ends before it begins ("2026-05" to "2026" is fine).

    False when either side is missing or invalid: those are reported on their own.
    """
    first, last = date_key(start), date_key(end, end_of_period=True)
    return first is not None and last is not None and last < first


def today_key() -> tuple[int, int, int]:
    today = datetime.now(timezone.utc)
    return (today.year, today.month, today.day)


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(data) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- control characters

_KEEP = {"\t", "\n", "\r"}
_LINE_BREAKS = {"\x0b": "\n", "\x0c": "\n"}


def clean_text(value: str) -> str:
    """``value`` without control characters, except tab, newline and carriage return.

    Vertical tab and form feed (line breaks in some word processors) become newlines; every
    other control character (Unicode category Cc, including NUL and DEL) is removed. Every
    submitted form value goes through this (``app.input_cleaning``).
    """
    if value.isprintable():
        return value
    return "".join(_LINE_BREAKS.get(ch, ch) for ch in value
                   if ch in _KEEP or ch in _LINE_BREAKS or unicodedata.category(ch) != "Cc")


def without_controls(value):
    """``value`` (text, or lists, dicts and Pydantic models of it) as the cleaned form would send it.

    Text saved before forms were cleaned can still hold control characters. The editors compare
    a submission with this cleaned copy of what is stored, so re-saving an unchanged form isn't
    a change that raises a revision or drops evidence.
    """
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [without_controls(v) for v in value]
    if isinstance(value, dict):
        return {k: without_controls(v) for k, v in value.items()}
    if isinstance(value, BaseModel):
        return type(value).model_validate(without_controls(value.model_dump(by_alias=True)))
    return value
