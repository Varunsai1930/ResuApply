"""Job requirements: validation, stable IDs and saving.

Ported from ResuSkill's ``resuskill_core.jobs`` so both projects apply the same rules:

- Every requirement quotes a verbatim excerpt from the job description (compared after
  normalizing case, quotes and whitespace). If any excerpt fails, the whole set is
  rejected and nothing is saved.
- IDs are ``r1``, ``r2`` ... A requirement with the same text and excerpt as a saved one
  keeps its ID; new ones get a number never used before for this job.
- Evidence links and overrides survive a re-save only for requirements that did not change.

The same validation runs for requirements the user types and for AI proposals.
"""

from __future__ import annotations

import math
import re

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Job
from ..schemas.requirements import (
    CATEGORIES, CRITERION_TYPES, DEGREE_LEVELS, IMPORTANCE, Requirement,
)
from .countries import normalize_country
from .profile import FieldError
from .text import date_key, ends_before_start, is_valid_date, norm_text, today_key
from .transactions import write


# One job's checklist. Far more than any posting states, and well inside the editor's form limits.
MAX_REQUIREMENTS = 200
MAX_YEARS = 100  # years of experience a requirement can ask for


class RequirementsInvalid(Exception):
    """The proposed requirements break a rule. Nothing was saved."""

    def __init__(self, errors: list[FieldError]):
        super().__init__("Requirements rejected; nothing was saved.")
        self.errors = errors


class RequirementsConflict(Exception):
    """The full editor was opened against a different saved revision."""


def _field(item: dict, name: str) -> str | None:
    prefix = item.get("_form") if isinstance(item, dict) else None
    return f"{prefix}-{name}" if prefix else None


def _str_list(crit: dict, name: str, label: str, errors: list[FieldError], field: str | None) -> list[str]:
    """A list of non-empty strings; a bare string is an error, not a list of characters."""
    value = crit.get(name)
    if value in (None, []):
        return []
    if not isinstance(value, list):
        errors.append(FieldError(f"{label}: {name} must be a list", field))
        return []
    if any(not isinstance(v, str) for v in value):
        errors.append(FieldError(f"{label}: {name} must contain only text", field))
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _criterion_date(value, label: str, field: str | None, errors: list[FieldError]) -> str | None:
    """Reject malformed AI date fields before constructing the stored Pydantic shape."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value != "present" and is_valid_date(value):
            return value
    errors.append(FieldError(f"{label} must be YYYY, YYYY-MM or YYYY-MM-DD", field))
    return None


def _validate_criterion(crit, label: str, item: dict, errors: list[FieldError]) -> dict | None:
    if crit in (None, {}):
        return None
    if not isinstance(crit, dict):
        errors.append(FieldError(f"{label}: criterion must be an object or empty", _field(item, "ctype")))
        return None
    kind = crit.get("type")
    if kind not in CRITERION_TYPES:
        errors.append(FieldError(f"{label}: criterion type must be one of {', '.join(CRITERION_TYPES)}", _field(item, "ctype")))
        return None
    clean: dict = {"type": kind}
    if kind == "skill":
        skills = _str_list(crit, "skills", label, errors, _field(item, "skills"))
        if not skills:
            errors.append(FieldError(f"{label}: list at least one skill", _field(item, "skills")))
        match = crit.get("match") or "all"
        if match not in ("all", "any"):
            errors.append(FieldError(f"{label}: skill match must be all or any", _field(item, "match")))
        clean.update(skills=skills, match=match)
    elif kind == "degree":
        level = str(crit.get("level") or "").lower()
        if level not in DEGREE_LEVELS:
            errors.append(FieldError(f"{label}: degree level must be one of {', '.join(DEGREE_LEVELS)}", _field(item, "level")))
        status = crit.get("status") or "any"
        if status not in ("any", "completed", "pursuing"):
            errors.append(FieldError(f"{label}: degree status must be any, completed or pursuing", _field(item, "status")))
        clean.update(level=level, fields=_str_list(crit, "fields", label, errors, _field(item, "fields")), status=status)
    elif kind == "graduation_window":
        start = _criterion_date(crit.get("from"), f"{label}: graduation from date", _field(item, "from"), errors)
        end = _criterion_date(crit.get("to"), f"{label}: graduation to date", _field(item, "to"), errors)
        if not start and not end:
            errors.append(FieldError(f"{label}: a graduation window needs a from and/or to date", _field(item, "from")))
        elif ends_before_start(start, end):
            errors.append(FieldError(f"{label}: the graduation window ends before it starts", _field(item, "to")))
        clean.update({"from": start, "to": end})
    elif kind == "location":
        mode = crit.get("work_mode") or None
        if mode not in (None, "remote", "hybrid", "onsite"):
            errors.append(FieldError(f"{label}: work mode must be remote, hybrid or onsite", _field(item, "work_mode")))
        locations = _str_list(crit, "locations", label, errors, _field(item, "locations"))
        if not locations and not mode:
            errors.append(FieldError(f"{label}: a location criterion needs locations and/or a work mode", _field(item, "locations")))
        clean.update(locations=locations, work_mode=mode)
    elif kind == "authorization":
        try:
            country = normalize_country(crit.get("country"))
        except ValueError as exc:
            errors.append(FieldError(f"{label}: {exc}", _field(item, "country")))
            country = ""
        sponsorship = crit.get("sponsorship_available")
        if sponsorship is not None and not isinstance(sponsorship, bool):
            errors.append(FieldError(f"{label}: sponsorship available must be yes, no or unknown", _field(item, "sponsorship")))
            sponsorship = None
        clean.update(country=country, sponsorship_available=sponsorship)
    elif kind == "availability":
        start_by = _criterion_date(crit.get("start_by"), f"{label}: start-by", _field(item, "start_by"), errors)
        start_from = _criterion_date(crit.get("start_from"), f"{label}: start-from", _field(item, "start_from"), errors)
        if not start_by and not start_from:
            errors.append(FieldError(f"{label}: availability needs a start-by and/or start-from date", _field(item, "start_by")))
        elif ends_before_start(start_from, start_by):
            errors.append(FieldError(f"{label}: the start-by date is before the start-from date", _field(item, "start_by")))
        clean.update(start_by=start_by, start_from=start_from)
    elif kind == "years_experience":
        years = crit.get("years")
        # isfinite: float("nan") and float("inf") parse, and NaN even passes "years < 0".
        if (isinstance(years, bool) or not isinstance(years, (int, float)) or not math.isfinite(years)
                or not 0 <= years <= MAX_YEARS):
            errors.append(FieldError(f"{label}: years must be a number from 0 to {MAX_YEARS}", _field(item, "years")))
            years = 0
        clean.update(years=years, area=str(crit.get("area") or "").strip())
    return clean


def _key(text: str, excerpt: str) -> str:
    return f"{norm_text(text)}|{norm_text(excerpt)}"


def validate_requirements(job: Job, items) -> tuple[list[Requirement], int]:
    """Check proposed requirements against the job. Returns (requirements, new ID counter).

    Raises ``RequirementsInvalid`` listing every problem; nothing is partially accepted.
    """
    if isinstance(items, dict) and "requirements" in items:
        items = items["requirements"]
    if not isinstance(items, list):
        raise RequirementsInvalid([FieldError("Requirements must be a list")])
    if len(items) > MAX_REQUIREMENTS:
        raise RequirementsInvalid([FieldError(
            f"There are {len(items):,} requirements. Keep it to {MAX_REQUIREMENTS:,} or fewer: "
            "remove repeated or minor ones, then save."
        )])
    description = norm_text(job.description)
    errors: list[FieldError] = []
    clean: list[dict] = []
    used_ids: set[str] = set()
    previous = {_key(r.text, r.excerpt): r.id for r in job.requirements}
    saved_ids = {r.id for r in job.requirements}
    # Only IDs already used by this job may be supplied; anything else is treated as new.
    explicit = {str(i.get("id") or "").strip() for i in items if isinstance(i, dict)} & saved_ids
    counter = max([job.requirement_counter or 0] + [
        int(m.group(1)) for rid in saved_ids for m in [re.fullmatch(r"r(\d+)", rid)] if m
    ])
    for index, item in enumerate(items):
        label = f"Requirement {index + 1}"
        if not isinstance(item, dict):
            errors.append(FieldError(f"{label} must be an object"))
            continue
        text = str(item.get("text") or "").strip()
        excerpt = str(item.get("excerpt") or "").strip()
        category = item.get("category") or "other"
        importance = item.get("importance") or "unspecified"
        if text:
            label = f"{label} ({text[:40]})"
        else:
            errors.append(FieldError(f"{label}: text is required", _field(item, "text")))
        if category not in CATEGORIES:
            errors.append(FieldError(f"{label}: category must be one of {', '.join(CATEGORIES)}", _field(item, "category")))
            category = "other"
        if importance not in IMPORTANCE:
            errors.append(FieldError(f"{label}: importance must be one of {', '.join(IMPORTANCE)}", _field(item, "importance")))
            importance = "unspecified"
        if not excerpt:
            errors.append(FieldError(f"{label}: excerpt is required (copy the words from the description)", _field(item, "excerpt")))
        elif norm_text(excerpt) not in description:
            errors.append(FieldError(f"{label}: excerpt not found in the job description: {excerpt[:80]!r}", _field(item, "excerpt")))

        req_id = str(item.get("id") or "").strip()
        if req_id not in explicit or req_id in used_ids:
            reuse = previous.get(_key(text, excerpt))
            if reuse and reuse not in used_ids and reuse not in explicit:
                req_id = reuse
            else:
                counter += 1
                req_id = f"r{counter}"
        used_ids.add(req_id)
        crit = _validate_criterion(item.get("criterion"), label, item, errors)
        clean.append({"id": req_id, "text": text, "category": category, "importance": importance, "excerpt": excerpt, "criterion": crit})
    if errors:
        raise RequirementsInvalid(errors)
    return [Requirement.model_validate(r) for r in clean], counter


def set_requirements(session: Session, job: Job, items, base_revision: int | None = None) -> bool:
    """Validate and save the job's requirements. Returns False when nothing changed.

    The job revision increases when the requirements change. Evidence links and
    overrides are kept only for requirements that are unchanged.
    """
    reviewed_revision = job.revision if base_revision is None else base_revision
    with write(session, job):
        if reviewed_revision != job.revision:
            raise RequirementsConflict(
                "The job or requirements changed while you were editing. Your input is kept below. "
                "Compare it with the saved requirements and the current description before saving again."
            )
        reqs, counter = validate_requirements(job, items)
        if reqs == job.requirements:
            return False
        old = {r.id: r for r in job.requirements}
        unchanged = {r.id for r in reqs if old.get(r.id) == r}
        job.requirements = reqs
        job.evidence = {k: v for k, v in job.evidence.items() if k in unchanged}
        job.overrides = {k: v for k, v in job.overrides.items() if k in unchanged}
        job.requirement_counter = counter
        job.revision += 1
        job.updated_at = utcnow()
        return True


def requirement(job: Job, req_id: str) -> Requirement | None:
    return next((r for r in job.requirements if r.id == req_id), None)


def excerpt_found(job: Job, req: Requirement) -> bool:
    """False when the description was edited after the requirement was saved and no longer contains it."""
    return norm_text(req.excerpt) in norm_text(job.description)


def degree_level(degree: str) -> int | None:
    """Index into DEGREE_LEVELS for a free-text degree, or None when unrecognised."""
    text = norm_text(degree).replace(".", "").replace("'", "")
    patterns = [
        (3, ("phd", "doctor", "dphil", "doctorate")),
        (2, ("master", "ms ", "msc", "meng", "mtech", "mba", "ma ", "mca", "mphil")),
        (1, ("bachelor", "bs ", "bsc", "ba ", "beng", "btech", "be ", "bca", "bba", "ab ")),
        (0, ("associate", "aa ", "as ")),
    ]
    padded = f"{text} "
    for level, needles in patterns:
        if any(padded.startswith(n) or f" {n}" in f" {padded}" for n in needles):
            return level
    return None


def is_future(date_value) -> bool:
    key = date_key(date_value, end_of_period=True)
    return bool(key) and key > today_key()
