"""Jobs / Tracker list, manual job entry and the Job Workspace."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Job
from ..services import jobs as job_service
from ..services import tracking
from ..services.profile import get_candidate
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


def _render_workspace(request: Request, session: Session, job: Job, msg: str = "", status_code: int = 200, **forms):
    return templates.TemplateResponse(
        request,
        "job_workspace.html",
        {
            "job": job,
            "application": job.application,
            "candidate": get_candidate(session),
            "msg": msg,
            "today": date.today().isoformat(),
            "status_form": forms.get("status_form", {}),
            "note_form": forms.get("note_form", {}),
            "active": "jobs",
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def list_jobs(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "jobs_list.html",
        {"jobs": job_service.list_all(session), "candidate": get_candidate(session), "active": "jobs"},
    )


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
    session: Session = Depends(get_session),
):
    job = _job_or_404(session, job_id)
    form = {"status": status, "on": on, "note": note}
    try:
        try:
            on_date = date.fromisoformat(on) if on.strip() else None
        except ValueError:
            raise tracking.TrackingError("Enter the date as YYYY-MM-DD.", "on") from None
        tracking.change_status(session, job.application, status, on_date, note)
    except tracking.TrackingError as exc:
        form["error"], form["error_field"] = str(exc), exc.field
        return _render_workspace(request, session, job, status_code=422, status_form=form)
    return RedirectResponse(f"/jobs/{job.id}?msg=status_changed#tracking", status_code=303)


@router.post("/{job_id}/notes")
def add_note(request: Request, job_id: int, text: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        tracking.add_note(session, job.application, text)
    except tracking.TrackingError as exc:
        form = {"text": text, "error": str(exc)}
        return _render_workspace(request, session, job, status_code=422, note_form=form)
    return RedirectResponse(f"/jobs/{job.id}?msg=note_added#notes", status_code=303)
