"""Text normalization and partial dates, ported from ResuSkill's ``resuskill_core.util``.

Keep these identical to ResuSkill so both projects apply the same rules.
"""

from __future__ import annotations

import re
import unicodedata

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
    month = match.group(2)
    return month is None or 1 <= int(month) <= 12
