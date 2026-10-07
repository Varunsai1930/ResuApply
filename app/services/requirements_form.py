"""Translate the requirements editor form to and from requirement dicts.

Each row uses ``req-<key>-<field>`` names. The criterion is edited as a type plus the
fields for that type; fields that don't belong to the chosen type are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..schemas.requirements import Requirement
from .profile_form import TRISTATE, split_list

_ROW_RE = re.compile(r"^req-(\w+)-(\w+)$")
CRITERION_FIELDS = {
    "skill": ("skills", "match"),
    "degree": ("level", "fields", "status"),
    "graduation_window": ("from", "to"),
    "location": ("locations", "work_mode"),
    "authorization": ("country", "sponsorship"),
    "availability": ("start_by", "start_from"),
    "years_experience": ("years", "area"),
}
CRITERION_LABELS = {
    "": "None (needs evidence)",
    "skill": "Skills",
    "degree": "Degree",
    "graduation_window": "Graduation window",
    "location": "Location / work mode",
    "authorization": "Work authorization",
    "availability": "Start date",
    "years_experience": "Years of experience",
}


def _criterion(row: dict) -> dict | None:
    kind = (row.get("ctype") or "").strip()
    if not kind:
        return None
    crit: dict = {"type": kind}
    if kind == "skill":
        crit |= {"skills": split_list(row.get("skills", ""), ","), "match": row.get("match") or "all"}
    elif kind == "degree":
        crit |= {"level": row.get("level", ""), "fields": split_list(row.get("fields", ""), ","),
                 "status": row.get("status") or "any"}
    elif kind == "graduation_window":
        crit |= {"from": (row.get("from") or "").strip() or None, "to": (row.get("to") or "").strip() or None}
    elif kind == "location":
        crit |= {"locations": split_list(row.get("locations", ""), ";\n"), "work_mode": row.get("work_mode") or None}
    elif kind == "authorization":
        raw = (row.get("sponsorship") or "").strip().lower()
        crit |= {"country": row.get("country", ""), "sponsorship_available": TRISTATE.get(raw, raw)}
    elif kind == "availability":
        crit |= {"start_by": (row.get("start_by") or "").strip() or None,
                 "start_from": (row.get("start_from") or "").strip() or None}
    elif kind == "years_experience":
        raw = (row.get("years") or "").strip()
        try:
            years: object = float(raw) if raw else None
        except ValueError:
            years = raw  # reported by validation
        crit |= {"years": years, "area": row.get("area", "")}
    return crit


def parse_form(items: Iterable[tuple[str, str]]) -> list[dict]:
    """Requirement dicts (with ``_form`` markers), in the order the rows appear. Blank rows are dropped."""
    rows: dict[str, dict] = {}
    for name, value in items:
        if match := _ROW_RE.match(name):
            key, attr = match.groups()
            rows.setdefault(key, {"_form": f"req-{key}"})[attr] = value if isinstance(value, str) else ""
    parsed = []
    for row in rows.values():
        item = {
            "_form": row["_form"],
            "id": (row.get("id") or "").strip(),
            "text": row.get("text", ""),
            "category": row.get("category", ""),
            "importance": row.get("importance", ""),
            "excerpt": row.get("excerpt", ""),
            "criterion": _criterion(row),
        }
        if (item["text"] or "").strip() or (item["excerpt"] or "").strip() or item["criterion"]:
            parsed.append(item)
    return parsed


def to_rows(items: Iterable[Requirement | dict]) -> list[dict]:
    """Flat values for the editor, from saved requirements or proposed/submitted dicts."""
    rows = []
    for index, item in enumerate(items):
        if isinstance(item, Requirement):
            item = item.model_dump(by_alias=True) | {"criterion": item.criterion_dict()}
        crit = item.get("criterion") or {}
        sponsorship = crit.get("sponsorship_available")
        years = crit.get("years")
        rows.append({
            "key": item["_form"].rsplit("-", 1)[1] if item.get("_form") else str(index),
            "id": item.get("id") or "",
            "text": item.get("text") or "",
            "category": item.get("category") or "other",
            "importance": item.get("importance") or "unspecified",
            "excerpt": item.get("excerpt") or "",
            "ctype": crit.get("type") or "",
            "skills": ", ".join(crit.get("skills") or []),
            "match": crit.get("match") or "all",
            "level": crit.get("level") or "",
            "fields": ", ".join(crit.get("fields") or []),
            "status": crit.get("status") or "any",
            "from": crit.get("from") or "",
            "to": crit.get("to") or "",
            "locations": "; ".join(crit.get("locations") or []),
            "work_mode": crit.get("work_mode") or "",
            "country": crit.get("country") or "",
            "sponsorship": {True: "yes", False: "no"}.get(sponsorship, sponsorship or ""),
            "start_by": crit.get("start_by") or "",
            "start_from": crit.get("start_from") or "",
            "years": ("" if years is None else (f"{years:g}" if isinstance(years, (int, float)) else str(years))),
            "area": crit.get("area") or "",
        })
    return rows
