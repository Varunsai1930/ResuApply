"""The Job Workspace as five guided steps, each with its status, and the one next action.

Everything here is read from existing state (the checklist, the resume state, the package
check and tracking); nothing new is stored. Blocked means progress really stops: approval
refused by ``package.check``, or a step waiting on an earlier one. A requirement gap never
blocks, so the Requirements step is Done once requirements are saved, whatever their results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from ..models import Application, Candidate, Job
from ..schemas.tracking import ReviewState, TrackingStatus
from . import answers as answer_service
from . import checklist, package, tracking
from . import resume as resume_service
from .status import Meaning, Status, for_review


class Step(StrEnum):
    REQUIREMENTS = "requirements"
    RESUME = "resume"
    QUESTIONS = "questions"
    REVIEW = "review"
    TRACK = "track"

    @property
    def number(self) -> int:
        return list(Step).index(self) + 1

    @property
    def title(self) -> str:
        return _TITLES[self]

    @property
    def anchor(self) -> str:
        """The id of the step's section on the page."""
        return "tracking" if self is Step.TRACK else self.value


_TITLES = {
    Step.REQUIREMENTS: "Requirements",
    Step.RESUME: "Resume",
    Step.QUESTIONS: "Questions",
    Step.REVIEW: "Review and approve",
    Step.TRACK: "Submit and track",
}

NO_PROFILE = "Create your profile first"


def step_url(job_id: int, step: Step) -> str:
    return f"/jobs/{job_id}?step={step.value}#{step.anchor}"


@dataclass(frozen=True)
class StepState:
    step: Step
    meaning: Meaning


@dataclass(frozen=True)
class NextAction:
    step: Step  # the step the action belongs to, shown when the workspace opens without one
    label: str
    href: str
    status: Status = Status.NEEDS_YOU  # what the action means for the job, e.g. on a jobs board card


@dataclass(frozen=True)
class Guide:
    steps: list[StepState]
    next: NextAction

    def state(self, step: Step) -> StepState:
        return self.steps[step.number - 1]


def _counts(results: list[checklist.CheckResult]) -> str:
    counts = checklist.summary(results)
    return ", ".join(f"{counts[s]} {checklist.LABELS[s].lower()}" for s in checklist.STATUSES if counts[s])


def _requirements(job: Job, candidate: Candidate | None) -> Meaning:
    if not job.requirements:
        return Meaning(Status.NEEDS_YOU, "Add the job's requirements")
    results = checklist.evaluate(job, candidate.profile if candidate else None)
    if not all(r.excerpt_found for r in results):
        return Meaning(Status.NEEDS_YOU, "The description changed; review them")
    return Meaning(Status.DONE, _counts(results))


def _resume(current: resume_service.ResumeState, candidate: Candidate | None) -> tuple[Meaning, str]:
    """The Resume step, and the label of its action while it isn't Done."""
    if candidate is None:
        return Meaning(Status.BLOCKED, NO_PROFILE), ""
    if current.accepted is not None and not current.accepted_stale:
        return Meaning(Status.DONE, "Accepted"), ""
    if current.proposal is not None and not current.proposal_stale:
        return Meaning(Status.NEEDS_YOU, "Review and accept"), "Review and accept the resume"
    if current.accepted is not None:
        return Meaning(Status.NEEDS_YOU, "Out of date; prepare a fresh one"), "Prepare a fresh resume"
    return Meaning(Status.NEEDS_YOU, "Prepare a resume"), "Prepare a resume"


def _questions(job: Job, application: Application, candidate: Candidate | None) -> tuple[Meaning, bool]:
    """The Questions step, and whether any question stops approval."""
    if candidate is None:
        return Meaning(Status.BLOCKED, NO_PROFILE), True
    if not job.questions:
        return Meaning(Status.INFO, "None added; add any the form asks"), False
    items = answer_service.resolve_all(job, application, candidate.profile)
    unsupported = {i.question.id for i in items
                   if answer_service.answer_problems(job, i.question, i.answer, candidate.profile)}
    open_items = [i for i in items if not i.resolved or i.question.id in unsupported]
    blocking = bool(unsupported) or any(package.answer_blocker(i) for i in items)
    if open_items:
        return Meaning(Status.NEEDS_YOU, f"{len(open_items)} of {len(items)} to finish"), blocking
    return Meaning(Status.DONE, f"{len(items)} answered" if len(items) != 1 else "1 answered"), blocking


def _waiting_on(steps: list[Step]) -> str:
    numbers = [str(s.number) for s in steps]
    if len(numbers) == 1:
        return f"until step {numbers[0]}"
    return f"until steps {', '.join(numbers[:-1])} and {numbers[-1]}"


def _review(review: ReviewState, blockers: list[str], resume: Meaning, questions_block: bool,
            candidate: Candidate | None) -> Meaning:
    if review is ReviewState.APPROVED:
        return for_review(review)
    if blockers:
        if candidate is None:
            return Meaning(Status.BLOCKED, NO_PROFILE)
        waiting = [s for s, waits in ((Step.RESUME, resume.status is not Status.DONE), (Step.QUESTIONS, questions_block))
                   if waits]
        return Meaning(Status.BLOCKED, _waiting_on(waiting) if waiting else "Fix the items listed")
    return for_review(review)


def _days(n: int | None) -> str:
    return "" if n is None else " for 1 day" if n == 1 else f" for {n} days"


def _track(application: Application, review: ReviewState, today: date) -> Meaning:
    status = application.status
    if tracking.follow_up_due(application, today):
        return Meaning(Status.NEEDS_YOU, f"Follow up: no reply{_days(tracking.days_since_applied(application, today))}")
    if status is TrackingStatus.SAVED:
        if review is ReviewState.APPROVED:
            return Meaning(Status.NEEDS_YOU, "Submit it, then record Applied")
        return Meaning(Status.INFO, "You submit on the employer's site")
    if status is TrackingStatus.APPLIED and application.applied_on:
        return Meaning(Status.DONE, f"Applied {application.applied_on.isoformat()}")
    return Meaning(Status.DONE, status.label)


def _next(job: Job, application: Application, candidate: Candidate | None, states: dict[Step, Meaning],
          review: ReviewState, resume_action: str, today: date) -> NextAction:
    def at(step: Step, label: str, status: Status | None = None) -> NextAction:
        return NextAction(step, label, step_url(job.id, step), status or states[step].status)

    if tracking.follow_up_due(application, today):
        return NextAction(Step.TRACK, "Follow up", f"/jobs/{job.id}?step=track#follow-up")
    if application.status is not TrackingStatus.SAVED:
        return at(Step.TRACK, "Update the status", Status.INFO)
    if candidate is None:
        return NextAction(Step.REQUIREMENTS, "Create your profile", "/profile/edit")
    if states[Step.REQUIREMENTS].status is Status.NEEDS_YOU:
        label = "Add the requirements" if not job.requirements else "Review the requirements"
        return at(Step.REQUIREMENTS, label)
    if states[Step.RESUME].status is not Status.DONE:
        return at(Step.RESUME, resume_action)
    if states[Step.QUESTIONS].status is Status.NEEDS_YOU:
        return at(Step.QUESTIONS, "Finish the questions")
    if review is not ReviewState.APPROVED:
        return at(Step.REVIEW, "Review and approve")
    return at(Step.TRACK, "Record that you applied")


def next_action(job: Job, application: Application, candidate: Candidate | None,
                today: date | None = None) -> Guide:
    """Each step's status, and the one thing to do next."""
    today = today or date.today()
    review = package.review_state(job, application, candidate)
    blockers, _ = package.check(job, application, candidate)
    resume, resume_action = _resume(resume_service.state(job, candidate), candidate)
    questions, questions_block = _questions(job, application, candidate)
    states = {
        Step.REQUIREMENTS: _requirements(job, candidate),
        Step.RESUME: resume,
        Step.QUESTIONS: questions,
        Step.REVIEW: _review(review, blockers, resume, questions_block, candidate),
        Step.TRACK: _track(application, review, today),
    }
    return Guide([StepState(step, states[step]) for step in Step], _next(job, application, candidate, states, review, resume_action, today))
