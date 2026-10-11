"""The answer bank: reusable answers to non-sensitive questions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_by_id, get_session
from ..models import AnswerBankEntry, Job
from ..services import answers as answer_service
from ..services.questions import CATEGORY_LABELS
from ..templating import templates

router = APIRouter(prefix="/answers")


@router.get("", response_class=HTMLResponse)
def bank(request: Request, msg: str = "", q: str = "", sort: Literal["recent", "most_used", "saved"] = "recent",
         session: Session = Depends(get_session)):
    """The answer bank, searched (``q``: every word in the question or answer) and sorted."""
    query = " ".join(q.split())[:answer_service.BANK_SEARCH_LIMIT]
    listing = answer_service.bank_listing(session, query, sort)
    entries = [
        {"entry": entry, "use": use, "job": session.get(Job, entry.source_job_id) if entry.source_job_id else None,
         "category": CATEGORY_LABELS.get(entry.category, entry.category)}
        for entry, use in listing.items
    ]
    return templates.TemplateResponse(request, "answers.html", {
        "entries": entries, "total": listing.total, "query": query, "sort": sort, "sorts": answer_service.BANK_SORTS,
        "msg": msg, "active": "answers",
    })


@router.post("/{entry_id}/delete")
def delete(entry_id: int, session: Session = Depends(get_session)):
    if get_by_id(session, AnswerBankEntry, entry_id) is None:
        raise HTTPException(404, "Saved answer not found.")
    answer_service.delete_bank_entry(session, entry_id)
    return RedirectResponse("/answers?msg=bank_deleted", status_code=303)
