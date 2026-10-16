"""Greenhouse autofill: open the employer's form in a visible browser and fill what the approved package covers.

Every step is the user's explicit action on the Submit and track step, and each one checks again:

1. **Open the form** needs an approved, current package (the page's ``package_token``), a job
   link to a standard Greenhouse-hosted form (``greenhouse.posting_ref``) and the resume PDF
   attached to that package (``resume_attachment``). The browser opens the link and the form is
   read; ``autofill_plan`` decides, field by field, what would be filled and what is left.
2. **Fill the verified fields** vouches for that plan (``plan_token``). The form is read again
   first: a CAPTCHA, a sign-in, a page that moved elsewhere or a form whose fields changed hands
   control back to the user instead. Fields that hold a value are kept unless the user ticked
   them, and only if the value is still the one the plan showed. Each filled value is read back.
3. **Close the browser** ends it; so does stopping the app.

Nothing here clicks Submit, sends keys or changes the tracking status: the user submits on the
employer's form and records Applied themselves. The AI model plays no part.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..models import Application, Candidate, Job
from ..schemas.tracking import ReviewState
from . import package, resume_attachment
from .autofill_browser import SETUP_MESSAGE, BrowserError, FillStep, FormBrowser, Inspection, LaunchOptions, installed
from .autofill_plan import (CONTACT_KEYS, LEAVE, OCCUPIED, SAME, FillData, PackageAnswer, PlanItem,
                            norm_value, plan, plan_token, structure)
from .greenhouse import HOSTS, PostingRef, posting_ref

# Report outcomes
FILLED, ATTACHED, MISMATCH, KEPT, ALREADY, LEFT, FAILED = (
    "filled", "attached", "mismatch", "kept", "already", "left", "failed")
OUTCOME_LABELS = {
    FILLED: "Filled and read back",
    ATTACHED: "Attached and checked",
    MISMATCH: "Doesn't match",
    KEPT: "Kept the existing value",
    ALREADY: "Already filled",
    LEFT: "Left for you",
    FAILED: "Not filled",
}
NOT_GREENHOUSE = ("Autofill works only with a standard Greenhouse-hosted form: the job's link must be a posting on "
                  f"{' or '.join(HOSTS)}. Other forms, including an employer's own page with a Greenhouse form "
                  "inside, are yours to fill in. Add the posting link with Edit details if you have it.")


class AutofillError(Exception):
    """A user-facing error. Nothing was filled."""


@dataclass(frozen=True)
class ReportRow:
    label: str
    outcome: str
    detail: str = ""

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS[self.outcome]


@dataclass
class Run:
    """The open browser's form and what happened to it. One at a time, kept in memory only."""

    job_id: int
    job_label: str
    url: str
    ref: PostingRef
    phase: str = "inspected"  # "inspected", "filled" or "handed_over"
    structure: str = ""
    plan: list[PlanItem] = field(default_factory=list)
    token: str = ""
    report: list[ReportRow] | None = None
    notice: str = ""  # why control went back to the user, or what to look at

    @property
    def fillable(self) -> list[PlanItem]:
        return [item for item in self.plan if item.fillable]

    @property
    def left(self) -> list[PlanItem]:
        return [item for item in self.plan if item.action == LEAVE]


@dataclass(frozen=True)
class Readiness:
    installed: bool
    posting_url: str | None
    approved: bool
    attachment: object | None  # the current ResumeAttachment
    outdated_attachment: bool  # a PDF is attached, but to an earlier package

    @property
    def problems(self) -> list[str]:
        problems = []
        if not self.installed:
            problems.append(SETUP_MESSAGE)
        if self.posting_url is None:
            problems.append(NOT_GREENHOUSE)
        if not self.approved:
            problems.append("Approve the current package first (step 4). Autofill uses only what you approved.")
        elif self.attachment is None:
            problems.append("Attach the resume PDF you saved from this package." + (
                " The one attached belongs to an earlier version of the package." if self.outdated_attachment else ""))
        return problems

    @property
    def ready(self) -> bool:
        return not self.problems


def readiness(job: Job, application: Application, candidate: Candidate | None) -> Readiness:
    approved = package.review_state(job, application, candidate) is ReviewState.APPROVED
    current = resume_attachment.current(job, application, candidate)
    return Readiness(
        installed=installed(),
        posting_url=job.url if posting_ref(job.url) is not None else None,
        approved=approved,
        attachment=current,
        outdated_attachment=application.resume_attachment is not None and current is None,
    )


def fill_data(job: Job, application: Application, candidate: Candidate | None) -> FillData:
    """The approved package's contact details, resolved answers and attached PDF, as autofill may use them."""
    resolved = package.resolved_package(job, application, candidate)
    contact = (resolved.get("resume") or {}).get("contact") or {}
    links = contact.get("links") or {}
    values = {key: str(links.get(key) if key in ("linkedin", "github", "portfolio") else contact.get(key) or "")
              for key in CONTACT_KEYS}
    answers = tuple(PackageAnswer(question=q["question"], category=q["category"], text=q["text"] or "",
                                  resolved=q["resolved"] is True, skipped=q["skipped"] is True)
                    for q in resolved["questions"])
    attachment = resume_attachment.current(job, application, candidate)
    return FillData(
        contact=values, answers=answers,
        resume_file=attachment.filename if attachment else None, resume_size=attachment.size if attachment else 0,
        package_hash=application.approval.content_hash if application.approval else "",
        resume_sha256=attachment.sha256 if attachment else "",
    )


def handover(inspection: Inspection, ref: PostingRef) -> str:
    """Why the browser is the user's now, or "" when ResuApply may carry on with the form."""
    if inspection.closed:
        return "The browser window was closed."
    if posting_ref(inspection.url) != ref:
        host = urlsplit(inspection.url).hostname or "another page"
        return (f"The page moved to {host}, not the Greenhouse form ResuApply opened. Nothing more is filled after "
                "an unexpected redirect; the browser is yours.")
    if inspection.status in (404, 410):
        return "Greenhouse has no form at this link; the posting may have closed."
    if inspection.login:
        return "The page asks you to sign in. Do that in the browser yourself; ResuApply never signs in for you."
    if inspection.captcha:
        return "A CAPTCHA appeared. Complete it in the browser yourself; ResuApply doesn't fill past it."
    if not inspection.form:
        return ("No standard Greenhouse application form was found on this page. Forms ResuApply can't read are "
                "yours to fill in.")
    if inspection.too_many:
        return "The form has more fields than a standard application form. Fill it in yourself."
    return ""


class Autofill:
    """The app's one autofill browser and the form it has open."""

    def __init__(self, data_dir: Path, options: LaunchOptions | None = None):
        self.data_dir = data_dir
        self.browser = FormBrowser(options)
        self.run: Run | None = None
        self._busy = threading.Lock()

    @contextmanager
    def _hold(self) -> Iterator[None]:
        """One step at a time: a second request while the browser works is refused, not queued."""
        if not self._busy.acquire(blocking=False):
            raise AutofillError("Autofill is still busy with the previous step. Wait for it to finish, then reload "
                                "the page.")
        try:
            yield
        finally:
            self._busy.release()

    def for_job(self, job_id: int) -> Run | None:
        return self.run if self.run is not None and self.run.job_id == job_id else None

    def _check(self, job: Job, application: Application, candidate: Candidate | None) -> FillData:
        problems = readiness(job, application, candidate).problems
        if problems:
            raise AutofillError(" ".join(problems))
        return fill_data(job, application, candidate)

    def _start_run(self, job: Job, ref: PostingRef, inspection: Inspection, data: FillData) -> Run:
        run = Run(job_id=job.id, job_label=f"{job.title} at {job.company}", url=job.url, ref=ref)
        self.run = run
        self._read(run, inspection, data)
        return run

    def _read(self, run: Run, inspection: Inspection, data: FillData) -> None:
        run.notice = handover(inspection, run.ref)
        run.structure = structure(list(inspection.fields))
        run.plan = plan(list(inspection.fields), data) if not run.notice else []
        run.token = plan_token(run.structure, run.plan, data)
        run.phase = "handed_over" if run.notice else "inspected"
        run.report = None

    def open(self, job: Job, candidate: Candidate | None, token: str) -> Run:
        """Open the job's Greenhouse form in the visible browser and plan what to fill."""
        with self._hold():
            application = job.application
            if self.run is not None and self.run.job_id != job.id:
                raise AutofillError(f"The browser is open for another job ({self.run.job_label}). Close it first.")
            data = self._check(job, application, candidate)
            if token != package.package_token(job, application, candidate):
                raise AutofillError("The package changed since you reviewed it, so the form wasn't opened. Review "
                                    "the package as it is now, then try again.")
            self._load_resume(job, application, candidate)  # a missing or changed file is refused before opening
            ref = posting_ref(job.url)
            try:
                inspection = self.browser.open(job.url)
            except BrowserError as exc:
                raise AutofillError(str(exc)) from None
            return self._start_run(job, ref, inspection, data)

    def inspect(self, job: Job, candidate: Candidate | None) -> Run:
        """Read the open form again, after the user dealt with whatever it asked of them."""
        with self._hold():
            run = self._run_for(job)
            data = self._check(job, job.application, candidate)
            try:
                inspection = self.browser.inspect()
            except BrowserError as exc:
                raise AutofillError(str(exc)) from None
            if inspection.closed:
                self._end()
                raise AutofillError("The browser window was closed. Open the form again when you're ready.")
            self._read(run, inspection, data)
            return run

    def fill(self, job: Job, candidate: Candidate | None, token: str, replace: set[str]) -> Run:
        """Fill the planned fields, read them back and report. Never submits."""
        with self._hold():
            run = self._run_for(job)
            application = job.application
            if run.phase == "handed_over":
                raise AutofillError("The browser is yours for now: " + run.notice + " Choose Check the form again "
                                    "when you want ResuApply to read it.")
            data = self._check(job, application, candidate)
            if posting_ref(job.url) != run.ref:
                raise AutofillError("The job's link changed since the form was opened. Close the browser and open "
                                    "the form again.")
            pdf = self._load_resume(job, application, candidate)
            try:
                inspection = self.browser.inspect()
            except BrowserError as exc:
                raise AutofillError(str(exc)) from None
            if inspection.closed:
                self._end()
                raise AutofillError("The browser window was closed. Open the form again when you're ready.")
            notice = handover(inspection, run.ref)
            if not notice and structure(list(inspection.fields)) != run.structure:
                notice = ("The form's fields changed since ResuApply read it. Nothing was filled; check the form "
                          "again, or fill it in yourself.")
            if notice:
                run.phase, run.notice, run.report = "handed_over", notice, None
                return run
            current = plan(list(inspection.fields), data)
            if token != run.token or plan_token(run.structure, current, data) != run.token:
                raise AutofillError("The package, the attached PDF or the plan changed since the form was read. "
                                    "Choose Check the form again, review what will be filled, then fill.")
            shown = {item.key: item for item in run.plan}
            steps, report = [], {}
            for item in current:
                planned = shown.get(item.key, item)
                if item.action == LEAVE:
                    report[item.key] = ReportRow(item.label, LEFT, item.reason)
                elif item.action == SAME:
                    report[item.key] = ReportRow(item.label, ALREADY, "The form already held this value.")
                elif item.action == OCCUPIED and item.key not in replace:
                    report[item.key] = ReportRow(item.label, KEPT, f"Kept: {item.existing}")
                elif item.action == OCCUPIED and norm_value(item.existing) != norm_value(planned.existing):
                    report[item.key] = ReportRow(item.label, KEPT, "It changed in the browser after the form was "
                                                 f"read, so your entry was kept: {item.existing}")
                elif item.file:
                    steps.append(FillStep(item.key, file_name=item.value, file_data=pdf))
                else:
                    steps.append(FillStep(item.key, value=item.value))
            expected = run.ref
            try:
                result = self.browser.fill(steps, lambda url: posting_ref(url) == expected)
            except BrowserError as exc:
                raise AutofillError(str(exc)) from None
            labels = {item.key: item.label for item in current}
            outcomes = {outcome.key: outcome for outcome in result.outcomes}
            for step in steps:
                report[step.key] = _checked(labels[step.key], step, outcomes.get(step.key), data)
            run.report = [report[item.key] for item in current if item.key in report]
            notices = []
            if result.blocked_submissions:
                notices.append("The page tried to submit the form while ResuApply was filling it; that was "
                               "stopped. Check the form carefully before you submit it.")
            if result.stopped == "navigated":
                notices.append(handover(result.after, run.ref) or "The page reloaded while ResuApply was filling, so "
                               "it stopped. Check the form again.")
            elif result.stopped == "closed" or result.after.closed:
                notices.append("The browser window was closed.")
            else:
                after = handover(result.after, run.ref)
                if after:
                    notices.append(after)
            run.notice = " ".join(notices)
            run.phase = "handed_over" if notices else "filled"
            return run

    def close(self) -> None:
        with self._hold():
            self._end()

    def shutdown(self) -> None:
        """App shutdown: close the browser and end its thread, whatever is in progress."""
        self.run = None
        self.browser.shutdown()

    def _end(self) -> None:
        self.run = None
        self.browser.close()

    def _run_for(self, job: Job) -> Run:
        run = self.for_job(job.id)
        if run is None:
            raise AutofillError("No browser is open for this job. Choose Open the form in a browser first.")
        return run

    def _load_resume(self, job: Job, application: Application, candidate: Candidate | None) -> bytes:
        attachment = resume_attachment.current(job, application, candidate)
        if attachment is None:
            raise AutofillError("Attach the resume PDF you saved from this package.")
        try:
            return resume_attachment.load(self.data_dir, job, attachment)
        except resume_attachment.AttachmentError as exc:
            raise AutofillError(str(exc)) from None


def _checked(label: str, step: FillStep, outcome, data: FillData) -> ReportRow:
    """What the read-back says about one filled field."""
    if outcome is None or not outcome.done:
        return ReportRow(label, FAILED, (outcome.error if outcome else "") or "ResuApply couldn't fill this field.")
    if outcome.error:
        return ReportRow(label, MISMATCH, outcome.error)
    if step.is_file:
        if outcome.read == step.file_name and outcome.read_size == data.resume_size:
            return ReportRow(label, ATTACHED, f"{step.file_name} ({data.resume_size:,} bytes)")
        shown = outcome.read or "no file"
        return ReportRow(label, MISMATCH, f"The form holds {shown}, not {step.file_name}. Attach it there yourself.")
    if norm_value(outcome.read) == norm_value(step.value):
        return ReportRow(label, FILLED, step.value)
    return ReportRow(label, MISMATCH, f"The form shows “{outcome.read}” instead of “{step.value}”.")
