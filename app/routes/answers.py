"""The answer bank: reusable answers to non-sensitive questions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AnswerBankEntry, Job
from ..services import answers as answer_service
from ..services.questions import CATEGORY_LABELS
from ..templating import templates

router = APIRouter(prefix="/answers")


@router.get("", response_class=HTMLResponse)
def bank(request: Request, msg: str = "", session: Session = Depends(get_session)):
    entries = [
        {"entry": entry, "job": session.get(Job, entry.source_job_id) if entry.source_job_id else None,
         "category": CATEGORY_LABELS.get(entry.category, entry.category)}
        for entry in answer_service.bank_entries(session)
    ]
    return templates.TemplateResponse(request, "answers.html", {"entries": entries, "msg": msg, "active": "answers"})


@router.post("/{entry_id}/delete")
def delete(entry_id: int, session: Session = Depends(get_session)):
    if session.get(AnswerBankEntry, entry_id) is None:
        raise HTTPException(404, "Saved answer not found.")
    answer_service.delete_bank_entry(session, entry_id)
    return RedirectResponse("/answers?msg=bank_deleted", status_code=303)
