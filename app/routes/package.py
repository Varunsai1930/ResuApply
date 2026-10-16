"""Approve the application package, and open the packages recorded as submitted."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Job
from ..services import cover_letter as letter_service
from ..services import package, resume_docx, resume_layouts
from ..services.profile import get_candidate
from ..templating import templates
from .jobs import _job_or_404, _render_workspace
from .resume_download import docx_response

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


@router.get("/snapshots/{snapshot_id}/resume.docx")
def snapshot_docx(job_id: int, snapshot_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    snapshot = _snapshot_or_404(job, snapshot_id)
    try:
        layout = resume_layouts.get_layout(snapshot.resume.get("_layout", "classic")).key
        content = resume_docx.export_resume(snapshot.resume, layout=layout)
    except (ValueError, resume_docx.ResumeDocxError) as exc:
        raise HTTPException(422, "This saved resume cannot be exported as a Word document.") from exc
    return docx_response(content, snapshot.resume["contact"]["name"], snapshot.job.get("company", ""))


def _snapshot_letter(job: Job, snapshot_id: int):
    """A submitted snapshot's frozen cover letter, ready to print or export, and the snapshot's layout."""
    snapshot = _snapshot_or_404(job, snapshot_id)
    if snapshot.cover_letter is None:
        raise HTTPException(404, "This submitted package has no cover letter.")
    try:
        layout = resume_layouts.get_layout(snapshot.resume.get("_layout", "classic")).key
        contact = snapshot.resume["contact"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "This saved cover letter cannot be shown.") from exc
    return snapshot, letter_service.document(contact, snapshot.job.get("company", ""), snapshot.cover_letter), layout


@router.get("/snapshots/{snapshot_id}/cover-letter")
def snapshot_cover_letter(request: Request, job_id: int, snapshot_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    snapshot, letter, layout = _snapshot_letter(job, snapshot_id)
    return templates.TemplateResponse(request, "cover_letter_print.html", {
        "job": job, "letter": letter, "selected_layout": layout,
        "back_href": f"/jobs/{job.id}/snapshots/{snapshot.id}", "back_label": "← Submitted package",
        "heading": "Cover letter as submitted",
        "note": f"Frozen when you recorded Applied on {snapshot.submitted_on.isoformat()}. Choose Print, then Save "
                "as PDF. Use A4 paper and disable the browser's headers and footers.",
        "docx_href": f"/jobs/{job.id}/snapshots/{snapshot.id}/cover-letter.docx",
    })


@router.get("/snapshots/{snapshot_id}/cover-letter.docx")
def snapshot_cover_letter_docx(job_id: int, snapshot_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    snapshot, letter, layout = _snapshot_letter(job, snapshot_id)
    try:
        content = resume_docx.export_cover_letter(letter, layout=layout)
    except resume_docx.ResumeDocxError as exc:
        raise HTTPException(422, "This saved cover letter cannot be exported as a Word document.") from exc
    return docx_response(content, letter["name"], snapshot.job.get("company", ""), kind="cover letter")
