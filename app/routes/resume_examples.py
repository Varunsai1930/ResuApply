"""A read-only gallery. Trying a layout never saves a candidate's data."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..services import resume_examples, resume_layouts
from ..templating import templates
from .jobs import _job_or_404

router = APIRouter(prefix="/resume-examples")


def _context(session, job_id):
    job = _job_or_404(session, job_id) if job_id is not None else None
    return {"active": None, "job": job, "layouts": resume_layouts.LAYOUTS,
            "examples": resume_examples.EXAMPLES, "hiring_examples": resume_examples.HIRING_EXAMPLES}


@router.get("", response_class=HTMLResponse)
def gallery(request: Request, job_id: int | None = None,
            role: Literal["all", "software", "data", "marketing"] = "all",
            level: Literal["all", "intern", "professional", "lead"] = "all",
            session: Session = Depends(get_session)):
    context = _context(session, job_id)
    context.update(role=role, level=level, role_groups=resume_examples.ROLE_GROUPS, role_levels=resume_examples.ROLE_LEVELS,
                   hiring_examples=[e for e in resume_examples.HIRING_EXAMPLES
                                    if (role == "all" or e["group"] == role) and (level == "all" or e["level"] == level)])
    return templates.TemplateResponse(request, "resume_examples.html", context)


@router.get("/{layout}", response_class=HTMLResponse)
def example(request: Request, layout: str, job_id: int | None = None, session: Session = Depends(get_session)):
    try:
        choice = resume_layouts.get_layout(layout)
    except ValueError as exc:
        raise HTTPException(404, "Resume example not found.") from exc
    return templates.TemplateResponse(request, "resume_example.html", {
        **_context(session, job_id), "choice": choice, "example": resume_examples.EXAMPLES[choice.key],
        "rendered": resume_examples.SAMPLE_RESUME,
    })
