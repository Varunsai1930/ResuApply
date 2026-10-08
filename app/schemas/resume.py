"""Source-backed resume content and separately stored proposals/accepted drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Claim(_Model):
    text: Text
    sources: list[Identifier] = Field(min_length=1)


class ResumeEntry(_Model):
    entry: Identifier
    bullets: list[Claim] = Field(default_factory=list)


class ResumeDraft(_Model):
    summary: Claim | None = None
    experience: list[ResumeEntry] = Field(default_factory=list)
    projects: list[ResumeEntry] = Field(default_factory=list)
    education: list[Identifier] | None = None
    certifications: list[Identifier] | None = None
    skills: list[Identifier] | None = None


class ResumeContent(ResumeDraft):
    education: list[Identifier] = Field(default_factory=list)
    certifications: list[Identifier] = Field(default_factory=list)
    skills: list[Identifier] = Field(default_factory=list)


class ResumeRecord(_Model):
    resume: ResumeContent
    profile_revision: int = Field(ge=1)
    job_revision: int = Field(ge=1)
    model: str = ""
    prompt_revision: str = ""
    outbound_hash: str | None = None
    created_at: datetime
    warnings: list[str] = Field(default_factory=list)


class ResumePackage(_Model):
    resume: ResumeRecord
    accepted_at: datetime
