"""The outbound-context preview: what career content may be sent to the AI model."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..services import outbound
from ..services.profile import get_candidate
from ..templating import templates

router = APIRouter(prefix="/profile/sharing")

_SAFE_NEXT = re.compile(r"^/jobs/\d+$")


def _safe_next(value: str) -> str:
    """Only return to a job workspace in this app; anything else goes back to the preview."""
    return value if _SAFE_NEXT.match(value or "") else ""


def _render(request: Request, session: Session, candidate, next_url: str = "", error: str = "", status_code: int = 200,
            msg: str = ""):
    state = outbound.state(session, request.app.state.settings, candidate) if candidate else None
    choices = None
    if state is not None:
        approval = state.approval
        # Pre-fill with the user's earlier choices, even when the approval is out of date.
        choices = {
            "excluded": set(approval.excluded) if approval else set(),
            "edits": dict(approval.edits) if approval else {},
        }
    return templates.TemplateResponse(
        request,
        "sharing.html",
        {
            "candidate": candidate,
            "state": state,
            "choices": choices,
            "always_removed": outbound.ALWAYS_REMOVED,
            "settings": request.app.state.settings,
            "next_url": next_url,
            "error": error,
            "msg": msg,
            "active": "profile",
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def preview(request: Request, next: str = "", msg: str = "", session: Session = Depends(get_session)):
    return _render(request, session, get_candidate(session), _safe_next(next), msg=msg)


@router.post("")
async def approve(request: Request, session: Session = Depends(get_session)):
    candidate = get_candidate(session)
    form = await request.form()
    next_url = _safe_next(str(form.get("next") or ""))
    if candidate is None:
        return RedirectResponse("/profile", status_code=303)
    included = {str(v) for v in form.getlist("include")}
    texts = {k.removeprefix("text-"): str(v) for k, v in form.multi_items() if k.startswith("text-")}
    try:
        outbound.approve(session, candidate, str(form.get("base_hash") or ""), included, texts)
    except outbound.OutboundError as exc:
        return _render(request, session, candidate, next_url, error=str(exc), status_code=409)
    if next_url:
        return RedirectResponse(f"{next_url}?msg=sharing_approved#requirements", status_code=303)
    return RedirectResponse("/profile/sharing?msg=sharing_approved", status_code=303)


@router.post("/withdraw")
def withdraw(request: Request, session: Session = Depends(get_session)):
    candidate = get_candidate(session)
    if candidate is not None:
        outbound.withdraw(session, candidate)
    return RedirectResponse("/profile/sharing?msg=sharing_withdrawn", status_code=303)
