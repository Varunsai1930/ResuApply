"""Jinja2 templates and the small filters the pages use."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .schemas.tracking import ReviewState, TrackingStatus
from .services.profile_form import skills_text
from .services.status import Meaning, Status, for_answer

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# The fixed main nav: (key matched against a page's `active`, link, label).
NAV_ITEMS = (
    ("jobs", "/jobs", "Jobs"),
    ("answers", "/answers", "Answers"),
    ("profile", "/profile", "Profile"),
)

# Confirmation messages shown after a redirect (?msg=code). Only known codes are shown.
MESSAGES = {
    "profile_saved": "Profile saved.",
    "profile_unchanged": "Nothing changed, so the profile and its revision were left as they were.",
    "job_created": "Job saved. Its tracking status is Saved.",
    "job_updated": "Job updated.",
    "job_unchanged": "Nothing changed in the job details.",
    "status_changed": "Tracking status updated.",
    "note_added": "Note added.",
    "requirements_saved": "Requirements saved. The checklist below is calculated from your profile.",
    "requirements_unchanged": "Nothing changed in the requirements.",
    "suggestions_ready": "Evidence suggestions are ready. Accept or reject each one; nothing changes until you do.",
    "suggestions_none": "The model found nothing in what you shared that supports the Unknown requirements. You can still link evidence yourself.",
    "evidence_linked": "Evidence linked.",
    "evidence_removed": "Evidence removed.",
    "suggestion_rejected": "Suggestion rejected. It won't be offered again.",
    "override_saved": "Override saved with your reason.",
    "override_cleared": "Override cleared. The calculated status applies again.",
    "question_added": "Question added. Check the category shown beside it; change it if it looks wrong.",
    "question_removed": "Question removed with its answer and draft.",
    "category_saved": "Category saved. Any answer or draft made for the old category was removed.",
    "answer_saved": "Answer saved.",
    "answer_confirmed": "Answer confirmed from your profile.",
    "answer_skipped": "Question skipped. It is optional, so it won't block approval.",
    "drafts_ready": "AI drafts are ready. Review each one; nothing is an answer until you accept it.",
    "draft_accepted": "Draft accepted as your answer.",
    "draft_discarded": "Draft discarded.",
    "bank_saved": "Saved to the answer bank. You can reuse it as a starting point for similar questions.",
    "bank_deleted": "Removed from the answer bank. Answers already used in applications are unchanged.",
    "package_approved": "Package approved. This does not mark the job Applied; submit it yourself, then record it under Tracking.",
    "status_recorded": "Recorded as Applied. The approved package was saved as what you submitted.",
    "sharing_approved": "Saved what may be sent to the AI model.",
    "sharing_withdrawn": "Approval withdrawn. You'll be asked again before candidate content is sent.",
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


def first_name(full_name: str | None) -> str:
    """The first word of a name, for greetings. Falls back to "there"."""
    parts = (full_name or "").split()
    return parts[0] if parts else "there"


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
    first_name=first_name,
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
    NAV_ITEMS=NAV_ITEMS,
    Status=Status,
    Meaning=Meaning,
    answer_meaning=for_answer,
)
