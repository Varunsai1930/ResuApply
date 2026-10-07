"""What candidate content may be sent to the model (PLAN.md "Outbound candidate context").

AI operations never receive the raw profile. They get a reduced career context:

- Always removed: contact details, links (profile and project), work authorization and
  sponsorship, preferences, availability, GPA, entry locations and confirmed-absent skills.
  None of these are needed to suggest evidence or tailor bullets; they are filled back in
  locally when rendering.
- With ``OPENROUTER_MODEL_TRUST=free`` the user reviews an editable preview and approves it
  before the first request. They can leave out any summary, entry, bullet, certification or
  the skills list, and reword the summary or a bullet. The approval is reused while the
  reduced context stays the same and asked for again when the profile changes it.
- With ``trusted`` the preview is optional; choices made in it are still applied.

Removing and rewording is best effort: it only covers what the user notices and edits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import Candidate, OutboundApproval
from ..schemas.profile import Profile
from .text import content_hash

ALWAYS_REMOVED = (
    "Name, email, phone and location",
    "LinkedIn, GitHub, portfolio and project links",
    "Work authorization and sponsorship",
    "Role, location and work-mode preferences, and availability",
    "GPA and the locations of your jobs",
    "Skills you said you don't have",
)
EDIT_LIMIT = 2000


class OutboundError(Exception):
    pass


def reduced_context(profile: Profile) -> dict:
    """The career content an AI operation may see, before the user's own exclusions and edits."""
    def bullets(entry) -> list[dict]:
        return [{"id": b.id, "text": b.text} for b in entry.bullets]

    return {
        "summary": {"id": "summary", "text": profile.summary} if profile.summary else None,
        "education": [
            {"id": e.id, "institution": e.institution, "degree": e.degree, "field": e.field,
             "start": e.start, "end": e.end, "bullets": bullets(e)}
            for e in profile.education
        ],
        "experience": [
            {"id": e.id, "organization": e.organization, "title": e.title, "start": e.start, "end": e.end,
             "technologies": e.technologies, "bullets": bullets(e)}
            for e in profile.experience
        ],
        "projects": [
            {"id": p.id, "name": p.name, "role": p.role, "start": p.start, "end": p.end,
             "technologies": p.technologies, "bullets": bullets(p)}
            for p in profile.projects
        ],
        "certifications": [{"id": c.id, "name": c.name, "issuer": c.issuer, "date": c.date} for c in profile.certifications],
        "skills": [s.name for s in profile.skills],
    }


def editable_ids(base: dict) -> set[str]:
    """IDs whose text the user may reword: the summary and every bullet."""
    ids = {"summary"} if base["summary"] else set()
    for section in ("education", "experience", "projects"):
        for entry in base[section]:
            ids.update(b["id"] for b in entry["bullets"])
    return ids


def excludable_ids(base: dict) -> set[str]:
    ids = editable_ids(base) | {c["id"] for c in base["certifications"]}
    for section in ("education", "experience", "projects"):
        ids.update(e["id"] for e in base[section])
    if base["skills"]:
        ids.add("skills")
    return ids


def apply_choices(base: dict, excluded: set[str], edits: dict[str, str]) -> dict:
    """The context with the user's exclusions removed and rewordings applied."""
    def bullets(entry) -> list[dict]:
        return [{"id": b["id"], "text": edits.get(b["id"], b["text"])} for b in entry["bullets"] if b["id"] not in excluded]

    out: dict = {"summary": None}
    if base["summary"] and "summary" not in excluded:
        out["summary"] = {"id": "summary", "text": edits.get("summary", base["summary"]["text"])}
    for section in ("education", "experience", "projects"):
        out[section] = [entry | {"bullets": bullets(entry)} for entry in base[section] if entry["id"] not in excluded]
    out["certifications"] = [c for c in base["certifications"] if c["id"] not in excluded]
    out["skills"] = [] if "skills" in excluded else list(base["skills"])
    return out


def source_ids(context: dict) -> set[str]:
    """Every ID the model may cite from this context."""
    ids = {"summary"} if context.get("summary") else set()
    for section in ("education", "experience", "projects"):
        for entry in context[section]:
            ids.add(entry["id"])
            ids.update(b["id"] for b in entry["bullets"])
    ids.update(c["id"] for c in context["certifications"])
    return ids


@dataclass
class OutboundState:
    """Whether AI operations may run with candidate content, and with what."""

    base: dict
    base_hash: str
    approval: OutboundApproval | None
    required: bool  # the preview must be approved before sending (free models)

    @property
    def approval_current(self) -> bool:
        return self.approval is not None and self.approval.base_hash == self.base_hash

    @property
    def approval_stale(self) -> bool:
        return self.approval is not None and not self.approval_current

    @property
    def ready(self) -> bool:
        """True when a request may be sent now."""
        if self.approval_current:
            return True
        # A stale approval holds choices the user made; ask again rather than ignore them.
        return not self.required and self.approval is None

    @property
    def context(self) -> dict | None:
        """The exact career content that would be sent, or None if approval is needed first."""
        if not self.ready:
            return None
        if self.approval_current:
            return apply_choices(self.base, set(self.approval.excluded), dict(self.approval.edits))
        return self.base

    @property
    def context_hash(self) -> str | None:
        context = self.context
        return content_hash(context) if context is not None else None


def get_approval(session: Session, candidate: Candidate) -> OutboundApproval | None:
    return session.scalars(select(OutboundApproval).where(OutboundApproval.candidate_id == candidate.id)).first()


def state(session: Session, settings: Settings, candidate: Candidate) -> OutboundState:
    base = reduced_context(candidate.profile)
    return OutboundState(
        base=base,
        base_hash=content_hash(base),
        approval=get_approval(session, candidate),
        required=settings.openrouter_model_trust == "free",
    )


def approve(
    session: Session,
    candidate: Candidate,
    reviewed_hash: str,
    included: set[str],
    texts: dict[str, str],
) -> OutboundApproval:
    """Save what the user approved. ``reviewed_hash`` must match the context they were shown."""
    base = reduced_context(candidate.profile)
    base_hash = content_hash(base)
    if reviewed_hash != base_hash:
        raise OutboundError("Your profile changed while you were reviewing. Check the updated content and approve again.")
    excluded = excludable_ids(base) - set(included)
    originals = {"summary": base["summary"]["text"]} if base["summary"] else {}
    for section in ("education", "experience", "projects"):
        for entry in base[section]:
            originals.update({b["id"]: b["text"] for b in entry["bullets"]})
    edits: dict[str, str] = {}
    for item_id, text in texts.items():
        if item_id not in originals or item_id in excluded:
            continue
        text = " ".join((text or "").split())
        if not text:
            excluded.add(item_id)  # clearing the text leaves the item out
        elif len(text) > EDIT_LIMIT:
            raise OutboundError(f"Keep each item under {EDIT_LIMIT:,} characters.")
        elif text != " ".join(originals[item_id].split()):
            edits[item_id] = text
    approval = get_approval(session, candidate)
    if approval is None:
        approval = OutboundApproval(candidate_id=candidate.id)
        session.add(approval)
    approval.base_hash = base_hash
    approval.excluded = sorted(excluded)
    approval.edits = edits
    approval.profile_revision = candidate.revision
    approval.approved_at = utcnow()
    session.commit()
    return approval


def withdraw(session: Session, candidate: Candidate) -> None:
    """Forget the approval; the next AI request will ask again (on free models)."""
    approval = get_approval(session, candidate)
    if approval is not None:
        session.delete(approval)
        session.commit()
