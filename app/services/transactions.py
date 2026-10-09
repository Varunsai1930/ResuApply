"""One writer at a time for changes to a job's questions, answers and package.

The questions and answers are JSON lists, saved whole. A request that read them, then saved
its edited copy, would silently undo whatever another request saved in between. So every
change starts the same way: take SQLite's writer lock, then reload the rows it is about to
change. Whatever it then reads is the committed state, and no other writer can commit until
this transaction ends.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import Application, Candidate, Job


def lock_and_reload(session: Session, job: Job, candidate: Candidate | None = None) -> None:
    """Hold the database writer lock for the rest of the transaction and reload job, application and candidate.

    The no-op update is what obtains the lock; other writers wait until this transaction ends.
    ``no_autoflush`` keeps unsaved in-memory edits (from a stale copy) from being written before
    the lock is held; the reload then replaces them with the committed rows.
    """
    with session.no_autoflush:
        session.execute(update(Application).where(Application.job_id == job.id)
                        .values(updated_at=Application.updated_at).execution_options(synchronize_session=False))
    session.refresh(job)
    session.refresh(job.application)
    if candidate is not None:
        session.refresh(candidate)


@contextmanager
def write(session: Session, job: Job, candidate: Candidate | None = None) -> Iterator[None]:
    """Lock and reload, run the change, then commit it; any error rolls everything back.

    Validation (tokens from the reviewed page, rules) runs inside the block, against the
    reloaded rows, before anything is changed.
    """
    try:
        lock_and_reload(session, job, candidate)
        yield
        session.commit()
    except BaseException:
        session.rollback()
        raise
