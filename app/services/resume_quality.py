"""Local writing suggestions. These never establish truth or gate acceptance."""

import re
from dataclasses import dataclass

from ..schemas.profile import Profile
from ..schemas.resume import ResumeContent


@dataclass(frozen=True)
class QualityNote:
    label: str
    message: str
    section: str


def quality_notes(profile: Profile, resume: ResumeContent) -> list[QualityNote]:
    notes = []
    seen = set()
    for section in ("experience", "projects", "education"):
        selected = getattr(resume, section)
        ids = set(selected if section == "education" else (e.entry for e in selected))
        claims = {e.entry: e.bullets for e in selected} if section != "education" else {}
        for entry in getattr(profile, section):
            if entry.id not in ids:
                continue
            label = getattr(entry, "organization", None) or getattr(entry, "name", None) or entry.institution
            if re.fullmatch(r"\d{4}-\d{2}", entry.start) and re.fullmatch(r"\d{4}-\d{2}", entry.end) and entry.start > entry.end:
                notes.append(QualityNote(label, "The end date is before the start date. Check the saved dates.", section))
            for bullet in (entry.bullets if section == "education" else claims[entry.id]):
                text = bullet.text
                key = re.sub(r"\W+", " ", text.casefold()).strip()
                if key in seen:
                    notes.append(QualityNote(label, "A bullet repeats wording used elsewhere: “" + text[:120] + "”. Consider keeping it once.", section))
                seen.add(key)
                if re.match(r"(?:responsible for|worked on|helped with|involved in)\b", text, re.I):
                    notes.append(QualityNote(label, "“" + text[:100] + "” starts broadly. Describe your specific action and the outcome you can support.", section))
                if len(text.split()) > 45:
                    notes.append(QualityNote(label, "A bullet has more than 45 words. Consider focusing it on one action and result.", section))
    return notes[:20]
