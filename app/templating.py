"""Jinja2 templates and the small filters the pages use."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .schemas.tracking import ReviewState, TrackingStatus
from .services.profile_form import skills_text

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Confirmation messages shown after a redirect (?msg=code). Only known codes are shown.
MESSAGES = {
    "profile_saved": "Profile saved.",
    "profile_unchanged": "Nothing changed, so the profile and its revision were left as they were.",
    "job_created": "Job saved. Its tracking status is Saved.",
    "job_updated": "Job updated.",
    "job_unchanged": "Nothing changed in the job details.",
    "status_changed": "Tracking status updated.",
    "note_added": "Note added.",
}


def asset(path: str) -> str:
    """URL of a static file with its modification time appended, so browsers fetch new versions."""
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


def local_datetime(value: datetime | None) -> str:
    """A UTC timestamp shown in this machine's local time."""
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else ""


def tristate(value) -> str:
    """Form value for a yes/no/unknown fact: True -> "yes", False -> "no", None -> ""."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "" if value is None else str(value)


def tristate_label(value: bool | None) -> str:
    return {True: "Yes", False: "No"}.get(value, "Unknown")


templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters.update(
    local_datetime=local_datetime,
    skills_text=skills_text,
    tristate=tristate,
    tristate_label=tristate_label,
)
templates.env.globals.update(
    asset=asset,
    TrackingStatus=TrackingStatus,
    ReviewState=ReviewState,
    MESSAGES=MESSAGES,
)
