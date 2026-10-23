"""Progress of a background AI request: a page that reloads itself, and a JSON status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..ai import tasks
from ..db import get_session
from .jobs import _job_or_404

router = APIRouter(prefix="/jobs/{job_id}/ai")

GONE = ("This AI request isn't running any more; the app may have restarted since it began. "
        "Open the job to see what was saved, and start the request again if you need it.")


@router.get("/{task_id}", response_class=HTMLResponse)
def progress(request: Request, job_id: int, task_id: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    task = request.app.state.ai_tasks.get(job.id, task_id)
    if task is None:
        raise HTTPException(404, GONE)
    return tasks.respond(request, session, job, task, wait_seconds=0)


@router.get("/{task_id}/status")
def status(request: Request, job_id: int, task_id: str):
    task = request.app.state.ai_tasks.get(job_id, task_id)
    if task is None:
        return JSONResponse({"state": "unknown", "message": GONE}, status_code=404)
    return {
        "id": task.id, "job_id": task.job_id, "operation": task.operation, "label": task.label,
        "state": task.state, "message": task.message(), "elapsed_seconds": task.elapsed_seconds(),
        "phase": task.phase,
        "started_at": task.started_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at and task.done else None,
        "page": task.page,
    }


@router.post("/{task_id}/cancel")
def cancel(request: Request, job_id: int, task_id: str, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    task = request.app.state.ai_tasks.get(job.id, task_id)
    if task is None:
        raise HTTPException(404, GONE)
    request.app.state.ai_tasks.cancel(job.id, task.id)
    return RedirectResponse(task.page, status_code=303)
