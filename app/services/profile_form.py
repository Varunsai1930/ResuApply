"""Translate the guided profile form to and from the profile dict shape.

Repeating sections use indexed field names, for example ``experience-3-organization`` and
``experience-3-bullets-7-text``. Index tokens are arbitrary (new rows get random tokens from
the browser) and rows keep the order in which they appear in the form. Every repeating row
carries its form prefix in ``_form`` so validation errors can point at the right inputs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .profile import ENTRY_FIELDS, ENTRY_SECTIONS, LINK_KEYS

_ENTRY_RE = re.compile(r"^(education|experience|projects)-(\w+)-(\w+)$")
_BULLET_RE = re.compile(r"^(education|experience|projects)-(\w+)-bullets-(\w+)-(id|text)$")
_ROW_RE = re.compile(r"^(certifications|authorization)-(\w+)-(\w+)$")
_ROW_FIELDS = {
    "certifications": ("id", "name", "issuer", "date"),
    "authorization": ("country", "authorized", "requires_sponsorship"),
}
TRISTATE = {"yes": True, "no": False, "": None, "unknown": None}


def split_list(text: str, separators: str = ",\n") -> list[str]:
    """Split comma- and/or newline-separated input into trimmed, non-empty items."""
    parts = re.split(f"[{re.escape(separators)}]", text or "")
    return [p.strip() for p in parts if p.strip()]


def parse_skills(text: str) -> list[dict]:
    """One category per line: ``Languages: Python, SQL``. Lines without a colon go under "Skills"."""
    skills = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        category, sep, names = line.partition(":")
        if not sep:
            category, names = "Skills", line
        for name in split_list(names, ","):
            skills.append({"name": name, "category": category.strip() or "Skills"})
    return skills


def skills_text(skills: Iterable) -> str:
    """Inverse of ``parse_skills``: group skills by category in first-seen order."""
    groups: dict[str, list[str]] = {}
    for skill in skills or []:
        name = skill["name"] if isinstance(skill, dict) else skill.name
        category = (skill.get("category") if isinstance(skill, dict) else skill.category) or "Skills"
        groups.setdefault(category, []).append(name)
    return "\n".join(f"{category}: {', '.join(names)}" for category, names in groups.items())


def parse_form(items: Iterable[tuple[str, str]]) -> dict:
    """Build a profile dict (with ``_form`` markers) from submitted form fields."""
    fields: dict[str, str] = {}
    entries: dict[str, dict[str, dict]] = {s: {} for s in ENTRY_SECTIONS}
    rows: dict[str, dict[str, dict]] = {s: {} for s in _ROW_FIELDS}

    for name, value in items:
        value = value if isinstance(value, str) else ""
        if match := _BULLET_RE.match(name):
            section, key, bkey, attr = match.groups()
            entry = entries[section].setdefault(key, {"_form": f"{section}-{key}", "_bullets": {}})
            bullet = entry["_bullets"].setdefault(bkey, {"_form": f"{section}-{key}-bullets-{bkey}"})
            bullet[attr] = value
        elif match := _ENTRY_RE.match(name):
            section, key, attr = match.groups()
            entry = entries[section].setdefault(key, {"_form": f"{section}-{key}", "_bullets": {}})
            entry[attr] = value
        elif match := _ROW_RE.match(name):
            section, key, attr = match.groups()
            if attr in _ROW_FIELDS[section]:
                rows[section].setdefault(key, {"_form": f"{section}-{key}"})[attr] = value
        else:
            fields[name] = value

    data: dict = {
        "contact": {
            "name": fields.get("contact-name", ""),
            "email": fields.get("contact-email", ""),
            "phone": fields.get("contact-phone", ""),
            "location": fields.get("contact-location", ""),
            "links": {k: fields.get(f"contact-links-{k}", "") for k in LINK_KEYS},
        },
        "summary": fields.get("summary", ""),
    }

    for section in ENTRY_SECTIONS:
        parsed = []
        for entry in entries[section].values():
            bullets = [
                {k: v for k, v in b.items() if k in ("id", "text", "_form")}
                for b in entry.pop("_bullets").values()
                if (b.get("text") or "").strip()
            ]
            clean = {"_form": entry["_form"], "id": entry.get("id", "")}
            clean |= {f: entry.get(f, "") for f in ENTRY_FIELDS[section]}
            if section != "education":
                clean["technologies"] = split_list(entry.get("technologies", ""), ",")
            clean["bullets"] = bullets
            # A row the user left completely blank is not an entry.
            if any((clean[f] or "").strip() for f in ENTRY_FIELDS[section]) or bullets or clean.get("technologies"):
                parsed.append(clean)
        data[section] = parsed

    data["skills"] = parse_skills(fields.get("skills", ""))
    data["skills_absent"] = split_list(fields.get("skills_absent", ""))

    data["certifications"] = [
        row for row in rows["certifications"].values()
        if any((row.get(f) or "").strip() for f in ("name", "issuer", "date"))
    ]

    auths = []
    for row in rows["authorization"].values():
        country = (row.get("country") or "").strip()
        parsed_row = {"_form": row["_form"], "country": country}
        for attr in ("authorized", "requires_sponsorship"):
            raw = (row.get(attr) or "").strip().lower()
            parsed_row[attr] = TRISTATE.get(raw, raw)  # unrecognised values are reported by validation
        if country or parsed_row["authorized"] is not None or parsed_row["requires_sponsorship"] is not None:
            auths.append(parsed_row)
    data["authorization"] = auths

    data["preferences"] = {
        "roles": split_list(fields.get("preferences-roles", ""), "\n"),
        "locations": split_list(fields.get("preferences-locations", ""), "\n"),
        "work_mode": fields.get("preferences-work_mode", ""),
    }
    data["availability"] = {
        "start_date": fields.get("availability-start_date", ""),
        "notes": fields.get("availability-notes", ""),
    }
    return data


def is_form_shaped(data: object) -> bool:
    """Whether ``data`` has the shape ``parse_form`` produces, so the profile form can show it.

    The review page posts that dict back as JSON. Anything else (edited by hand or damaged)
    is refused before it is saved or shown, instead of failing inside the template.
    """
    def text(value: object) -> bool:
        return isinstance(value, str)

    def texts(value: object) -> bool:
        return isinstance(value, list) and all(text(v) for v in value)

    def mapping(value: object, check=text) -> bool:
        return isinstance(value, dict) and all(isinstance(k, str) and check(v) for k, v in value.items())

    def rows(value: object, check=text) -> bool:
        return isinstance(value, list) and all(mapping(row, check) for row in value)

    def entry(value: object) -> bool:
        checks = {"technologies": texts, "bullets": rows}
        return isinstance(value, dict) and all(isinstance(k, str) and checks.get(k, text)(v) for k, v in value.items())

    if not isinstance(data, dict):
        return False
    contact = data.get("contact")
    if not (isinstance(contact, dict) and mapping(contact.get("links", {}))
            and mapping({k: v for k, v in contact.items() if k != "links"})):
        return False
    preferences, availability = data.get("preferences"), data.get("availability")
    if not (mapping(preferences, lambda v: text(v) or texts(v)) and mapping(availability)):
        return False
    return (text(data.get("summary", ""))
            and all(isinstance(data.get(section, []), list) and all(entry(e) for e in data.get(section, []))
                    for section in ENTRY_SECTIONS)
            and rows(data.get("skills", [])) and texts(data.get("skills_absent", []))
            and rows(data.get("certifications", []))
            and rows(data.get("authorization", []), lambda v: v is None or isinstance(v, (str, bool))))
