"""Database tables: candidates, jobs, applications and answer_bank (PLAN.md §3),
plus ai_runs (validated AI results and their metadata) and outbound_approvals (what the
user approved for sending to the model).

Columns for later milestones (proposals, answers, snapshots) exist already; their JSON
shapes are tightened when those features land.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, PydanticJSON, UTCDateTime, utcnow
from .schemas.package import Answer, AnswerDraft, Approval, Question, SubmittedSnapshot
from .schemas.profile import Profile
from .schemas.requirements import EvidenceLink, Override, Requirement
from .schemas.resume import ResumePackage, ResumeRecord
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
    requirements: Mapped[list[Requirement]] = mapped_column(PydanticJSON(list[Requirement]), default=list)
    # Highest requirement number ever issued ("r7" -> 7), so deleted requirement IDs are never reused.
    requirement_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # requirement ID -> evidence the user confirmed; requirement ID -> manual status with a reason
    evidence: Mapped[dict[str, EvidenceLink]] = mapped_column(PydanticJSON(dict[str, EvidenceLink]), default=dict)
    overrides: Mapped[dict[str, Override]] = mapped_column(PydanticJSON(dict[str, Override]), default=dict)
    questions: Mapped[list[Question]] = mapped_column(PydanticJSON(list[Question]), default=list)
    # Highest question number ever issued ("q4" -> 4), so removed question IDs are never reused.
    question_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    current_proposal: Mapped[ResumeRecord | None] = mapped_column(PydanticJSON(ResumeRecord), nullable=True)
    accepted_package: Mapped[ResumePackage | None] = mapped_column(PydanticJSON(ResumePackage), nullable=True)
    answers: Mapped[list[Answer]] = mapped_column(PydanticJSON(list[Answer]), default=list)
    # AI proposals for open questions; not answers until the user accepts them.
    answer_drafts: Mapped[list[AnswerDraft]] = mapped_column(PydanticJSON(list[AnswerDraft]), default=list, server_default="[]")
    approval: Mapped[Approval | None] = mapped_column(PydanticJSON(Approval), nullable=True)
    # The approval rules version (services.package.APPROVAL_RULES) an approval passed, and which
    # approval that was (services.package.approval_key). Plain columns, not part of the approval
    # JSON, so earlier versions of the app can read the row. Those versions don't update them, so
    # the version only counts for the approval whose key matches.
    approval_rules: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    approval_rules_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_snapshots: Mapped[list[SubmittedSnapshot]] = mapped_column(PydanticJSON(list[SubmittedSnapshot]), default=list)
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


class AIRun(Base):
    """A validated AI result, reused while its inputs, model and prompt revision are unchanged.

    Only results that passed local validation are stored. Prompts and raw responses are not.
    """

    __tablename__ = "ai_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String(40))
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(200))
    prompt_revision: Mapped[str] = mapped_column(String(40))
    # e.g. {"job": 3, "profile": 5, "outbound": "a1b2..."}: what the result was built from
    input_revisions: Mapped[JSONDict] = mapped_column(PydanticJSON(JSONDict), default=dict)
    result: Mapped[JSONDict] = mapped_column(PydanticJSON(JSONDict))
    usage: Mapped[JSONDict] = mapped_column(PydanticJSON(JSONDict), default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OutboundApproval(Base):
    """The user's approval of the career content that may be sent to the model.

    ``base_hash`` identifies the reduced context the user reviewed. When the profile changes
    so that the reduced context differs, the approval no longer applies and is asked for again.
    """

    __tablename__ = "outbound_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), unique=True)
    base_hash: Mapped[str] = mapped_column(String(32))
    excluded: Mapped[list[str]] = mapped_column(PydanticJSON(list[str]), default=list)
    edits: Mapped[dict[str, str]] = mapped_column(PydanticJSON(dict[str, str]), default=dict)
    profile_revision: Mapped[int] = mapped_column(Integer)
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
