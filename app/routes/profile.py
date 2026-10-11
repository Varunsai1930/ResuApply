"""Profile pages: view, guided edit form, review-changes step and save.

Saving is always two steps: the form posts to /profile/review, which shows the diff,
and only the confirmation on that page posts to /profile/save.
"""

from __future__ import annotations

import json
from enum import StrEnum

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import get_session
from ..services import jobs as job_service
from ..services import profile as profile_service
from ..services import sample as sample_service
from ..services import resume_draft, resume_import
from ..services.citations import Usage, citations
from ..services.profile_gaps import asks_for_facts, gaps
from ..services.profile_form import is_form_shaped, parse_form
from ..templating import templates
from .forms import read_form

router = APIRouter(prefix="/profile")

# A new profile starts with one empty row in each repeating section.
BLANK_PROFILE = {
    "contact": {"links": {}},
    "education": [{}],
    "experience": [{"bullets": [{}]}],
    "projects": [{"bullets": [{}]}],
    "certifications": [],
    "authorization": [{}],
    "preferences": {},
    "availability": {},
}


class Section(StrEnum):
    """A part of the profile that can be edited on its own."""

    CONTACT = "contact"
    EDUCATION = "education"
    EXPERIENCE = "experience"
    PROJECTS = "projects"
    SKILLS = "skills"
    AUTHORIZATION = "authorization"
    PREFERENCES = "preferences"

    @property
    def label(self) -> str:  # not "title": that would hide str.title() on this str enum
        return SECTION_TITLES[self]

    def owns(self, field: str) -> bool:
        """Whether a form field belongs to this section."""
        return any(field == p or field.startswith(p + "-") for p in SECTION_FIELDS[self])


SECTION_TITLES = {
    Section.CONTACT: "Contact and summary",
    Section.EDUCATION: "Education",
    Section.EXPERIENCE: "Experience",
    Section.PROJECTS: "Projects",
    Section.SKILLS: "Skills and certifications",
    Section.AUTHORIZATION: "Work authorization",
    Section.PREFERENCES: "Preferences and availability",
}
# The form field names, or their prefixes, of each section; they are also its keys in the profile data.
SECTION_FIELDS = {
    Section.CONTACT: ("contact", "summary"),
    Section.EDUCATION: ("education",),
    Section.EXPERIENCE: ("experience",),
    Section.PROJECTS: ("projects",),
    Section.SKILLS: ("skills", "skills_absent", "certifications"),
    Section.AUTHORIZATION: ("authorization",),
    Section.PREFERENCES: ("preferences", "availability"),
}


def _section_of(raw: object) -> Section | None:
    """The section a posted form was editing; anything else means the whole profile."""
    try:
        return Section(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return None


def _revision_of(raw: object) -> int | None:
    """The profile revision a section editor was opened at, or None when missing or damaged."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        return None
    return int(raw)


def _render_form(request: Request, data: dict, errors=(), warnings=(), status_code: int = 200, message: str = "",
                 section: Section | None = None, opened_revision: int | None = None, imported: dict | None = None):
    """The profile editor. With ``section``, only that section shows; the others stay in the form as
    hidden fields with their saved values, so the review and save steps are the same as for the
    whole profile. A problem outside the section shows the whole profile, so it can be fixed."""
    errors = list(errors)
    error_fields = {e.field for e in errors if e.field}
    if section and errors and not all(e.field and section.owns(e.field) for e in errors):
        section = None
        message = message or "There's a problem outside this section, so the whole profile is shown."
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "data": data,
            "errors": errors,
            "error_fields": error_fields,
            "warnings": list(warnings),
            "message": message,
            "section": section,
            "sections": list(Section),
            "opened_revision": opened_revision,
            "imported": imported,
            "active": "profile",
        },
        status_code=status_code,
    )


def _load_payload(payload: object) -> dict:
    try:
        data = json.loads(payload) if isinstance(payload, str) else None
    except (json.JSONDecodeError, RecursionError):  # RecursionError: absurdly deep nesting
        data = None
    if not is_form_shaped(data):
        raise HTTPException(400, "The reviewed profile data is missing or damaged. Edit the profile again.")
    return data


@router.get("", response_class=HTMLResponse)
def view_profile(request: Request, msg: str = "", session: Session = Depends(get_session)):
    candidate = profile_service.get_candidate(session)
    jobs = job_service.list_all(session) if candidate else []
    return templates.TemplateResponse(request, "profile_view.html", {
        "candidate": candidate, "msg": msg, "active": "profile",
        "gaps": gaps(jobs, candidate.profile) if candidate else [],
        "asked_jobs": asks_for_facts(jobs),
        "citations": citations(jobs),
        "sample": sample_service.current(session),
        "not_cited": Usage(),
    })


@router.get("/edit", response_class=HTMLResponse)
def edit_profile(request: Request, section: Section | None = None, session: Session = Depends(get_session)):
    """The profile editor, or one section of it. A new profile is always edited whole."""
    candidate = profile_service.get_candidate(session)
    data = candidate.profile.model_dump() if candidate else BLANK_PROFILE
    return _render_form(request, data, section=section if candidate else None,
                        opened_revision=candidate.revision if candidate else None)


@router.post("/edit", response_class=HTMLResponse)
async def back_to_edit(request: Request):
    """Return from the review page to the form without losing the proposed edits."""
    form = await read_form(request)
    return _render_form(request, _load_payload(form.get("payload")), section=_section_of(form.get("section")),
                        opened_revision=_revision_of(form.get("opened_revision")))


@router.post("/review", response_class=HTMLResponse)
async def review_profile(request: Request, session: Session = Depends(get_session)):
    form = await read_form(request)
    section = _section_of(form.get("section"))
    opened = _revision_of(form.get("opened_revision"))
    data = parse_form((k, v) for k, v in form.multi_items()
                      if isinstance(v, str) and k not in ("section", "opened_revision"))
    candidate = profile_service.get_candidate(session)
    if section and candidate and opened != candidate.revision:
        # The other sections were posted as they were when the editor opened. The profile has
        # moved on since, so they would undo newer changes: show them as saved now, keep this
        # section's edits, and let the user review again.
        fresh = candidate.profile.model_dump() | {key: data[key] for key in SECTION_FIELDS[section]}
        message = (f"The profile changed since you opened this section (revision {opened or '?'} → "
                   f"{candidate.revision}). The other sections now show their saved values and your edits to "
                   "this section are kept. Check them, then review again.")
        return _render_form(request, fresh, status_code=409, message=message, section=section,
                            opened_revision=candidate.revision)
    try:
        result = profile_service.review(session, data)
    except profile_service.ProfileInvalid as exc:
        return _render_form(request, data, exc.errors, exc.warnings, status_code=422, section=section,
                            opened_revision=opened)
    return templates.TemplateResponse(
        request,
        "profile_review.html",
        {
            "result": result,
            "payload": json.dumps(data),
            "section": section,
            "opened_revision": opened,
            "active": "profile",
        },
    )


@router.post("/save")
async def save_profile(request: Request, session: Session = Depends(get_session)):
    form = await read_form(request)
    raw_revision = form.get("base_revision")
    try:
        base_revision = int(raw_revision) if isinstance(raw_revision, str) else None
    except ValueError:
        base_revision = None
    if base_revision is None:
        raise HTTPException(422, "The reviewed profile has no valid revision. Edit the profile again.")
    data = _load_payload(form.get("payload"))
    # The form is read here with the editor's limits; the save itself runs in the thread pool,
    # like other synchronous routes, so overlapping saves are serialized by the database.
    return await run_in_threadpool(_save, request, session, data, base_revision)


def _save(request: Request, session: Session, data: dict, base_revision: int):
    try:
        result = profile_service.save(session, data, base_revision=base_revision)
    except profile_service.ProfileInvalid as exc:
        return _render_form(request, data, exc.errors, exc.warnings, status_code=422)
    except profile_service.StaleReview as exc:
        return _render_form(request, data, status_code=409, message=str(exc))
    msg = "profile_saved" if result.saved else "profile_unchanged"
    return RedirectResponse(f"/profile?msg={msg}", status_code=303)


# ---------------------------------------------------------------- importing from a resume file

def _render_import(request: Request, candidate, error: str = "", message: str = "", status_code: int = 200):
    return templates.TemplateResponse(request, "profile_import.html", {
        "candidate": candidate, "error": error, "message": message, "active": "profile",
        "max_mb": resume_import.MAX_BYTES // (1024 * 1024),
    }, status_code=status_code)


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, session: Session = Depends(get_session)):
    return _render_import(request, profile_service.get_candidate(session))


@router.post("/import", response_class=HTMLResponse)
async def import_resume(request: Request, file: UploadFile | None = File(None), session: Session = Depends(get_session)):
    """Read a resume file into the profile form. Nothing is saved: the form and its review step come next.

    With no profile, the draft fills the form. With one, only what it lacks is added (``merge``),
    so an import never changes a fact the user already saved.
    """
    candidate = profile_service.get_candidate(session)
    if candidate is not None and candidate.sample:
        return _render_import(request, candidate, status_code=409,
                              error="Remove the sample data first, then import your own resume.")
    data = await file.read(resume_import.MAX_BYTES + 1) if file is not None else b""
    try:
        text = await run_in_threadpool(resume_import.extract_text, data)
    except resume_import.ResumeImportError as exc:
        return _render_import(request, candidate, error=str(exc), status_code=422)
    draft = resume_draft.draft_profile(text)
    name = ((file.filename if file else "") or "your resume").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:200]
    if candidate is None:
        return _render_form(request, draft, imported={"filename": name, "added": None, "text": text})
    merged = resume_draft.merge(candidate.profile.model_dump(), draft)
    if not merged.added:
        return _render_import(request, candidate, message=f"Nothing new was found in {name}: everything it was "
                              "read to hold is already in your profile.")
    return _render_form(request, merged.data, imported={"filename": name, "added": merged.added, "text": text},
                        opened_revision=candidate.revision)
