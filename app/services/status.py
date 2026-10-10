"""The four status meanings every screen shares: Done, Needs you, Blocked and Info.

Existing statuses (checklist results, review states, resolved answers) keep their own wording
where they are shown in detail; these helpers say which of the four meanings each one has, so
steps, cards and badges agree on colour and icon.

Blocked is kept for what really stops progress (package blockers, a step waiting on an earlier
one). A requirement gap never blocks an application, so Unmet and Unknown are Info.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..schemas.tracking import ReviewState
from . import checklist
from .answers import ResolvedAnswer


class Status(StrEnum):
    DONE = "done"
    NEEDS_YOU = "needs_you"
    BLOCKED = "blocked"
    INFO = "info"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Status.DONE: "Done",
    Status.NEEDS_YOU: "Needs you",
    Status.BLOCKED: "Blocked",
    Status.INFO: "Info",
}


@dataclass(frozen=True)
class Meaning:
    """One of the four statuses, with a short note on what it means here and an optional action."""

    status: Status
    note: str
    action: str | None = None


_CHECK_MEANINGS = {
    checklist.MET: Meaning(Status.DONE, "Met"),
    checklist.UNMET: Meaning(Status.INFO, "Gap"),
    checklist.UNKNOWN: Meaning(Status.INFO, "Unknown", "Link evidence"),
}

_REVIEW_MEANINGS = {
    ReviewState.APPROVED: Meaning(Status.DONE, "Approved"),
    ReviewState.DRAFT: Meaning(Status.NEEDS_YOU, "Review and approve"),
    ReviewState.STALE: Meaning(Status.NEEDS_YOU, "Changed since approval; approve again"),
}


def for_check(status: str) -> Meaning:
    """A checklist result: Met is Done; Unmet (a gap) and Unknown are Info, since neither blocks."""
    return _CHECK_MEANINGS[status]


def for_review(state: ReviewState) -> Meaning:
    """A package review state: Approved is Done; Draft and Stale need the user's approval."""
    return _REVIEW_MEANINGS[ReviewState(state)]


def for_answer(item: ResolvedAnswer) -> Meaning:
    """A resolved question: an explicit skip is Info, a resolved answer Done, anything open Needs you."""
    if item.skipped:
        return Meaning(Status.INFO, item.label)
    if item.resolved:
        return Meaning(Status.DONE, item.label)
    return Meaning(Status.NEEDS_YOU, item.label)
