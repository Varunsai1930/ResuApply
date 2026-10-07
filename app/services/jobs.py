"""Manually entered jobs.

The description is untrusted data: it is stored verbatim and shown escaped, never obeyed.
Requirement extraction arrives in Milestone 2; for now a job is its details plus tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import utcnow
from ..models import Application, Job
from ..schemas.tracking import ReviewState, StatusEvent, TrackingStatus
from .profile import get_candidate

LIMITS = {"title": 200, "company": 200, "location": 200, "url": 2000, "description": 100_000}
# Changing any of these changes what a package would be built from, so the revision increases.
REVISION_FIELDS = ("title", "company", "location", "description")


class JobInvalid(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("Job is invalid; nothing was saved.")
        self.errors = errors  # form field name -> message


@dataclass(frozen=True)
class JobInput:
    title: str
    company: str
    description: str
    location: str = ""
    url: str = ""


def clean_input(title: str, company: str, description: str, location: str = "", url: str = "") -> JobInput:
    """Check required fields and limits. The description keeps its exact text (only line endings are unified)."""
    errors: dict[str, str] = {}
    values = {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        # Browsers submit textarea line breaks as CRLF; store the text as it was pasted.
        "description": (description or "").replace("\r\n", "\n").replace("\r", "\n"),
    }
    for name, label in (("title", "Job title"), ("company", "Company"), ("description", "Job description")):
        if not values[name].strip():
            errors[name] = f"{label} is required."
    for name, limit in LIMITS.items():
        if len(values[name]) > limit:
            errors[name] = f"Keep this under {limit:,} characters."
    if values["url"] and "url" not in errors:
        parts = urlsplit(values["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            errors["url"] = "Enter a full link starting with http:// or https://, or leave it empty."
    if errors:
        raise JobInvalid(errors)
    return JobInput(**values)


def create(session: Session, data: JobInput, today: date | None = None) -> Job:
    """Save a job with its application record: review state Draft, tracking status Saved."""
    now = utcnow()
    candidate = get_candidate(session)
    job = Job(**vars(data), revision=1, created_at=now, updated_at=now)
    job.application = Application(
        candidate_id=candidate.id if candidate else None,
        review_state=ReviewState.DRAFT.value,
        tracking_status=TrackingStatus.SAVED.value,
        status_history=[StatusEvent(status=TrackingStatus.SAVED, on=today or date.today(), recorded_at=now)],
        notes=[],
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    return job


def update(session: Session, job: Job, data: JobInput) -> bool:
    """Apply edits. Returns False when nothing changed. The revision increases on relevant changes."""
    changed = [name for name, value in vars(data).items() if getattr(job, name) != value]
    if not changed:
        return False
    for name in changed:
        setattr(job, name, getattr(data, name))
    if any(name in REVISION_FIELDS for name in changed):
        job.revision += 1
    job.updated_at = utcnow()
    session.commit()
    return True


def get(session: Session, job_id: int) -> Job | None:
    return session.scalars(
        select(Job).where(Job.id == job_id).options(selectinload(Job.application))
    ).first()


def list_all(session: Session) -> list[Job]:
    """Every saved job, most recently created first."""
    return list(session.scalars(
        select(Job).options(selectinload(Job.application)).order_by(Job.created_at.desc(), Job.id.desc())
    ))
