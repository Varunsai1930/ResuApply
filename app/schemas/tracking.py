"""Review state, tracking status, status history and notes for an application.

Preparation and tracking are separate: the review state says whether the package is
ready, the tracking status records what the user did. Approval never sets Applied.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ReviewState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    STALE = "stale"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class TrackingStatus(StrEnum):
    SAVED = "saved"
    APPLIED = "applied"
    ASSESSMENT = "assessment"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    WITHDRAWN = "withdrawn"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class StatusEvent(BaseModel):
    """One entry in the status history. ``on`` is the date the user gives; ``recorded_at`` is when it was saved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TrackingStatus
    on: date
    recorded_at: datetime
    note: str = ""


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    at: datetime
