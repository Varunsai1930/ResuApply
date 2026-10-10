"""Profile pages: view, guided edit form, review-changes step and save.

Saving is always two steps: the form posts to /profile/review, which shows the diff,
and only the confirmation on that page posts to /profile/save.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import get_session
from ..services import profile as profile_service
from ..services.profile_form import is_form_shaped, parse_form
from ..templating import templates
from .forms import read_form

router = APIRouter(prefix="/profile")

# A new profile starts with one empty row in each repeating section.
BLANK_PROFILE = {
    "contact": {"links": {}},
    "education": [{}],
    "experience": [{"bullets": [{}]}],
    "projects": [{"bullets": [{}]}],
    "certifications": [],
    "authorization": [{}],
    "preferences": {},
    "availability": {},
}


def _render_form(request: Request, data: dict, errors=(), warnings=(), status_code: int = 200, message: str = ""):
    errors = list(errors)
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "data": data,
            "errors": errors,
            "error_fields": {e.field for e in errors if e.field},
            "warnings": list(warnings),
            "message": message,
            "active": "profile",
        },
        status_code=status_code,
    )


def _load_payload(payload: object) -> dict:
    try:
        data = json.loads(payload) if isinstance(payload, str) else None
    except (json.JSONDecodeError, RecursionError):  # RecursionError: absurdly deep nesting
        data = None
    if not is_form_shaped(data):
        raise HTTPException(400, "The reviewed profile data is missing or damaged. Edit the profile again.")
    return data


@router.get("", response_class=HTMLResponse)
def view_profile(request: Request, msg: str = "", session: Session = Depends(get_session)):
    candidate = profile_service.get_candidate(session)
    return templates.TemplateResponse(
        request, "profile_view.html", {"candidate": candidate, "msg": msg, "active": "profile"}
    )


@router.get("/edit", response_class=HTMLResponse)
def edit_profile(request: Request, session: Session = Depends(get_session)):
    candidate = profile_service.get_candidate(session)
    data = candidate.profile.model_dump() if candidate else BLANK_PROFILE
    return _render_form(request, data)


@router.post("/edit", response_class=HTMLResponse)
async def back_to_edit(request: Request):
    """Return from the review page to the form without losing the proposed edits."""
    form = await read_form(request)
    return _render_form(request, _load_payload(form.get("payload")))


@router.post("/review", response_class=HTMLResponse)
async def review_profile(request: Request, session: Session = Depends(get_session)):
    form = await read_form(request)
    data = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str))
    try:
        result = profile_service.review(session, data)
    except profile_service.ProfileInvalid as exc:
        return _render_form(request, data, exc.errors, exc.warnings, status_code=422)
    return templates.TemplateResponse(
        request,
        "profile_review.html",
        {
            "result": result,
            "payload": json.dumps(data),
            "active": "profile",
        },
    )


@router.post("/save")
async def save_profile(request: Request, session: Session = Depends(get_session)):
    form = await read_form(request)
    raw_revision = form.get("base_revision")
    try:
        base_revision = int(raw_revision) if isinstance(raw_revision, str) else None
    except ValueError:
        base_revision = None
    if base_revision is None:
        raise HTTPException(422, "The reviewed profile has no valid revision. Edit the profile again.")
    data = _load_payload(form.get("payload"))
    # The form is read here with the editor's limits; the save itself runs in the thread pool,
    # like other synchronous routes, so overlapping saves are serialized by the database.
    return await run_in_threadpool(_save, request, session, data, base_revision)


def _save(request: Request, session: Session, data: dict, base_revision: int):
    try:
        result = profile_service.save(session, data, base_revision=base_revision)
    except profile_service.ProfileInvalid as exc:
        return _render_form(request, data, exc.errors, exc.warnings, status_code=422)
    except profile_service.StaleReview as exc:
        return _render_form(request, data, status_code=409, message=str(exc))
    msg = "profile_saved" if result.saved else "profile_unchanged"
    return RedirectResponse(f"/profile?msg={msg}", status_code=303)
