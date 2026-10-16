"""The welcome page, and loading and removing the sample data."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..services import jobs as job_service
from ..services import resume_attachment, sample
from ..services.autofill import AutofillError
from ..services.profile import get_candidate
from ..templating import templates

router = APIRouter()


def is_fresh(session: Session) -> bool:
    """Nothing saved yet: no profile and no jobs. The home page then opens the welcome page."""
    return get_candidate(session) is None and not job_service.list_all(session)


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def home(session: Session = Depends(get_session)):
    """The welcome page on a fresh install, the jobs board after that."""
    return RedirectResponse("/welcome" if is_fresh(session) else "/jobs", status_code=303)


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request, msg: str = "", session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "welcome.html", {
        "candidate": get_candidate(session), "sample": sample.current(session), "msg": msg, "active": None,
    })


@router.post("/sample")
def load_sample(request: Request, session: Session = Depends(get_session)):
    try:
        sample.load(session)
    except sample.SampleError as exc:
        session.rollback()
        return templates.TemplateResponse(request, "welcome.html", {
            "candidate": get_candidate(session), "sample": sample.current(session), "error": str(exc),
            "active": None,
        }, status_code=409)
    return RedirectResponse("/jobs?msg=sample_loaded", status_code=303)


@router.get("/sample/remove", response_class=HTMLResponse)
def confirm_remove(request: Request, session: Session = Depends(get_session)):
    """What removing the sample data takes away, with the button that does it."""
    return templates.TemplateResponse(request, "sample_remove.html",
                                      {"sample": sample.current(session), "active": None})


@router.post("/sample/remove")
def remove_sample(request: Request, session: Session = Depends(get_session)):
    removed = sample.remove(session)
    job_ids = [job.id for job in removed.jobs]
    resume_attachment.remove_files(request.app.state.settings.resolved_data_dir, job_ids)
    if request.app.state.autofill.run is not None and request.app.state.autofill.run.job_id in job_ids:
        try:
            request.app.state.autofill.close()
        except AutofillError:  # busy with a step; it ends with the app or the next Close the browser
            pass
    return RedirectResponse("/welcome?msg=sample_removed", status_code=303)
