"""The canonical candidate profile: validation, stable IDs, diff, revision and save.

Ported from ResuSkill's ``resuskill_core.profile`` so both projects follow the same rules:

- Every education, experience and project entry, every bullet and every certification has
  a stable ID (``exp-1``, ``exp-1-b2``, ``cert-1``) that survives edits.
- IDs come from per-prefix counters that only grow, so a deleted ID is never reused.
- The revision increases only when the content really changes.
- Only the user changes the profile: the web form is reviewed as a diff before saving.

Differences from ResuSkill, all stricter:
- An ``id`` supplied on input is kept only if it already exists in the saved profile (in
  the same section, or the same entry for bullets). Unknown IDs are treated as new, so a
  stale or edited form can never resurrect a deleted ID.
- Unknown fields are dropped with a warning instead of being stored as-is, because the
  stored shape is validated by ``app.schemas.profile.Profile``.
- Errors carry the form field they belong to, so the form can highlight it.

Input is a plain dict shaped like ResuSkill's profile JSON. Entries, bullets,
certifications and authorization rows may carry a ``_form`` key (the form field prefix,
e.g. ``experience-3``); it is used only for error locations and is never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, Candidate
from ..schemas.profile import Profile
from .countries import normalize_country
from .skills import canon, display
from .text import ends_before_start, is_valid_date, norm_text

ENTRY_SECTIONS = {"education": "edu", "experience": "exp", "projects": "proj"}
NAME_FIELDS = {"education": "institution", "experience": "organization", "projects": "name"}
ENTRY_FIELDS = {
    "education": ("institution", "degree", "field", "start", "end", "gpa"),
    "experience": ("organization", "title", "location", "start", "end"),
    "projects": ("name", "role", "link", "start", "end"),
}
SECTION_LABELS = {"education": "Education", "experience": "Experience", "projects": "Project"}
FIELD_LABELS = {
    "institution": "institution", "organization": "organization", "name": "name",
    "start": "start date", "end": "end date",
}
LINK_KEYS = ("linkedin", "github", "portfolio")
WORK_MODES = {"remote", "hybrid", "onsite", "any"}
TOP_LEVEL = {
    "contact", "summary", "education", "experience", "projects", "skills", "skills_absent",
    "certifications", "preferences", "availability", "authorization", "_meta",
}
DATE_HINT = 'YYYY, YYYY-MM, YYYY-MM-DD or "present"'
LINK_LABELS = {"linkedin": "LinkedIn", "github": "GitHub", "portfolio": "Portfolio"}
LINK_HINT = "a web address, such as github.com/you or https://example.com"
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")


def is_web_link(value: str) -> bool:
    """Whether a profile link is a web address: http(s)://…, or a bare one like github.com/you.

    Other schemes (``javascript:``, ``data:``, ``mailto:``) are refused, so a link is safe to
    show as clickable. "example.com:8080/x" is a host and port, not a scheme.
    """
    match = _SCHEME.match(value)
    if not match:
        return True
    scheme = match.group(1).lower()
    return scheme in ("http", "https") or "." in scheme


# ---------------------------------------------------------------- errors and results

@dataclass(frozen=True)
class FieldError:
    message: str
    field: str | None = None  # form field name to highlight, when known


class ProfileInvalid(Exception):
    """The proposed profile breaks a rule. Nothing was saved."""

    def __init__(self, errors: list[FieldError], warnings: list[str] | None = None):
        super().__init__("Profile is invalid; nothing was saved.")
        self.errors = errors
        self.warnings = warnings or []


class StaleReview(Exception):
    """The saved profile changed after the reviewed diff was produced."""


@dataclass(frozen=True)
class Change:
    kind: Literal["added", "changed", "removed"]
    path: str
    before: str | None = None
    after: str | None = None

    @property
    def line(self) -> str:
        """ResuSkill-style one-line summary: ``+ path: after``, ``~ path: a -> b``, ``- path: before``."""
        if self.kind == "added":
            return f"+ {self.path}: {self.after}"
        if self.kind == "removed":
            return f"- {self.path}: {self.before}"
        return f"~ {self.path}: {self.before} -> {self.after}"


@dataclass
class Normalized:
    profile: Profile
    id_counters: dict[str, int]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    normalized: Normalized
    changes: list[Change]
    base_revision: int  # 0 when no profile is saved yet

    @property
    def has_changes(self) -> bool:
        return bool(self.changes) or self.is_new

    @property
    def is_new(self) -> bool:
        return self.base_revision == 0


@dataclass
class SaveResult:
    candidate: Candidate
    changes: list[Change]
    warnings: list[str]
    saved: bool  # False when nothing changed (revision kept)


# ---------------------------------------------------------------- helpers

def _clean_str(value) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _as_list(value, label: str, errors: list[FieldError], field_name: str | None = None) -> list:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        errors.append(FieldError(f"{label} must be a list", field_name))
        return []
    return value


def _dump(profile: Profile | dict | None) -> dict:
    if profile is None:
        return {}
    return profile.model_dump() if isinstance(profile, Profile) else profile


def _entry_key(section: str, entry: dict) -> str:
    if section == "education":
        parts = (entry.get("institution"), entry.get("degree"))
    elif section == "experience":
        parts = (entry.get("organization"), entry.get("title"), entry.get("start"))
    else:
        parts = (entry.get("name"),)
    return "|".join(norm_text(_clean_str(p)) for p in parts)


def _entry_label(section: str, index: int, entry: dict) -> str:
    name = _clean_str(entry.get(NAME_FIELDS[section]))
    base = f"{SECTION_LABELS[section]} {index + 1}"
    return f"{base} ({name})" if name else base


def _form_field(raw: dict, name: str) -> str | None:
    prefix = raw.get("_form") if isinstance(raw, dict) else None
    return f"{prefix}-{name}" if prefix else None


def _next_id(counters: dict[str, int], prefix: str) -> str:
    counters[prefix] = counters.get(prefix, 0) + 1
    return f"{prefix}-{counters[prefix]}"


def _seed_counters(current: dict, stored: dict[str, int] | None) -> dict[str, int]:
    """Start from the stored counters and make sure they cover every ID in the saved profile."""
    counters = {k: int(v) for k, v in (stored or {}).items()}

    def bump(key: str, number: int) -> None:
        counters[key] = max(counters.get(key, 0), number)

    for section, prefix in ENTRY_SECTIONS.items():
        for entry in current.get(section) or []:
            match = re.fullmatch(rf"{prefix}-(\d+)", str(entry.get("id", "")))
            if match:
                bump(prefix, int(match.group(1)))
            for bullet in entry.get("bullets") or []:
                match = re.fullmatch(rf"({re.escape(str(entry.get('id')))}-b)(\d+)", str(bullet.get("id", "")))
                if match:
                    bump(match.group(1), int(match.group(2)))
    for cert in current.get("certifications") or []:
        match = re.fullmatch(r"cert-(\d+)", str(cert.get("id", "")))
        if match:
            bump("cert", int(match.group(1)))
    return counters


def _parse_tristate(value, label: str, field_name: str | None, errors: list[FieldError]) -> bool | None:
    if value in (True, False, None):
        return value
    errors.append(FieldError(f"{label} must be yes, no or unknown", field_name))
    return None


# ---------------------------------------------------------------- normalize

def normalize(
    data: dict,
    current: Profile | dict | None = None,
    id_counters: dict[str, int] | None = None,
) -> Normalized:
    """Validate a proposed profile and assign stable IDs.

    ``current`` is the saved profile (if any) and ``id_counters`` its stored counters.
    Raises ``ProfileInvalid`` with every problem found; nothing is partially applied.
    """
    if not isinstance(data, dict):
        raise ProfileInvalid([FieldError("Profile must be an object")])
    errors: list[FieldError] = []
    warnings: list[str] = []
    current = _dump(current)
    counters = _seed_counters(current, id_counters)
    prof: dict = {}

    # Contact and summary
    contact = data.get("contact") or {}
    if not isinstance(contact, dict):
        errors.append(FieldError("Contact must be an object"))
        contact = {}
    raw_links = contact.get("links") or {}
    if not isinstance(raw_links, dict):
        errors.append(FieldError("Contact links must be an object"))
        raw_links = {}
    for key in raw_links:
        if key not in LINK_KEYS and _clean_str(raw_links[key]):
            warnings.append(f"Unknown link {key!r} ignored")
    prof["contact"] = {k: _clean_str(contact.get(k)) for k in ("name", "email", "phone", "location")} | {
        "links": {k: _clean_str(raw_links.get(k)) for k in LINK_KEYS}
    }
    for key, link in prof["contact"]["links"].items():
        if link and not is_web_link(link):
            errors.append(FieldError(f"{LINK_LABELS[key]} link must be {LINK_HINT} (got {link!r})", f"contact-links-{key}"))
    if not prof["contact"]["name"]:
        errors.append(FieldError("Name is required", "contact-name"))
    email = prof["contact"]["email"]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append(FieldError(f"Email looks invalid: {email!r}", "contact-email"))
    prof["summary"] = _clean_str(data.get("summary"))

    # Education, experience and projects
    for section, prefix in ENTRY_SECTIONS.items():
        current_entries = [e for e in current.get(section) or [] if isinstance(e, dict)]
        current_by_id = {e.get("id"): e for e in current_entries}
        current_by_key = {_entry_key(section, e): e for e in current_entries}
        raw_entries = _as_list(data.get(section), SECTION_LABELS[section], errors, section)
        # IDs the input explicitly keeps; key-based matching must not hand these to another entry.
        explicit_ids = {
            _clean_str(r.get("id")) for r in raw_entries
            if isinstance(r, dict) and _clean_str(r.get("id")) in current_by_id
        }
        seen_ids: set[str] = set()
        entries = []
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                errors.append(FieldError(f"{SECTION_LABELS[section]} {index + 1} must be an object"))
                continue
            label = _entry_label(section, index, raw)
            entry: dict = {}
            for name in ENTRY_FIELDS[section]:
                entry[name] = _clean_str(raw.get(name))
            for name in raw:
                if name not in ENTRY_FIELDS[section] and name not in ("id", "bullets", "technologies", "_form"):
                    warnings.append(f"{label}: unknown field {name!r} ignored")
            for name in ("start", "end"):
                if entry[name].lower() == "present":
                    entry[name] = "present"
                if not is_valid_date(entry[name]):
                    errors.append(FieldError(
                        f"{label}: {FIELD_LABELS[name]} must be {DATE_HINT} (got {entry[name]!r})",
                        _form_field(raw, name),
                    ))
            if entry.get("link") and not is_web_link(entry["link"]):
                errors.append(FieldError(f"{label}: link must be {LINK_HINT} (got {entry['link']!r})",
                                         _form_field(raw, "link")))
            # "present" as the end is always accepted: a role can start in the future.
            if entry["end"] != "present" and ends_before_start(entry["start"], entry["end"]):
                errors.append(FieldError(f"{label}: end date is before the start date", _form_field(raw, "end")))
            name_field = NAME_FIELDS[section]
            if not entry[name_field]:
                errors.append(FieldError(f"{label}: {FIELD_LABELS[name_field]} is required", _form_field(raw, name_field)))

            supplied = _clean_str(raw.get("id"))
            if supplied in current_by_id and supplied not in seen_ids:
                entry_id = supplied
            else:
                previous = current_by_key.get(_entry_key(section, entry))
                reusable = previous and previous["id"] not in seen_ids and previous["id"] not in explicit_ids
                entry_id = previous["id"] if reusable else _next_id(counters, prefix)
            seen_ids.add(entry_id)
            previous = current_by_id.get(entry_id)

            if section != "education":
                techs: list[str] = []
                tech_keys: set[str] = set()
                for tech in _as_list(raw.get("technologies"), f"{label}: technologies", errors, _form_field(raw, "technologies")):
                    tech = _clean_str(tech)
                    if tech and canon(tech) not in tech_keys:
                        tech_keys.add(canon(tech))
                        techs.append(display(tech))
                entry["technologies"] = techs

            entry["bullets"] = _bullets(entry_id, raw, previous, label, counters, errors)
            entries.append({"id": entry_id, **entry})
        prof[section] = entries

    # Skills and confirmed-absent skills
    skills = []
    seen_skills: set[str] = set()
    for index, skill in enumerate(_as_list(data.get("skills"), "Skills", errors, "skills")):
        if isinstance(skill, str):
            skill = {"name": skill}
        if not isinstance(skill, dict) or not _clean_str(skill.get("name")):
            errors.append(FieldError(f"Skill {index + 1} needs a name", "skills"))
            continue
        key = canon(skill["name"])
        if key in seen_skills:
            warnings.append(f"Duplicate skill {skill['name']!r} ignored")
            continue
        seen_skills.add(key)
        skills.append({"name": display(_clean_str(skill["name"])), "category": _clean_str(skill.get("category")) or "Skills"})
    prof["skills"] = skills

    absent = []
    absent_keys: set[str] = set()
    for name in _as_list(data.get("skills_absent"), "Skills you don't have", errors, "skills_absent"):
        name = _clean_str(name)
        if not name or canon(name) in absent_keys:
            continue
        if canon(name) in seen_skills:
            errors.append(FieldError(f"{name!r} is listed both as a skill and as a skill you don't have", "skills_absent"))
        absent_keys.add(canon(name))
        absent.append(display(name))
    prof["skills_absent"] = absent

    # Certifications
    current_certs = {c.get("id") for c in current.get("certifications") or []}
    certs = []
    used_cert_ids: set[str] = set()
    for index, raw in enumerate(_as_list(data.get("certifications"), "Certifications", errors, "certifications")):
        if not isinstance(raw, dict):
            errors.append(FieldError(f"Certification {index + 1} must be an object"))
            continue
        cert = {k: _clean_str(raw.get(k)) for k in ("name", "issuer", "date")}
        if not any(cert.values()):
            continue
        label = f"Certification {index + 1}" + (f" ({cert['name']})" if cert["name"] else "")
        if not cert["name"]:
            errors.append(FieldError(f"{label}: name is required", _form_field(raw, "name")))
        if not is_valid_date(cert["date"]) or cert["date"] == "present":
            errors.append(FieldError(f"{label}: date must be YYYY, YYYY-MM or YYYY-MM-DD (got {cert['date']!r})", _form_field(raw, "date")))
        supplied = _clean_str(raw.get("id"))
        cert_id = supplied if supplied in current_certs and supplied not in used_cert_ids else _next_id(counters, "cert")
        used_cert_ids.add(cert_id)
        certs.append({"id": cert_id, **cert})
    prof["certifications"] = certs

    # Preferences and availability
    prefs = data.get("preferences") or {}
    work_mode = _clean_str(prefs.get("work_mode")).lower() or None
    if work_mode not in WORK_MODES | {None}:
        errors.append(FieldError("Work mode must be remote, hybrid, onsite or any", "preferences-work_mode"))
        work_mode = None
    prof["preferences"] = {
        "roles": [_clean_str(r) for r in _as_list(prefs.get("roles"), "Preferred roles", errors, "preferences-roles") if _clean_str(r)],
        "locations": [_clean_str(r) for r in _as_list(prefs.get("locations"), "Preferred locations", errors, "preferences-locations") if _clean_str(r)],
        "work_mode": work_mode,
    }

    avail = data.get("availability") or {}
    start = _clean_str(avail.get("start_date"))
    if start and (not is_valid_date(start) or start == "present"):
        errors.append(FieldError(f"Available-from date must be YYYY, YYYY-MM or YYYY-MM-DD (got {start!r})", "availability-start_date"))
    prof["availability"] = {"start_date": start, "notes": _clean_str(avail.get("notes"))}

    # Work authorization: per-country facts, unknown unless the user says otherwise
    auths = []
    countries: set[str] = set()
    for index, raw in enumerate(_as_list(data.get("authorization"), "Work authorization", errors, "authorization")):
        if not isinstance(raw, dict):
            errors.append(FieldError(f"Work authorization row {index + 1} must be an object"))
            continue
        raw_country = raw.get("country")
        if raw_country in (None, "") or (isinstance(raw_country, str) and not raw_country.strip()):
            errors.append(FieldError(f"Work authorization row {index + 1} needs a country", _form_field(raw, "country")))
            continue
        try:
            country = normalize_country(raw_country)
        except ValueError as exc:
            errors.append(FieldError(str(exc), _form_field(raw, "country")))
            continue
        if country in countries:
            errors.append(FieldError(f"Work authorization for {country} is listed twice", _form_field(raw, "country")))
        countries.add(country)
        auths.append({
            "country": country,
            "authorized": _parse_tristate(raw.get("authorized"), f"Authorized to work in {country}", _form_field(raw, "authorized"), errors),
            "requires_sponsorship": _parse_tristate(raw.get("requires_sponsorship"), f"Requires sponsorship in {country}", _form_field(raw, "requires_sponsorship"), errors),
        })
    prof["authorization"] = auths

    for key in data:
        if key not in TOP_LEVEL:
            warnings.append(f"Unknown top-level field {key!r} ignored")

    if errors:
        raise ProfileInvalid(errors, warnings)
    return Normalized(Profile.model_validate(prof), counters, warnings)


def _bullets(entry_id: str, raw: dict, previous: dict | None, label: str, counters: dict[str, int], errors: list[FieldError]) -> list[dict]:
    """Bullets keep their ID while they stay in the same entry; new ones get the next number."""
    prev_ids = {b.get("id") for b in (previous or {}).get("bullets") or []}
    prev_by_text = {norm_text(b.get("text", "")): b.get("id") for b in (previous or {}).get("bullets") or []}
    raw_bullets = _as_list(raw.get("bullets"), f"{label}: bullets", errors, _form_field(raw, "bullets"))
    explicit = {
        _clean_str(b.get("id")) for b in raw_bullets
        if isinstance(b, dict) and _clean_str(b.get("id")) in prev_ids
    }
    counter_key = f"{entry_id}-b"
    bullets = []
    used: set[str] = set()
    for index, bullet in enumerate(raw_bullets):
        if isinstance(bullet, str):
            bullet = {"text": bullet}
        if not isinstance(bullet, dict):
            errors.append(FieldError(f"{label}: bullet {index + 1} must be text"))
            continue
        text = _clean_str(bullet.get("text"))
        if not text:
            continue  # a cleared bullet is a deleted bullet
        supplied = _clean_str(bullet.get("id"))
        if supplied in prev_ids and supplied not in used:
            bullet_id = supplied
        else:
            reuse = prev_by_text.get(norm_text(text))
            if reuse and reuse not in used and reuse not in explicit:
                bullet_id = reuse
            else:
                counters[counter_key] = counters.get(counter_key, 0) + 1
                bullet_id = f"{counter_key}{counters[counter_key]}"
        used.add(bullet_id)
        bullets.append({"id": bullet_id, "text": text})
    return bullets


# ---------------------------------------------------------------- diff

EMPTY = (None, "", [], {})


def _fmt(value) -> str:
    if value in EMPTY:
        return "(empty)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _tri(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "unknown"}[value]


def _entry_summary(section: str, entry: dict) -> str:
    """Every field of a new entry on one line, so it can be checked before saving."""
    fields = [f for f in ENTRY_FIELDS[section] if f not in (NAME_FIELDS[section], "start", "end")]
    parts = [entry[NAME_FIELDS[section]]]
    parts += [f"GPA {entry[f]}" if f == "gpa" else entry[f] for f in fields if entry.get(f)]
    if entry.get("start") or entry.get("end"):
        parts.append(f"{entry.get('start') or '?'} – {entry.get('end') or '?'}")
    if entry.get("technologies"):
        parts.append(", ".join(entry["technologies"]))
    count = len(entry["bullets"])
    return " · ".join(parts) + f" ({count} bullet{'s' if count != 1 else ''})"


def diff(old: Profile | dict | None, new: Profile | dict) -> list[Change]:
    """Field-level changes from the saved profile to the proposed one."""
    old, new = _dump(old), _dump(new)
    changes: list[Change] = []

    def compare(path: str, a, b) -> None:
        if a == b or (a in EMPTY and b in EMPTY):
            return
        if a in EMPTY:
            changes.append(Change("added", path, after=_fmt(b)))
        elif b in EMPTY:
            changes.append(Change("removed", path, before=_fmt(a)))
        else:
            changes.append(Change("changed", path, _fmt(a), _fmt(b)))

    def order(path: str, before: list[str], after: list[str]) -> None:
        common = [k for k in after if k in before]
        if common != [k for k in before if k in after]:
            changes.append(Change("changed", f"{path} order", _fmt([k for k in before if k in after]), _fmt(common)))

    old_contact = old.get("contact") or {}
    for key in ("name", "email", "phone", "location"):
        compare(f"contact.{key}", old_contact.get(key), new["contact"].get(key))
    old_links = old_contact.get("links") or {}
    for key in LINK_KEYS:
        compare(f"contact.links.{key}", old_links.get(key), new["contact"]["links"].get(key))
    compare("summary", old.get("summary"), new.get("summary"))

    for section in ENTRY_SECTIONS:
        old_entries = {e["id"]: e for e in old.get(section) or []}
        new_entries = {e["id"]: e for e in new.get(section) or []}
        for entry_id, entry in new_entries.items():
            before = old_entries.get(entry_id)
            if before is None:
                changes.append(Change("added", f"{section} {entry_id}", after=_entry_summary(section, entry)))
                for bullet in entry["bullets"]:
                    changes.append(Change("added", bullet["id"], after=bullet["text"]))
                continue
            for name in [f for f in entry if f not in ("id", "bullets")]:
                compare(f"{section}.{entry_id}.{name}", before.get(name), entry.get(name))
            old_b = {b["id"]: b["text"] for b in before.get("bullets") or []}
            new_b = {b["id"]: b["text"] for b in entry["bullets"]}
            for bid, text in new_b.items():
                if bid not in old_b:
                    changes.append(Change("added", bid, after=text))
                elif old_b[bid] != text:
                    changes.append(Change("changed", bid, old_b[bid], text))
            for bid in old_b.keys() - new_b.keys():
                changes.append(Change("removed", bid, before=old_b[bid]))
            order(f"{section}.{entry_id} bullet", list(old_b), list(new_b))
        for entry_id in old_entries.keys() - new_entries.keys():
            changes.append(Change("removed", f"{section} {entry_id}", before=old_entries[entry_id].get(NAME_FIELDS[section])))
        order(section, list(old_entries), list(new_entries))

    old_skills = {canon(s["name"]): s for s in old.get("skills") or []}
    new_skills = {canon(s["name"]): s for s in new["skills"]}
    for key, skill in new_skills.items():
        if key not in old_skills:
            changes.append(Change("added", "skill", after=f"{skill['name']} ({skill['category']})"))
    for key, skill in old_skills.items():
        if key not in new_skills:
            changes.append(Change("removed", "skill", before=skill["name"]))
    for key in [k for k in new_skills if k in old_skills]:
        for name in ("name", "category"):
            compare(f"skill {new_skills[key]['name']}.{name}", old_skills[key].get(name), new_skills[key].get(name))
    order("skills", list(old_skills), list(new_skills))
    compare("skills_absent", old.get("skills_absent") or [], new["skills_absent"])

    old_certs = {c["id"]: c for c in old.get("certifications") or []}
    new_certs = {c["id"]: c for c in new["certifications"]}
    for cert_id, cert in new_certs.items():
        before = old_certs.get(cert_id)
        if before is None:
            changes.append(Change("added", f"certification {cert_id}", after=" · ".join(v for v in (cert["name"], cert["issuer"], cert["date"]) if v)))
            continue
        for name in ("name", "issuer", "date"):
            compare(f"certifications.{cert_id}.{name}", before.get(name), cert.get(name))
    for cert_id in old_certs.keys() - new_certs.keys():
        changes.append(Change("removed", f"certification {cert_id}", before=old_certs[cert_id]["name"]))

    old_prefs = old.get("preferences") or {}
    for key in ("roles", "locations", "work_mode"):
        compare(f"preferences.{key}", old_prefs.get(key) or ([] if key != "work_mode" else None), new["preferences"][key])
    old_avail = old.get("availability") or {}
    for key in ("start_date", "notes"):
        compare(f"availability.{key}", old_avail.get(key), new["availability"][key])

    old_auth = {a["country"]: a for a in old.get("authorization") or []}
    new_auth = {a["country"]: a for a in new["authorization"]}
    for country, auth in new_auth.items():
        before = old_auth.get(country)
        if before is None:
            changes.append(Change("added", f"authorization {country}", after=(
                f"authorized: {_tri(auth['authorized'])}, sponsorship needed: {_tri(auth['requires_sponsorship'])}"
            )))
            continue
        for key in ("authorized", "requires_sponsorship"):
            if before.get(key) != auth[key]:
                changes.append(Change("changed", f"authorization.{country}.{key}", _tri(before.get(key)), _tri(auth[key])))
    for country in old_auth.keys() - new_auth.keys():
        changes.append(Change("removed", f"authorization {country}", before=country))
    return changes


# ---------------------------------------------------------------- persistence

def get_candidate(session: Session) -> Candidate | None:
    """The single local candidate, if a profile has been saved."""
    return session.scalars(select(Candidate).order_by(Candidate.id).limit(1)).first()


def review(session: Session, data: dict) -> ReviewResult:
    """Validate the proposed profile against the saved one and list the changes. Saves nothing."""
    candidate = get_candidate(session)
    current = candidate.profile if candidate else None
    normalized = normalize(data, current, candidate.id_counters if candidate else None)
    return ReviewResult(normalized, diff(current, normalized.profile), candidate.revision if candidate else 0)


def save(session: Session, data: dict, base_revision: int | None = None) -> SaveResult:
    """Validate and store the profile. The revision increases only if the content changed.

    ``base_revision`` is the revision the user reviewed against. If the saved profile has
    moved on since then, ``StaleReview`` is raised and nothing is written.
    """
    candidate = get_candidate(session)
    current_revision = candidate.revision if candidate else 0
    if base_revision is not None and base_revision != current_revision:
        raise StaleReview(
            f"The profile changed since you reviewed it (revision {base_revision} → {current_revision}). "
            "Review the changes again."
        )
    current = candidate.profile if candidate else None
    normalized = normalize(data, current, candidate.id_counters if candidate else None)
    changes = diff(current, normalized.profile)
    if candidate and candidate.profile == normalized.profile:
        # A long-lived session may still hold an older profile in its identity map.
        stored_revision = session.scalar(select(Candidate.revision).where(Candidate.id == candidate.id))
        if stored_revision != current_revision:
            session.rollback()
            raise StaleReview("The profile changed since you reviewed it. Review the changes again.")
        return SaveResult(candidate, [], normalized.warnings, saved=False)

    now = utcnow()
    if candidate is None:
        # A fixed singleton key makes simultaneous first saves conflict instead of
        # silently creating two local candidates, both issuing the same source IDs.
        candidate = Candidate(id=1, profile=normalized.profile, revision=1, id_counters=normalized.id_counters, created_at=now, updated_at=now)
        session.add(candidate)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            if get_candidate(session) is None:
                raise
            raise StaleReview("The profile changed since you reviewed it. Review the changes again.") from None
        # Jobs saved before the profile existed belong to this (only) candidate.
        session.execute(update(Application).where(Application.candidate_id.is_(None)).values(candidate_id=candidate.id))
    else:
        # Compare and update in the database; checking an ORM revision before an
        # unconditional flush lets overlapping sessions overwrite each other's edits.
        updated = session.execute(
            update(Candidate)
            .where(Candidate.id == candidate.id, Candidate.revision == current_revision)
            .values(profile=normalized.profile, id_counters=normalized.id_counters,
                    revision=current_revision + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            session.rollback()
            raise StaleReview("The profile changed since you reviewed it. Review the changes again.")
    session.commit()
    session.refresh(candidate)
    return SaveResult(candidate, changes, normalized.warnings, saved=True)


# ---------------------------------------------------------------- lookups

@dataclass(frozen=True)
class Source:
    """A citable piece of the profile: the summary, an entry, a bullet or a certification."""

    id: str
    kind: str  # summary | education | experience | projects | bullet | certification
    entry: str | None
    text: str
    technologies: tuple[str, ...] = ()


def sources(profile: Profile | dict | None) -> dict[str, Source]:
    """Every citable source in the profile, keyed by ID."""
    prof = _dump(profile)
    index: dict[str, Source] = {}
    if prof.get("summary"):
        index["summary"] = Source("summary", "summary", None, prof["summary"])
    for section in ENTRY_SECTIONS:
        for entry in prof.get(section) or []:
            label = " · ".join(
                _clean_str(entry.get(f))
                for f in ("title", "role", "organization", "name", "degree", "field", "institution")
                if entry.get(f)
            )
            techs = tuple(entry.get("technologies") or ())
            index[entry["id"]] = Source(entry["id"], section, entry["id"], label, techs)
            for bullet in entry.get("bullets") or []:
                index[bullet["id"]] = Source(bullet["id"], "bullet", entry["id"], bullet["text"], techs)
    for cert in prof.get("certifications") or []:
        text = f"{cert.get('name', '')} {cert.get('issuer', '')}".strip()
        index[cert["id"]] = Source(cert["id"], "certification", cert["id"], text)
    return index


def skill_keys(profile: Profile | dict | None) -> set[str]:
    """Canonical keys of skills the profile confirms, including entry technologies."""
    prof = _dump(profile)
    keys = {canon(s["name"]) for s in prof.get("skills") or []}
    for section in ("experience", "projects"):
        for entry in prof.get(section) or []:
            keys.update(canon(t) for t in entry.get("technologies") or [])
    return keys


def graduation_date(profile: Profile | dict | None) -> str | None:
    dates = [e.get("end") for e in _dump(profile).get("education") or []]
    dates = [d for d in dates if d and d != "present"]
    return max(dates) if dates else None
