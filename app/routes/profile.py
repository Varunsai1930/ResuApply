"""Profile pages: view, guided edit form, review-changes step and save.

Saving is always two steps: the form posts to /profile/review, which shows the diff,
and only the confirmation on that page posts to /profile/save.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..services import profile as profile_service
from ..services.profile_form import parse_form
from ..templating import templates

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


def _load_payload(payload: str) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
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
def back_to_edit(request: Request, payload: str = Form(...)):
    """Return from the review page to the form without losing the proposed edits."""
    return _render_form(request, _load_payload(payload))


@router.post("/review", response_class=HTMLResponse)
async def review_profile(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
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
def save_profile(
    request: Request,
    payload: str = Form(...),
    base_revision: int = Form(...),
    session: Session = Depends(get_session),
):
    data = _load_payload(payload)
    try:
        result = profile_service.save(session, data, base_revision=base_revision)
    except profile_service.ProfileInvalid as exc:
        return _render_form(request, data, exc.errors, exc.warnings, status_code=422)
    except profile_service.StaleReview as exc:
        return _render_form(request, data, status_code=409, message=str(exc))
    msg = "profile_saved" if result.saved else "profile_unchanged"
    return RedirectResponse(f"/profile?msg={msg}", status_code=303)
