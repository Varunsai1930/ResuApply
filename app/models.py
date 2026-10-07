"""Database tables: candidates, jobs, applications and answer_bank (PLAN.md §3).

Columns for later milestones (requirements, proposals, answers, snapshots) exist now so
the schema stays stable, but Milestone 1 only reads and writes the profile, job details
and tracking columns. Their JSON shapes are tightened when those features land.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, PydanticJSON, UTCDateTime, utcnow
from .schemas.profile import Profile
from .schemas.tracking import Note, ReviewState, StatusEvent, TrackingStatus

JSONList = list[dict[str, Any]]
JSONDict = dict[str, Any]


class Candidate(Base):
    """The single local candidate. ``profile`` holds every fact; the user is its only editor."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile: Mapped[Profile] = mapped_column(PydanticJSON(Profile))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Highest ID ever issued per prefix ("exp", "exp-1-b", ...), so deleted IDs are never reused.
    id_counters: Mapped[dict[str, int]] = mapped_column(PydanticJSON(dict[str, int]), default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(2000), default="")
    # The pasted description, kept verbatim. It is untrusted data: shown and quoted, never obeyed.
    description: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    requirements: Mapped[JSONList] = mapped_column(PydanticJSON(JSONList), default=list)
    evidence: Mapped[JSONDict] = mapped_column(PydanticJSON(JSONDict), default=dict)
    overrides: Mapped[JSONDict] = mapped_column(PydanticJSON(JSONDict), default=dict)
    questions: Mapped[JSONList] = mapped_column(PydanticJSON(JSONList), default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )


class Application(Base):
    """Preparation (review state, package) and tracking (status, history, notes) for one job."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    review_state: Mapped[str] = mapped_column(String(20), default=ReviewState.DRAFT.value)
    tracking_status: Mapped[str] = mapped_column(String(20), default=TrackingStatus.SAVED.value)
    status_history: Mapped[list[StatusEvent]] = mapped_column(PydanticJSON(list[StatusEvent]), default=list)
    notes: Mapped[list[Note]] = mapped_column(PydanticJSON(list[Note]), default=list)
    applied_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_proposal: Mapped[JSONDict | None] = mapped_column(PydanticJSON(JSONDict), nullable=True)
    accepted_package: Mapped[JSONDict | None] = mapped_column(PydanticJSON(JSONDict), nullable=True)
    answers: Mapped[JSONList] = mapped_column(PydanticJSON(JSONList), default=list)
    submitted_snapshots: Mapped[JSONList] = mapped_column(PydanticJSON(JSONList), default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="application")

    @property
    def review(self) -> ReviewState:
        return ReviewState(self.review_state)

    @property
    def status(self) -> TrackingStatus:
        return TrackingStatus(self.tracking_status)


class AnswerBankEntry(Base):
    """A reusable approved answer to a non-sensitive question (filled from Milestone 3b)."""

    __tablename__ = "answer_bank"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40))
    sources: Mapped[list[str]] = mapped_column(PydanticJSON(list[str]), default=list)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
