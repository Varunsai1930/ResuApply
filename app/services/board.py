"""The jobs board: jobs in columns by tracking status, each card with its next action, plus a summary.

Everything is read from existing state. A saved job's card shows the next action from the
guided workspace (``workspace.next_action``); a job past Saved shows where it stands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..models import Candidate, Job
from ..schemas.tracking import ReviewState, TrackingStatus
from . import tracking
from .status import Meaning, Status
from .workspace import Step, next_action

SEARCH_LIMIT = 200  # characters of a search; anything longer is cut, never an error

# (key, title, statuses, shown when empty). Assessment is a stage of an application still waiting
# on the employer, so it sits with Applied; Rejected and Withdrawn are both closed.
COLUMNS = (
    ("saved", "Saved", (TrackingStatus.SAVED,), "Add a job to start preparing an application."),
    ("applied", "Applied", (TrackingStatus.APPLIED, TrackingStatus.ASSESSMENT), "Jobs appear here once you record Applied."),
    ("interview", "Interview", (TrackingStatus.INTERVIEW,), "Interviews appear here when you record one."),
    ("offer", "Offer", (TrackingStatus.OFFER,), "Offers appear here when you record one."),
    ("closed", "Closed", (TrackingStatus.REJECTED, TrackingStatus.WITHDRAWN), "Rejected and withdrawn jobs, with their notes kept."),
)
HEARD_BACK = (TrackingStatus.ASSESSMENT, TrackingStatus.INTERVIEW, TrackingStatus.OFFER, TrackingStatus.REJECTED)
CLOSED = (TrackingStatus.REJECTED, TrackingStatus.WITHDRAWN)


@dataclass(frozen=True)
class Card:
    job: Job
    meaning: Meaning  # the status, and the text the card shows after it
    step: Step | None = None  # the step of the next action, for a job still being prepared


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    cards: list[Card]
    empty: str


@dataclass(frozen=True)
class Summary:
    active: int  # not rejected or withdrawn
    applied_this_month: int
    heard_back: int  # of the applied jobs, those the employer answered
    applied: int  # every job with an application date
    ready: int  # approved and not yet recorded as applied


@dataclass(frozen=True)
class FollowUp:
    job: Job
    days: int | None  # since applying


@dataclass(frozen=True)
class Board:
    columns: list[Column]
    summary: Summary
    query: str
    shown: int  # cards matching the search
    follow_ups: list[FollowUp]  # applications due a follow-up, whatever the search


def clean_query(raw: str | None) -> str:
    return " ".join((raw or "").split())[:SEARCH_LIMIT]


def matches(job: Job, query: str) -> bool:
    """Whether every word of the search appears in the job's title, company or location."""
    text = " ".join((job.title, job.company, job.location or "")).casefold()
    return all(word in text for word in query.casefold().split())


def _since(job: Job) -> date | None:
    history = job.application.status_history
    return history[-1].on if history else None


def _days(n: int) -> str:
    return "today" if n == 0 else "1 day ago" if n == 1 else f"{n} days ago"


def card(job: Job, candidate: Candidate | None, today: date) -> Card:
    application = job.application
    status = application.status
    if status is TrackingStatus.SAVED:
        guide = next_action(job, application, candidate, today)
        return Card(job, Meaning(guide.next.status, guide.next.label), guide.next.step)
    since = _since(job)
    if tracking.follow_up_due(application, today):
        days = tracking.days_since_applied(application, today)
        ago = f", applied {_days(days)}" if days is not None else ""
        return Card(job, Meaning(Status.NEEDS_YOU, f"Follow up: no reply{ago}"))
    if status is TrackingStatus.APPLIED:
        applied_on = application.applied_on or since
        if applied_on is None:
            return Card(job, Meaning(Status.INFO, "Applied"))
        return Card(job, Meaning(Status.INFO, f"Applied {applied_on.isoformat()}, {_days((today - applied_on).days)}"))
    when = f" on {since.isoformat()}" if since else ""
    if status is TrackingStatus.OFFER:
        return Card(job, Meaning(Status.DONE, f"Offer{when}"))
    if status in CLOSED:
        kept = ", notes kept" if application.notes else ""
        return Card(job, Meaning(Status.INFO, f"{status.label}{when}{kept}"))
    return Card(job, Meaning(Status.INFO, f"{status.label}{when}"))


def summary(jobs: list[Job], today: date) -> Summary:
    apps = [j.application for j in jobs]
    applied = [a for a in apps if a.applied_on is not None]
    return Summary(
        active=sum(1 for a in apps if a.status not in CLOSED),
        applied_this_month=sum(1 for a in applied if (a.applied_on.year, a.applied_on.month) == (today.year, today.month)),
        heard_back=sum(1 for a in applied if a.status in HEARD_BACK),
        applied=len(applied),
        ready=sum(1 for a in apps if a.status is TrackingStatus.SAVED and a.review is ReviewState.APPROVED),
    )


def board(jobs: list[Job], candidate: Candidate | None, today: date, query: str = "") -> Board:
    """The board for these jobs (in their given order), filtered by the search; the summary counts every job."""
    query = clean_query(query)
    shown = [j for j in jobs if matches(j, query)] if query else jobs
    columns = [
        Column(key, title, [card(j, candidate, today) for j in shown if j.application.status in statuses], empty)
        for key, title, statuses, empty in COLUMNS
    ]
    follow_ups = [FollowUp(j, tracking.days_since_applied(j.application, today))
                  for j in jobs if tracking.follow_up_due(j.application, today)]
    return Board(columns, summary(jobs, today), query, len(shown), follow_ups)
