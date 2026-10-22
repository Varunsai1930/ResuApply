"""Run AI requests in the background, so no page waits up to 90 seconds for the model.

A route checks what it can at once (a profile exists, AI is set up), then ``start``s the request
and answers with ``respond``: the result, if the request finished within a moment (a cached
result, an instant refusal), otherwise the request's progress page. That page reloads itself
(an HTML meta refresh, no JavaScript) and shows the result once the request has finished; a plain
reload does the same.

The request is the same service function as before (``operations.extract_requirements``,
``tailor_resume`` and so on), run in a worker thread with its own database session, so it keeps
every guarantee: freshness checks before each request and before saving, all-or-nothing
validation, cached validated runs, ``operations.exclusive`` and failures that leave existing work
untouched. Its outcome, a result or the exception it raised, is turned into a page by the route's
``finish`` function, which handles each error exactly as the route did when it waited.

Requests are kept in memory: this is a single-user local app, and a request doesn't outlive it.
Only one request per operation and job runs at a time; starting it again shows the running one.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from ..db import get_by_id, utcnow
from ..models import Candidate, Job
from ..services.answers import AnswerError
from ..services.cover_letter import CoverLetterError
from ..services.profile import get_candidate
from ..services.resume import ResumeError
from ..templating import templates
from . import operations
from .answers import DRAFT_ANSWERS
from .client import AIError
from .cover_letter import DRAFT_COVER_LETTER
from .resume import TAILOR_RESUME

logger = logging.getLogger("resuapply.ai")

# How long a route waits for the request before showing the progress page instead. Long enough
# for a cached result or a refusal to show at once, short enough that the page never seems stuck.
WAIT_SECONDS = 1.0
REFRESH_SECONDS = 2  # the progress page reloads itself this often
KEEP_FINISHED = timedelta(hours=1)  # a finished request's outcome stays viewable this long
WORKERS = 4
SHUTDOWN_SECONDS = 5.0  # how long app shutdown waits for running requests before leaving them

# What the progress page calls each request, and where its step is in the job's workspace.
LABELS = {
    operations.PARSE_JOB: "Extracting requirements",
    operations.SUGGEST_EVIDENCE: "Suggesting evidence",
    TAILOR_RESUME: "Tailoring your resume",
    DRAFT_ANSWERS: "Drafting answers",
    DRAFT_COVER_LETTER: "Drafting your cover letter",
}
STEPS = {
    operations.PARSE_JOB: "requirements#requirements",
    operations.SUGGEST_EVIDENCE: "requirements#requirements",
    TAILOR_RESUME: "resume",
    DRAFT_ANSWERS: "questions",
    DRAFT_COVER_LETTER: "questions#cover-letter",
}

# work(session, job, candidate) runs in the background; finish(request, session, job, task) makes the page.
Work = Callable[[Session, Job, Candidate | None], Any]
Finish = Callable[[Request, Session, Job, "Task"], Response]


class Gone(Exception):
    """What the request needed was removed while it waited to run."""


@dataclass
class Task:
    id: str
    operation: str
    job_id: int
    finish: Finish
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    future: Future = field(default_factory=Future)

    @property
    def label(self) -> str:
        return LABELS.get(self.operation, "Working")

    @property
    def page(self) -> str:
        return f"/jobs/{self.job_id}/ai/{self.id}"

    @property
    def back(self) -> str:
        step = STEPS.get(self.operation, "requirements")
        path, _, anchor = step.partition("#")
        return f"/jobs/{self.job_id}?step={path}" + (f"#{anchor}" if anchor else "")

    @property
    def done(self) -> bool:
        return self.future.done()

    @property
    def state(self) -> str:
        """running, done or failed. A refusal that changes nothing (approval needed, nothing to do) is failed too."""
        if not self.future.done():
            return "running"
        return "failed" if self.future.cancelled() or self.future.exception() is not None else "done"

    def elapsed_seconds(self) -> int:
        return int(((self.finished_at or utcnow()) - self.started_at).total_seconds())

    def result(self):
        """The work's return value; raises the exception the work raised."""
        return self.future.result()

    def message(self) -> str:
        """A plain account of a finished request, for the status endpoint."""
        if not self.future.done():
            return f"{self.label}… This can take up to 90 seconds."
        if self.future.cancelled():
            return "The request was stopped before it ran. Nothing was saved; start it again."
        exc = self.future.exception()
        if exc is None:
            return "Finished. Open the page to see the result."
        if isinstance(exc, operations.ApprovalNeeded):
            return "Review what the AI model may see, then start the request again. Nothing was sent."
        if isinstance(exc, AIError):
            return exc.message
        if isinstance(exc, (operations.NothingToDo, Gone, ResumeError, AnswerError, CoverLetterError)):
            return str(exc)
        return "Something unexpected went wrong. Your work is unchanged; open the page for details."


class AITasks:
    """The app's AI requests: at most ``workers`` running at once, and what each request is doing.

    Each request gets a daemon thread that waits for a free slot. Daemon threads never hold the
    process open: stopping the app waits ``SHUTDOWN_SECONDS`` for running requests, then leaves
    them. A request cut off that way saves nothing, since every save is one transaction at its end.
    """

    def __init__(self, session_factory: sessionmaker[Session], workers: int = WORKERS):
        self._session_factory = session_factory
        self._slots = threading.BoundedSemaphore(workers)
        self._closed = threading.Event()
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def start(self, operation: str, job_id: int, work: Work, finish: Finish) -> Task:
        """Run ``work`` in the background, or return the same request if it is already running."""
        with self._lock:
            self._forget_finished()
            running = next((t for t in self._tasks.values()
                            if t.operation == operation and t.job_id == job_id and not t.done), None)
            if running is not None:
                return running
            if self._closed.is_set():
                raise RuntimeError("The app is stopping; no new AI request can start.")
            task = Task(secrets.token_hex(8), operation, job_id, finish)
            self._tasks[task.id] = task
        try:
            threading.Thread(target=self._thread, args=(task, work), daemon=True,
                             name=f"resuapply-ai-{task.id}").start()
        except RuntimeError:
            # Thread exhaustion must finish the registered request, otherwise every
            # retry finds the same task waiting forever for a worker that never ran.
            task.finished_at = utcnow()
            if task.future.set_running_or_notify_cancel():
                task.future.set_exception(AIError("busy", "The AI request could not start because the app is busy. "
                                                         "Nothing was saved; try again."))
        return task

    def _thread(self, task: Task, work: Work) -> None:
        with self._slots:
            # A request still waiting for a slot when the app stops never starts.
            if self._closed.is_set() or not task.future.set_running_or_notify_cancel():
                task.future.cancel()
                return
            try:
                result = self._run(task, work)
            except BaseException as exc:  # noqa: BLE001  (handed to the page, which reports it)
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)

    def _run(self, task: Task, work: Work):
        try:
            with self._session_factory() as session:
                job = get_by_id(session, Job, task.job_id)
                if job is None:
                    raise Gone("This job was deleted while the request was waiting. Nothing was saved.")
                return work(session, job, get_candidate(session))
        except (AIError, operations.ApprovalNeeded, operations.NothingToDo, Gone):
            raise
        except Exception as exc:
            # The route reports it; the log names only the operation and the kind of failure.
            logger.info("ai task op=%s ended with %s", task.operation, type(exc).__name__)
            raise
        finally:
            task.finished_at = utcnow()

    def _forget_finished(self) -> None:
        cutoff = utcnow() - KEEP_FINISHED
        for task_id in [i for i, t in self._tasks.items() if t.done and t.finished_at and t.finished_at < cutoff]:
            del self._tasks[task_id]

    def get(self, job_id: int, task_id: str) -> Task | None:
        with self._lock:
            self._forget_finished()
            task = self._tasks.get(task_id)
        return task if task is not None and task.job_id == job_id else None

    def running(self, job_id: int) -> list[Task]:
        with self._lock:
            self._forget_finished()
            tasks = [t for t in self._tasks.values() if t.job_id == job_id and not t.done]
        return sorted(tasks, key=lambda t: t.started_at)

    def shutdown(self, timeout: float | None = None) -> None:
        """Start nothing new, then wait a little for running requests before the app closes the model client.

        Requests still running after that end when the client is closed or the process exits, saving nothing.
        """
        self._closed.set()
        with self._lock:
            pending = [t.future for t in self._tasks.values() if not t.done]
        for future in pending:
            future.cancel()  # only those that haven't started
        wait(pending, timeout=SHUTDOWN_SECONDS if timeout is None else timeout)


PROFILE_GONE = "Your profile was removed before the request ran. Create your profile, then start it again."


def with_profile(run: Callable[[Session, Job, Candidate], Any]) -> Work:
    """Work that needs the candidate's profile: refused the same way everywhere if it was removed meanwhile."""
    def work(session: Session, job: Job, candidate: Candidate | None):
        if candidate is None:
            raise Gone(PROFILE_GONE)
        return run(session, job, candidate)
    return work


def start(request: Request, operation: str, job: Job, work: Work, finish: Finish) -> Task:
    return request.app.state.ai_tasks.start(operation, job.id, work, finish)


def respond(request: Request, session: Session, job: Job, task: Task, wait_seconds: float | None = None) -> Response:
    """The request's result page once it has finished; until then, its progress page.

    A form post waits ``WAIT_SECONDS`` first, so a quick result skips the progress page, then
    redirects to that page. The page itself passes ``wait_seconds=0`` and shows the progress.
    """
    wait([task.future], timeout=WAIT_SECONDS if wait_seconds is None else wait_seconds)
    if not task.done:
        if wait_seconds is None:  # a form post: send the browser to the page a reload keeps working on
            return RedirectResponse(task.page, status_code=303)
        return templates.TemplateResponse(request, "ai_task.html", {
            "job": job, "task": task, "refresh": REFRESH_SECONDS, "active": "jobs",
        })
    if task.future.cancelled():
        raise HTTPException(409, "The AI request was stopped before it ran, so nothing was saved. "
                                 "Go back to the job and start it again.")
    if isinstance(task.future.exception(), Gone):
        raise HTTPException(409, str(task.future.exception()))
    # The work committed in its own session: read the job as it is now.
    session.expire_all()
    return task.finish(request, session, job, task)
