"""Tracking status, status history and notes.

Tracking records what the user did; it is separate from the review state. Only an
explicit user action changes the status: approving a package never sets Applied.
"""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application
from ..schemas.tracking import Note, StatusEvent, TrackingStatus
from . import transactions

NOTE_LIMIT = 5000
EARLIEST = date(1990, 1, 1)  # older dates are typing mistakes, such as a missing digit in the year
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse_date(text: str) -> date | None:
    """A status date typed as YYYY-MM-DD, or None when empty.

    ``date.fromisoformat`` also reads week dates ("2026-W41") and compact ones ("20261010"),
    which the date field never produces; those are refused like any other format.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        if not _ISO_DATE.fullmatch(text):
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise TrackingError("Enter the date as YYYY-MM-DD.", "on") from None


class TrackingError(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def change_status(
    session: Session,
    application: Application,
    status: str,
    on: date | None = None,
    note: str = "",
    today: date | None = None,
) -> StatusEvent:
    """Record a new tracking status with the date it happened (default today) and an optional note."""
    with session.no_autoflush:
        job = application.job
    with transactions.write(session, job):
        event = apply_status(application, status, on, note, today)
    return event


def apply_status(
    application: Application,
    status: str,
    on: date | None = None,
    note: str = "",
    today: date | None = None,
) -> StatusEvent:
    """Stage a validated status change; the caller must already hold the application writer lock."""
    today = today or date.today()
    try:
        new_status = TrackingStatus((status or "").strip().lower())
    except ValueError:
        raise TrackingError("Choose a status from the list.", "status") from None
    if new_status == application.status:
        raise TrackingError(f"The status is already {new_status.label}.", "status")
    on = on or today
    if on > today:
        raise TrackingError("The date can't be in the future.", "on")
    if on < EARLIEST:
        raise TrackingError(f"Enter a date from {EARLIEST.year} onwards.", "on")
    note = (note or "").strip()
    if len(note) > NOTE_LIMIT:
        raise TrackingError(f"Keep the note under {NOTE_LIMIT:,} characters.", "note")

    now = utcnow()
    event = StatusEvent(status=new_status, on=on, recorded_at=now, note=note)
    application.tracking_status = new_status.value
    application.status_history = [*application.status_history, event]
    if new_status == TrackingStatus.APPLIED and application.applied_on is None:
        application.applied_on = on
    application.updated_at = now
    return event


def add_note(session: Session, application: Application, text: str) -> Note:
    text = (text or "").strip()
    if not text:
        raise TrackingError("Write a note first.", "text")
    if len(text) > NOTE_LIMIT:
        raise TrackingError(f"Keep the note under {NOTE_LIMIT:,} characters.", "text")
    with session.no_autoflush:
        job = application.job
    with transactions.write(session, job):
        now = utcnow()
        note = Note(text=text, at=now)
        application.notes = [*application.notes, note]
        application.updated_at = now
    return note
