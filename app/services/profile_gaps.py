"""What the questions in the user's jobs ask for that the profile doesn't hold.

Read from saved questions and the current profile; nothing is stored and nothing is guessed.
A gap only names the missing fact and the jobs that ask for it; the user decides whether to add it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..models import Job
from ..schemas.package import Question
from ..schemas.profile import Profile
from ..schemas.tracking import TrackingStatus
from . import questions as q_rules
from .countries import display_name

# factual key -> (what to call it, the profile section that holds it)
FACTS = {
    "name": ("Full name", "contact"),
    "email": ("Email", "contact"),
    "phone": ("Phone", "contact"),
    "location": ("Location", "contact"),
    "linkedin": ("LinkedIn", "contact"),
    "github": ("GitHub", "contact"),
    "portfolio": ("Portfolio", "contact"),
    "graduation_date": ("Graduation date", "education"),
    "school": ("School", "education"),
    "degree": ("Degree", "education"),
    "major": ("Field of study", "education"),
    "gpa": ("GPA", "education"),
    "start_date": ("Available start date", "preferences"),
    "authorization": ("Work authorization", "authorization"),
    "sponsorship": ("Whether you need visa sponsorship", "authorization"),
}
CLOSED = (TrackingStatus.REJECTED, TrackingStatus.WITHDRAWN)


@dataclass(frozen=True)
class Gap:
    key: str  # "start_date", "authorization:CA"
    label: str
    section: str  # the profile section to add it in
    jobs: list[Job]  # the open jobs whose questions ask for it, in the order given


def _describe(gap: str) -> tuple[str, str]:
    key, _, code = gap.partition(":")
    label, section = FACTS.get(key, (key.replace("_", " ").capitalize(), "contact"))
    if key in ("authorization", "sponsorship"):
        label += f" in {display_name(code)}" if code else " (your profile has no country yet)"
    return label, section


def _factual_questions(jobs: list[Job]) -> Iterator[tuple[Job, Question]]:
    """The questions answered from profile facts, in jobs that aren't closed."""
    for job in jobs:
        if job.application.status in CLOSED:
            continue
        for question in job.questions:
            if question.category in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL) and question.factual_key:
                yield job, question


def asks_for_facts(jobs: list[Job]) -> bool:
    """Whether any job that isn't closed has a question answered from profile facts."""
    return next(_factual_questions(jobs), None) is not None


def gaps(jobs: list[Job], profile: Profile) -> list[Gap]:
    """Missing profile facts asked for by the factual questions of jobs that aren't closed, most asked first."""
    asked: dict[str, list[Job]] = {}
    for job, question in _factual_questions(jobs):
        gap = q_rules.profile_gap(question.factual_key, question.text, profile)
        if gap and job not in asked.setdefault(gap, []):
            asked[gap].append(job)
    found = [Gap(key, *_describe(key), jobs) for key, jobs in asked.items()]
    return sorted(found, key=lambda g: (-len(g.jobs), g.label))
