"""Jobs / Tracker list, manual job entry and the Job Workspace."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..ai import operations
from ..db import get_session
from ..models import Job
from ..schemas.tracking import TrackingStatus
from ..services import board as board_service
from ..services import checklist, outbound, package, question_rows, tracking
from ..services import jobs as job_service
from ..services import workspace as guide_service
from ..services.profile import get_candidate
from ..services.questions import CATEGORIES, CATEGORY_LABELS, UNKNOWN
from ..templating import templates

router = APIRouter(prefix="/jobs")


def _job_or_404(session: Session, job_id: int) -> Job:
    job = job_service.get(session, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job


def _render_job_form(request: Request, values: dict, errors: dict | None = None, job: Job | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "job_form.html",
        {"values": values, "errors": errors or {}, "job": job, "active": "jobs",
         "saved_token": job_service.edit_token(job) if job else ""},
        status_code=status_code,
    )


def reviewed_revision_of(base_revision: object, replace_revision: object = None) -> int | None:
    """The saved revision an editor's input was reviewed against, or None when the form has none.

    ``base_revision`` is the revision the editor was opened at. After a conflict, the editor
    shows the saved version and offers ``replace_revision``: ticking it means the user compared
    their input with that newer revision, so it takes precedence.
    """
    for raw in (replace_revision, base_revision):
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
            return None
        try:
            revision = int(raw)
        except ValueError:  # Includes Python's limit on decimal integer digits.
            return None
        return revision if revision >= 1 else None
    return None


def _source_groups(candidate) -> list[dict]:
    """Profile items the user can link as evidence, grouped by entry, in profile order."""
    if candidate is None:
        return []
    prof = candidate.profile
    groups = []
    if prof.summary:
        groups.append({"label": "Summary", "items": [("summary", prof.summary)]})
    for title, entries, name in (("Experience", prof.experience, "organization"),
                                 ("Projects", prof.projects, "name"),
                                 ("Education", prof.education, "institution")):
        for entry in entries:
            label = f"{title}: {getattr(entry, name)}"
            items = [(entry.id, f"The whole entry ({getattr(entry, name)})")] + [(b.id, b.text) for b in entry.bullets]
            groups.append({"label": label, "items": items})
    if prof.certifications:
        groups.append({"label": "Certifications", "items": [(c.id, c.name) for c in prof.certifications]})
    return groups


# A re-rendered form opens the step it belongs to, so its error shows beside it.
FORM_STEPS = {
    "check_error": guide_service.Step.REQUIREMENTS,
    "ai_error": guide_service.Step.REQUIREMENTS,
    "ai_notice": guide_service.Step.REQUIREMENTS,
    "resume_error": guide_service.Step.RESUME,
    "resume_notice": guide_service.Step.RESUME,
    "question_error": guide_service.Step.QUESTIONS,
    "question_form": guide_service.Step.QUESTIONS,
    "questions_notice": guide_service.Step.QUESTIONS,
    "package_error": guide_service.Step.REVIEW,
    "status_form": guide_service.Step.TRACK,
    "note_form": guide_service.Step.TRACK,
    "follow_up_error": guide_service.Step.TRACK,
}


def _render_workspace(request: Request, session: Session, job: Job, msg: str = "", status_code: int = 200,
                      step: guide_service.Step | None = None, **forms):
    from .resume import presentation_state

    settings = request.app.state.settings
    candidate = get_candidate(session)
    results = checklist.evaluate(job, candidate.profile if candidate else None)
    groups = {status: [r for r in results if r.status == status] for status in checklist.STATUSES}
    suggestions = operations.current_suggestions(session, job, candidate, settings)
    sharing = outbound.state(session, settings, candidate) if candidate else None
    application = job.application
    # An error page shows the state without saving it: a refused request writes nothing.
    if status_code < 400:
        review = package.sync_review_state(session, job, application, candidate)
    else:
        review = package.review_state(job, application, candidate)
    blockers, warnings = package.check(job, application, candidate)
    guide = guide_service.next_action(job, application, candidate)
    if step is None:
        step = next((FORM_STEPS[name] for name, value in forms.items() if value and name in FORM_STEPS), None)
    return templates.TemplateResponse(
        request,
        "job_workspace.html",
        {
            "job": job,
            "application": job.application,
            "candidate": candidate,
            "msg": msg,
            "today": date.today().isoformat(),
            "status_form": forms.get("status_form", {}),
            "follow_up_error": forms.get("follow_up_error"),
            "follow_up_due": tracking.follow_up_due(application),
            "days_since_applied": tracking.days_since_applied(application),
            "follow_up_choices": tracking.FOLLOW_UP_CHOICES,
            "follow_up_default": tracking.FOLLOW_UP_DEFAULT,
            "remind_choices": tracking.REMIND_CHOICES,
            "snooze_days": tracking.SNOOZE_DAYS,
            "note_form": forms.get("note_form", {}),
            "check_error": forms.get("check_error"),
            "ai_error": forms.get("ai_error"),
            "ai_notice": forms.get("ai_notice"),
            "results": results,
            "result_groups": groups,
            "counts": checklist.summary(results),
            "suggestions": suggestions,
            "source_groups": _source_groups(candidate),
            "settings": settings,
            "sharing": sharing,
            "resume_state": presentation_state(session, settings, job, candidate),
            "resume_error": forms.get("resume_error"),
            "resume_notice": forms.get("resume_notice"),
            "question_rows": question_rows.rows(session, job, application, candidate) if candidate else [],
            "draft_count": question_rows.draft_count(job, application, candidate) if candidate else 0,
            "pending_drafts": bool(application.answer_drafts),
            "question_error": forms.get("question_error"),
            "question_form": forms.get("question_form") or {},
            "questions_notice": forms.get("questions_notice"),
            "question_categories": [(c, CATEGORY_LABELS[c]) for c in CATEGORIES if c != UNKNOWN],
            "review": review,
            "blockers": blockers,
            "warnings": warnings,
            "package_error": forms.get("package_error"),
            "package_token": package.package_token(job, application, candidate),
            "snapshots": list(reversed(application.submitted_snapshots)),
            "has_proposal": operations.cached_proposal(session, job, settings.openrouter_model) is not None,
            "guide": guide,
            "step": step or guide.next.step,
            "Step": guide_service.Step,
            "active": "jobs",
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def list_jobs(request: Request, q: str = "", msg: str = "", session: Session = Depends(get_session)):
    """The jobs board: columns by tracking status, each card with its next action; ``q`` searches."""
    candidate = get_candidate(session)
    jobs = job_service.list_all(session)
    for job in jobs:  # the stored review state is refreshed so the board shows Stale as soon as inputs change
        package.sync_review_state(session, job, job.application, candidate)
    board = board_service.board(jobs, candidate, date.today(), q)
    return templates.TemplateResponse(request, "jobs_list.html",
                                      {"jobs": jobs, "board": board, "candidate": candidate, "msg": msg, "Step": guide_service.Step,
                                       "snooze_days": tracking.SNOOZE_DAYS, "active": "jobs"})


@router.get("/new", response_class=HTMLResponse)
def new_job(request: Request):
    return _render_job_form(request, {})


@router.post("")
def create_job(
    request: Request,
    title: str = Form(""),
    company: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    session: Session = Depends(get_session),
):
    values = {"title": title, "company": company, "description": description, "location": location, "url": url}
    try:
        data = job_service.clean_input(**values)
    except job_service.JobInvalid as exc:
        return _render_job_form(request, values, exc.errors, status_code=422)
    job = job_service.create(session, data)
    return RedirectResponse(f"/jobs/{job.id}?msg=job_created", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
def workspace(request: Request, job_id: int, msg: str = "", step: guide_service.Step | None = None,
              session: Session = Depends(get_session)):
    """One step of the workspace; without ``step``, the step of the next action."""
    return _render_workspace(request, session, _job_or_404(session, job_id), msg, step=step)


@router.get("/{job_id}/edit", response_class=HTMLResponse)
def edit_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    values = {name: getattr(job, name) for name in ("title", "company", "description", "location", "url")}
    values["base_revision"] = job.revision
    values["base_token"] = job_service.edit_token(job)
    return _render_job_form(request, values, job=job)


@router.post("/{job_id}/edit")
def update_job(
    request: Request,
    job_id: int,
    title: str = Form(""),
    company: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    url: str = Form(""),
    base_revision: str = Form(""),
    replace_revision: str = Form(""),
    base_token: str = Form(""),
    replace_token: str = Form(""),
    session: Session = Depends(get_session),
):
    job = _job_or_404(session, job_id)
    reviewed_revision = reviewed_revision_of(base_revision, replace_revision)
    reviewed_token = replace_token if reviewed_revision_of(replace_revision) is not None else base_token
    values = {"title": title, "company": company, "description": description, "location": location, "url": url,
              "base_revision": str(reviewed_revision) if reviewed_revision else base_revision,
              "base_token": reviewed_token}
    try:
        data = job_service.clean_input(title, company, description, location, url)
    except job_service.JobInvalid as exc:
        return _render_job_form(request, values, exc.errors, job=job, status_code=422)
    try:
        if reviewed_revision is None or not reviewed_token:
            raise job_service.JobChanged("The edit form has no valid review information. Compare your changes with the saved job below.")
        changed = job_service.update(session, job, data, reviewed_revision, reviewed_token)
    except job_service.JobChanged as exc:
        return _render_job_form(request, values, {"base_revision": str(exc)}, job=job, status_code=409)
    return RedirectResponse(f"/jobs/{job.id}?msg={'job_updated' if changed else 'job_unchanged'}", status_code=303)


@router.post("/{job_id}/status")
def change_status(
    request: Request,
    job_id: int,
    status: str = Form(""),
    on: str = Form(""),
    note: str = Form(""),
    snapshot: str = Form(""),
    package_token: str = Form(""),
    follow_up: str = Form(""),
    session: Session = Depends(get_session),
):
    job = _job_or_404(session, job_id)
    form = {"status": status, "on": on, "note": note, "snapshot": bool(snapshot), "follow_up": follow_up}
    # The checkbox and the reminder only matter when recording Applied; any other status is a plain status change.
    applied = status.strip().lower() == TrackingStatus.APPLIED.value
    save_package = bool(snapshot) and applied
    try:
        on_date = tracking.parse_date(on)
        days = tracking.parse_days(follow_up, tracking.FOLLOW_UP_CHOICES) if applied else None
        if save_package:
            package.record_applied(session, job, get_candidate(session), package_token, on_date, note,
                                   follow_up_days=days)
        else:
            tracking.change_status(session, job.application, status, on_date, note, follow_up_days=days)
    except tracking.TrackingError as exc:
        session.rollback()
        form["error"], form["error_field"] = str(exc), exc.field
        return _render_workspace(request, session, job, status_code=422, status_form=form)
    except package.PackageError as exc:
        session.rollback()
        form["error"], form["error_field"] = str(exc), "snapshot"
        return _render_workspace(request, session, job, status_code=409, status_form=form)
    msg = "status_recorded" if save_package else "status_changed"
    return RedirectResponse(f"/jobs/{job.id}?step=track&msg={msg}#tracking", status_code=303)


@router.post("/{job_id}/notes")
def add_note(request: Request, job_id: int, text: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        tracking.add_note(session, job.application, text)
    except tracking.TrackingError as exc:
        form = {"text": text, "error": str(exc)}
        return _render_workspace(request, session, job, status_code=422, note_form=form)
    return RedirectResponse(f"/jobs/{job.id}?step=track&msg=note_added#notes", status_code=303)


def _after_follow_up(job: Job, back: str, msg: str) -> RedirectResponse:
    """Back to the board when the action came from its banner, else to the job's Track step."""
    if back == "board":
        return RedirectResponse(f"/jobs?msg={msg}#follow-ups", status_code=303)
    return RedirectResponse(f"/jobs/{job.id}?step=track&msg={msg}#follow-up", status_code=303)


@router.post("/{job_id}/follow-up/done")
def follow_up_done(request: Request, job_id: int, back: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        tracking.followed_up(session, job.application)
    except tracking.TrackingError as exc:
        session.rollback()
        return _render_workspace(request, session, job, status_code=409, follow_up_error=str(exc))
    return _after_follow_up(job, back, "followed_up")


@router.post("/{job_id}/follow-up/remind")
def follow_up_remind(request: Request, job_id: int, days: str = Form(""), back: str = Form(""),
                     session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        chosen = tracking.parse_days(days, tracking.REMIND_CHOICES)
        tracking.set_reminder(session, job.application, chosen)
    except tracking.TrackingError as exc:
        session.rollback()
        status_code = 409 if exc.field == "status" else 422  # not Applied (any more), or not a listed choice
        return _render_workspace(request, session, job, status_code=status_code, follow_up_error=str(exc))
    return _after_follow_up(job, back, "reminder_set" if chosen else "reminder_cleared")
