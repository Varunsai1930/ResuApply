"""Approve the application package, and open the packages recorded as submitted."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Job
from ..services import package
from ..services.profile import get_candidate
from ..templating import templates
from .jobs import _job_or_404, _render_workspace

router = APIRouter(prefix="/jobs/{job_id}")


def _snapshot_or_404(job: Job, snapshot_id: int):
    snapshot = package.get_snapshot(job.application, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "Submitted package not found.")
    return snapshot


@router.post("/approve")
def approve(request: Request, job_id: int, package_token: str = Form(""), session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    try:
        package.approve(session, job, get_candidate(session), package_token)
    except package.PackageError as exc:
        session.rollback()
        error = {"message": str(exc), "details": exc.details}
        return _render_workspace(request, session, job, status_code=409, package_error=error)
    return RedirectResponse(f"/jobs/{job.id}?step=review&msg=package_approved#review", status_code=303)


@router.get("/snapshots/{snapshot_id}", response_class=HTMLResponse)
def snapshot(request: Request, job_id: int, snapshot_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return templates.TemplateResponse(request, "snapshot.html", {
        "job": job, "snapshot": _snapshot_or_404(job, snapshot_id), "active": "jobs",
    })


@router.get("/snapshots/{snapshot_id}/resume", response_class=HTMLResponse)
def snapshot_resume(request: Request, job_id: int, snapshot_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    return templates.TemplateResponse(request, "snapshot_resume.html", {
        "job": job, "snapshot": _snapshot_or_404(job, snapshot_id),
    })
