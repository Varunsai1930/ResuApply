"""Tracking status, status history and notes.

Tracking records what the user did; it is separate from the review state. Only an
explicit user action changes the status: approving a package never sets Applied.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application
from ..schemas.tracking import Note, StatusEvent, TrackingStatus
from . import transactions

NOTE_LIMIT = 5000
FOLLOW_UP_CHOICES = (7, 10, 14)  # days after applying, offered when recording Applied
FOLLOW_UP_DEFAULT = 7
SNOOZE_DAYS = 3
REMIND_CHOICES = (3, 7, 10, 14)  # days from today, when setting a reminder later
FOLLOWED_UP_NOTE = "Followed up with the employer."
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


def parse_days(text: str, choices: tuple[int, ...], field: str = "follow_up") -> int | None:
    """A reminder choice: one of ``choices`` as typed by the select, or None for "don't remind me" (empty)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isascii() and text.isdigit() and int(text) in choices:
        return int(text)
    raise TrackingError("Choose when to be reminded from the list.", field)


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
    follow_up_days: int | None = None,
) -> StatusEvent:
    """Record a new tracking status with the date it happened (default today) and an optional note.

    ``follow_up_days`` applies when the new status is Applied: remind that many days after it.
    """
    with session.no_autoflush:
        job = application.job
    with transactions.write(session, job):
        event = apply_status(application, status, on, note, today, follow_up_days)
    return event


def apply_status(
    application: Application,
    status: str,
    on: date | None = None,
    note: str = "",
    today: date | None = None,
    follow_up_days: int | None = None,
) -> StatusEvent:
    """Stage a validated status change; the caller must already hold the application writer lock.

    Recording Applied sets the follow-up reminder ``follow_up_days`` after the applied date (none
    without it); any other status means the employer replied or the job closed, so it clears it.
    """
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
    application.follow_up_after = (
        on + timedelta(days=follow_up_days) if new_status == TrackingStatus.APPLIED and follow_up_days else None
    )
    application.updated_at = now
    return event


# ---------------------------------------------------------------- follow-up reminders

def follow_up_due(application: Application, today: date | None = None) -> bool:
    """Whether to remind the user to follow up: still Applied (no reply) and the reminder date has come."""
    today = today or date.today()
    return (application.status is TrackingStatus.APPLIED and application.follow_up_after is not None
            and application.follow_up_after <= today)


def days_since_applied(application: Application, today: date | None = None) -> int | None:
    today = today or date.today()
    return (today - application.applied_on).days if application.applied_on else None


def _check_applied(application: Application) -> None:
    if application.status is not TrackingStatus.APPLIED:
        raise TrackingError("Follow-up reminders are for applications still waiting for a reply (status Applied).",
                            "status")


def set_reminder(session: Session, application: Application, days: int | None, today: date | None = None) -> None:
    """Remind the user ``days`` from today, or never (None)."""
    today = today or date.today()
    with session.no_autoflush:
        job = application.job
    with transactions.write(session, job):
        _check_applied(application)
        application.follow_up_after = today + timedelta(days=days) if days else None
        application.updated_at = utcnow()


def followed_up(session: Session, application: Application) -> None:
    """The user followed up: note it in the job's notes and clear the reminder."""
    with session.no_autoflush:
        job = application.job
    with transactions.write(session, job):
        _check_applied(application)
        now = utcnow()
        application.notes = [*application.notes, Note(text=FOLLOWED_UP_NOTE, at=now)]
        application.follow_up_after = None
        application.updated_at = now


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
