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
from ..services import checklist, outbound, package, question_rows, tracking
from ..services import jobs as job_service
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
        {"values": values, "errors": errors or {}, "job": job, "active": "jobs"},
        status_code=status_code,
    )


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


def _render_workspace(request: Request, session: Session, job: Job, msg: str = "", status_code: int = 200, **forms):
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
            "draft_count": question_rows.draft_count(job, application),
            "pending_drafts": bool(application.answer_drafts),
            "question_error": forms.get("question_error"),
            "question_form": forms.get("question_form") or {},
            "questions_notice": forms.get("questions_notice"),
            "question_categories": [(c, CATEGORY_LABELS[c]) for c in CATEGORIES if c != UNKNOWN],
            "review": review,
            "blockers": blockers,
            "warnings": warnings,
            "package_error": forms.get("package_error"),
            "snapshots": list(reversed(application.submitted_snapshots)),
            "has_proposal": operations.cached_proposal(session, job, settings.openrouter_model) is not None,
            "active": "jobs",
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def list_jobs(request: Request, session: Session = Depends(get_session)):
    candidate = get_candidate(session)
    jobs = job_service.list_all(session)
    for job in jobs:  # the stored review state is refreshed so the list shows Stale as soon as inputs change
        package.sync_review_state(session, job, job.application, candidate)
    return templates.TemplateResponse(request, "jobs_list.html", {"jobs": jobs, "candidate": candidate, "active": "jobs"})


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
def workspace(request: Request, job_id: int, msg: str = "", session: Session = Depends(get_session)):
    return _render_workspace(request, session, _job_or_404(session, job_id), msg)


@router.get("/{job_id}/edit", response_class=HTMLResponse)
def edit_job(request: Request, job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    values = {name: getattr(job, name) for name in ("title", "company", "description", "location", "url")}
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
    session: Session = Depends(get_session),
):
    job = _job_or_404(session, job_id)
    values = {"title": title, "company": company, "description": description, "location": location, "url": url}
    try:
        data = job_service.clean_input(**values)
    except job_service.JobInvalid as exc:
        return _render_job_form(request, values, exc.errors, job=job, status_code=422)
    changed = job_service.update(session, job, data)
    return RedirectResponse(f"/jobs/{job.id}?msg={'job_updated' if changed else 'job_unchanged'}", status_code=303)


@router.post("/{job_id}/status")
def change_status(
    request: Request,
    job_id: int,
    status: str = Form(""),
    on: str = Form(""),
    note: str = Form(""),
    snapshot: str = Form(""),
    session: Session = Depends(get_session),
):
    job = _job_or_404(session, job_id)
    form = {"status": status, "on": on, "note": note, "snapshot": bool(snapshot)}
    # The checkbox only matters when recording Applied; any other status is a plain status change.
    save_package = bool(snapshot) and status.strip().lower() == TrackingStatus.APPLIED.value
    try:
        try:
            on_date = date.fromisoformat(on) if on.strip() else None
        except ValueError:
            raise tracking.TrackingError("Enter the date as YYYY-MM-DD.", "on") from None
        if save_package:
            package.record_applied(session, job, get_candidate(session), on_date, note)
        else:
            tracking.change_status(session, job.application, status, on_date, note)
    except tracking.TrackingError as exc:
        session.rollback()
        form["error"], form["error_field"] = str(exc), exc.field
        return _render_workspace(request, session, job, status_code=422, status_form=form)
    except package.PackageError as exc:
        session.rollback()
        form["error"], form["error_field"] = str(exc), "snapshot"
        return _render_workspace(request, session, job, status_code=409, status_form=form)
    msg = "status_recorded" if save_package else "status_changed"
    return RedirectResponse(f"/jobs/{job.id}?msg={msg}#tracking", status_code=303)


@router.post("/{job_id}/notes")
def add_note(request: Request, job_id: int, text: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        tracking.add_note(session, job.application, text)
    except tracking.TrackingError as exc:
        form = {"text": text, "error": str(exc)}
        return _render_workspace(request, session, job, status_code=422, note_form=form)
    return RedirectResponse(f"/jobs/{job.id}?msg=note_added#notes", status_code=303)
