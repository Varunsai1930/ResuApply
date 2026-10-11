"""How many accepted resumes and answers cite each profile item, shown beside it on the profile.

Counted from each job's accepted resume (a resume counts once per item, however many of its
sentences cite it) and from accepted answers that cite profile sources. Read only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..models import Job
from ..schemas.resume import ResumeContent


@dataclass(frozen=True)
class Usage:
    resumes: int = 0
    answers: int = 0

    @property
    def text(self) -> str:
        parts = []
        if self.resumes:
            parts.append(f"{self.resumes} resume{'s' if self.resumes != 1 else ''}")
        if self.answers:
            parts.append(f"{self.answers} answer{'s' if self.answers != 1 else ''}")
        return f"cited in {' and '.join(parts)}" if parts else "not cited yet"


def resume_sources(resume: ResumeContent) -> set[str]:
    """Every profile item a resume's sentences cite."""
    claims = [resume.summary] if resume.summary else []
    for entry in [*resume.experience, *resume.projects]:
        claims.extend(entry.bullets)
    return {source for claim in claims for source in claim.sources}


def citations(jobs: list[Job]) -> dict[str, Usage]:
    """Profile item ID -> how many accepted resumes and answers cite it."""
    resumes: Counter[str] = Counter()
    answers: Counter[str] = Counter()
    for job in jobs:
        application = job.application
        if application.accepted_package is not None:
            resumes.update(resume_sources(application.accepted_package.resume.resume))
        for answer in application.answers:
            if not answer.skipped:
                answers.update(set(answer.sources))
    return {key: Usage(resumes[key], answers[key]) for key in resumes.keys() | answers.keys()}
