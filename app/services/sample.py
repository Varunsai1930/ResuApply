"""Sample data for trying ResuApply: load a fictional profile and job, and remove them again.

Loading never touches a real profile: it is refused once one exists. Everything it adds is
marked ``sample``, and removing takes away exactly that, plus the answers saved to the answer
bank from a sample job. The user's own jobs are kept.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import AnswerBankEntry, Application, Candidate, Job
from ..sample_data import SAMPLE_JOB, SAMPLE_PROFILE, SAMPLE_REQUIREMENTS
from . import jobs as job_service
from . import profile as profile_service
from .requirements import set_requirements


class SampleError(Exception):
    """Sample data can't be loaded now. Nothing was changed."""


@dataclass(frozen=True)
class SampleData:
    """What removing the sample data takes away."""

    candidate: Candidate | None
    jobs: list[Job]
    bank_entries: list[AnswerBankEntry]

    @property
    def present(self) -> bool:
        return self.candidate is not None or bool(self.jobs)


def current(session: Session) -> SampleData:
    candidate = session.scalars(select(Candidate).where(Candidate.sample.is_(True))).first()
    jobs = list(session.scalars(select(Job).where(Job.sample.is_(True)).order_by(Job.id)))
    entries = list(session.scalars(select(AnswerBankEntry).where(
        AnswerBankEntry.source_job_id.in_([job.id for job in jobs])).order_by(AnswerBankEntry.id))) if jobs else []
    return SampleData(candidate, jobs, entries)


def load(session: Session) -> Job:
    """Add the fictional profile and a job with its requirements reviewed. Returns the job."""
    if profile_service.get_candidate(session) is not None:
        raise SampleError("Sample data can only be loaded before you create a profile, so it never mixes with yours.")
    candidate = profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
    candidate.sample = True
    session.commit()
    job = job_service.create(session, job_service.clean_input(**SAMPLE_JOB))
    job.sample = True
    session.commit()
    set_requirements(session, job, copy.deepcopy(SAMPLE_REQUIREMENTS))
    return job


def remove(session: Session) -> SampleData:
    """Remove the sample profile, the sample jobs and bank answers saved from them. Returns what went."""
    found = current(session)
    for entry in found.bank_entries:
        session.delete(entry)
    for job in found.jobs:
        session.delete(job)  # its application and AI results go with it
    if found.candidate is not None:
        # The user's own jobs may have been prepared with the sample profile; they keep their records.
        session.execute(update(Application).where(Application.candidate_id == found.candidate.id)
                        .values(candidate_id=None))
        session.execute(delete(Candidate).where(Candidate.id == found.candidate.id))
    session.commit()
    return found
