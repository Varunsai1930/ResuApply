"""Importing a job from Greenhouse's public Job Board API. The app's only outbound fetch besides the AI provider.

A fetch happens only when the user asks for it on the Add a job page. The rules are strict:

- only a posting link on ``boards.greenhouse.io`` or ``job-boards.greenhouse.io`` over https is
  accepted, and only its board name and job number are read from it;
- the request always goes to ``boards-api.greenhouse.io``, built here, never to the link itself;
- redirects are refused, the whole exchange has a deadline, and the response is read up to a size limit;
- every failure is a ``GreenhouseError`` with a plain message, raised before anything is saved.

Everything fetched is untrusted data: the description is stored as the job's text and the
question labels as question text, shown escaped and never obeyed.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Job
from ..schemas.posting_import import ImportedQuestion, ImportPreview, LeftOut, PostingImport, QuestionField
from . import answers
from . import questions as q_rules
from .answers import QUESTION_LIMIT
from .text import clean_text

HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"
TIMEOUT_SECONDS = 10.0  # for the whole exchange, not per read
# httpx limits each phase on its own, so each gets a share: connecting, sending the request and waiting for
# the headers take at most 9 seconds together, and a read of the body starts only while one fits.
PHASE_SECONDS = 3.0
MAX_BYTES = 2 * 1024 * 1024  # a posting with its questions is tens of kilobytes

_BOARD = re.compile(r"[A-Za-z0-9_-]{1,100}")
_JOB_ID = re.compile(r"[0-9]{1,20}")
_JOB_PATH = re.compile(r"/([A-Za-z0-9_-]{1,100})/jobs/([0-9]{1,20})/?")


class GreenhouseError(Exception):
    """The posting couldn't be imported. Nothing was saved or changed."""


@dataclass(frozen=True)
class PostingRef:
    board: str
    job_id: str

    @property
    def api_url(self) -> str:
        return API.format(board=self.board, job_id=self.job_id)


@dataclass(frozen=True)
class Posting:
    title: str
    company: str
    location: str
    description: str  # plain text, from the posting's HTML


def posting_ref(url: str) -> PostingRef | None:
    """The board and job a Greenhouse posting link names, or None for any other link.

    Accepts ``https://boards.greenhouse.io/{board}/jobs/{id}`` (and the same on
    ``job-boards.greenhouse.io``), with or without a query, and the embedded form
    ``/embed/job_app?for={board}&token={id}``. Anything else (another host, http, a port,
    a user name, another path) is not a Greenhouse posting.
    """
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or (parts.hostname or "") not in HOSTS or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        return None
    if match := _JOB_PATH.fullmatch(parts.path):
        return PostingRef(match.group(1), match.group(2))
    if parts.path.rstrip("/") == "/embed/job_app":
        query = parse_qs(parts.query)
        board, job_id = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
        if _BOARD.fullmatch(board) and _JOB_ID.fullmatch(job_id):
            return PostingRef(board, job_id)
    return None


# ---------------------------------------------------------------- fetching

def fetch(ref: PostingRef, transport: httpx.BaseTransport | None = None) -> dict:
    """The posting's JSON, read within TIMEOUT_SECONDS in all and up to MAX_BYTES.

    ``transport`` replaces the network in tests.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    body = bytearray()

    def time_for_a_read() -> None:
        if time.monotonic() > deadline - PHASE_SECONDS:  # the read may wait PHASE_SECONDS for its data
            raise httpx.ReadTimeout("deadline passed")

    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(PHASE_SECONDS), follow_redirects=False) as client:
            with client.stream("GET", ref.api_url, headers={"Accept": "application/json"}) as response:
                if response.status_code == 404:
                    raise GreenhouseError("Greenhouse has no such job. The posting may have closed; check the link.")
                if response.is_redirect:
                    raise GreenhouseError("Greenhouse answered with a redirect, which isn't followed. Nothing was "
                                          "imported; paste the posting instead.")
                if response.status_code != 200:
                    raise GreenhouseError(f"Greenhouse answered with an error ({response.status_code}). Nothing was "
                                          "imported; try again later or paste the posting instead.")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise _too_large()
                time_for_a_read()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise _too_large()
                    time_for_a_read()
    except httpx.TimeoutException:
        raise GreenhouseError(f"Greenhouse took longer than {TIMEOUT_SECONDS:.0f} seconds. Nothing was imported; "
                              "try again, or paste the posting instead.") from None
    except httpx.HTTPError:
        raise GreenhouseError("Greenhouse couldn't be reached. Check your connection, or paste the posting "
                              "instead.") from None
    try:
        data = json.loads(bytes(body))
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise _malformed()
    return data


def _too_large() -> GreenhouseError:
    return GreenhouseError(f"Greenhouse's answer was over {MAX_BYTES // (1024 * 1024)} MB, which is not a job "
                           "posting. Nothing was imported; paste the posting instead.")


def _malformed() -> GreenhouseError:
    return GreenhouseError("Greenhouse's answer wasn't a job posting ResuApply can read. Nothing was imported; "
                           "paste the posting instead.")


# ---------------------------------------------------------------- reading the posting

_BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
           "blockquote", "pre", "hr"}
_HIDDEN = {"script", "style", "template", "head"}


class _Text(HTMLParser):
    """The text of an HTML fragment: block elements start new lines, list items get a bullet."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self.hidden += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in _BLOCKS and tag != "li":  # the next item starts its own line: no blank line between items
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(fragment: str) -> str:
    """Plain text from the posting's content, which the API sends as escaped HTML."""
    parser = _Text()
    parser.feed(html.unescape(fragment or ""))
    parser.close()
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in clean_text("".join(parser.parts)).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _text(value: object, limit: int = 2000) -> str:
    """A short value from the posting as one clean line; anything that isn't text is empty."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", clean_text(html.unescape(value))).strip()[:limit]


def read_posting(data: dict) -> Posting:
    """The job's details and description from the API's JSON. A posting without a title or text is refused."""
    location = data.get("location")
    posting = Posting(
        title=_text(data.get("title"), 200),
        company=_text(data.get("company_name"), 200),
        location=_text(location.get("name") if isinstance(location, dict) else "", 200),
        description=html_text(data["content"]) if isinstance(data.get("content"), str) else "",
    )
    if not posting.title or not posting.description:
        raise _malformed()
    return posting


# ---------------------------------------------------------------- the application questions

MAX_QUESTIONS = 100  # more than this is not one application form
MAX_LEFT_OUT = 50  # fields listed as left for the employer's form; the rest are counted
OPTION_LIMIT = 100  # choices kept per question, each up to 200 characters

# Greenhouse field types ResuApply can prepare an answer for, and how the Questions step names them.
FIELD_TYPES = {
    "input_text": "short_text",
    "textarea": "long_text",
    "multi_value_single_select": "single_select",
    "multi_value_multi_select": "multi_select",
}
FIELD_LABELS = {
    "short_text": "Short text",
    "long_text": "Long text",
    "single_select": "Choose one",
    "multi_select": "Choose any",
    "unsupported": "A field ResuApply doesn't know",
}
_RESUME_FIELDS = ("resume", "resume_text")


def _options(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    labels = (_text(v.get("label"), 200) for v in values[:OPTION_LIMIT] if isinstance(v, dict))
    return tuple(label for label in labels if label)


def _form_question(item: dict, sensitive: bool) -> ImportedQuestion | LeftOut | None:
    """A question from the ``questions``, ``location_questions`` or compliance lists; None for a hidden field."""
    label = _text(item.get("label"), QUESTION_LIMIT + 1)
    fields = item.get("fields") if isinstance(item.get("fields"), list) else []
    fields = [f for f in fields if isinstance(f, dict)]
    types = [f.get("type") if isinstance(f.get("type"), str) else "" for f in fields]
    if fields and all(t == "input_hidden" for t in types):
        return None  # filled in by the form itself, not asked of the applicant
    if not label:
        return LeftOut("A question without a label", "Check the employer's form for it.")
    if any(f.get("name") in _RESUME_FIELDS for f in fields):
        return LeftOut(label, "Attach the resume from your approved package on the employer's form.")
    if len(label) > QUESTION_LIMIT:
        return LeftOut(label[:200] + "…", "Too long to add as a question; answer it on the employer's form.")
    supported = next((f for f, t in zip(fields, types) if t in FIELD_TYPES), None)
    if supported is not None:
        return ImportedQuestion(label, item.get("required") is True, FIELD_TYPES[supported["type"]],
                                _options(supported.get("values")), sensitive)
    if "input_file" in types:
        return LeftOut(label, "A file upload: attach it on the employer's form.")
    unknown = next((t for t in types if t and t != "input_hidden"), "")
    return ImportedQuestion(label, item.get("required") is True, "unsupported", (), sensitive, unknown[:100])


def _demographic_question(item: dict) -> ImportedQuestion | LeftOut | None:
    label = _text(item.get("label"), QUESTION_LIMIT + 1)
    if not label:
        return None
    kind = item.get("type") if isinstance(item.get("type"), str) else ""
    field_type = FIELD_TYPES.get(kind, "unsupported")
    return ImportedQuestion(label[:QUESTION_LIMIT], item.get("required") is True, field_type,
                            _options(item.get("answer_options")), True, "" if kind in FIELD_TYPES else kind[:100])


def read_questions(data: dict) -> tuple[list[ImportedQuestion], list[LeftOut]]:
    """The posting's application questions in form order, and the fields that aren't added as questions.

    Text and choice fields become questions with their required flag and field type. Questions
    from the self-identification and demographic sections are always Sensitive. A field of a
    type ResuApply doesn't know is still added, for the user to confirm its category; file
    uploads (the resume among them) and hidden fields are not questions to answer here.
    """
    found: list[ImportedQuestion | LeftOut | None] = []
    sections = [(data.get(key), False) for key in ("questions", "location_questions")]
    compliance = data.get("compliance") if isinstance(data.get("compliance"), list) else []
    sections += [(section.get("questions"), True) for section in compliance if isinstance(section, dict)]
    for items, sensitive in sections:
        if isinstance(items, list):
            found.extend(_form_question(item, sensitive) for item in items if isinstance(item, dict))
    demographic = data.get("demographic_questions")
    items = demographic.get("questions") if isinstance(demographic, dict) else None
    if isinstance(items, list):
        found.extend(_demographic_question(item) for item in items if isinstance(item, dict))
    questions = [q for q in found if isinstance(q, ImportedQuestion)]
    left_out = [q for q in found if isinstance(q, LeftOut)]
    if len(left_out) > MAX_LEFT_OUT:
        rest = len(left_out) - MAX_LEFT_OUT
        left_out = [*left_out[:MAX_LEFT_OUT], LeftOut(f"{rest} more fields", "Not listed here; check the employer's form.")]
    if len(questions) > MAX_QUESTIONS:
        left_out.append(LeftOut(f"{len(questions) - MAX_QUESTIONS} more questions",
                                f"Only the first {MAX_QUESTIONS} are added; answer the rest on the employer's form."))
        questions = questions[:MAX_QUESTIONS]
    return questions, left_out


def category(question: ImportedQuestion) -> str | None:
    """The category to add an imported question with, or None to use the one the rules detect.

    Self-identification and demographic questions are Sensitive whatever their wording. A field
    ResuApply doesn't know is Unrecognized, so the user confirms it, unless the rules already
    read it as sensitive (which nothing may override).
    """
    detected, _ = q_rules.classify(question.label)
    if question.sensitive:
        return None if detected in q_rules.SENSITIVE_CATEGORIES else q_rules.SENSITIVE
    if question.field_type == "unsupported" and detected not in q_rules.SENSITIVE_CATEGORIES:
        return q_rules.UNKNOWN
    return None


# ---------------------------------------------------------------- reviewing and saving an import

_PREVIEW = TypeAdapter(ImportPreview)


def import_posting(url: str, transport: httpx.BaseTransport | None = None) -> tuple[Posting, ImportPreview]:
    """Fetch and read the posting a Greenhouse link names: its details, and its questions for review."""
    ref = posting_ref(url)
    if ref is None:
        raise GreenhouseError("Only a Greenhouse posting link can be imported: https://boards.greenhouse.io/… or "
                              "https://job-boards.greenhouse.io/…, ending in /jobs/ and a number. Other links are "
                              "saved as a reference; paste the posting instead.")
    data = fetch(ref, transport)
    posting = read_posting(data)
    try:
        questions, left_out = read_questions(data)
        return posting, ImportPreview(ref.board, ref.job_id, utcnow(), tuple(questions), tuple(left_out))
    except ValidationError:  # anything the posting holds beyond what a preview keeps is refused, not a crash
        raise _malformed() from None


def preview_json(preview: ImportPreview) -> str:
    return _PREVIEW.dump_json(preview).decode()


def read_preview(raw: object) -> ImportPreview | None:
    """The import carried by the Add a job form, or None when there is none or it is damaged."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _PREVIEW.validate_json(raw)
    except (ValidationError, ValueError, RecursionError):
        return None


def save_import(session: Session, job: Job, preview: ImportPreview) -> None:
    """Add the reviewed questions to a newly saved job and keep each one's form field.

    Each question is added as the user would add it (``answers.add_question``), so its category
    is detected by the same rules and a sensitive question can't be made anything else; only
    ``category`` narrows it further. One that is refused is listed as left out instead.
    """
    fields: dict[str, QuestionField] = {}
    left_out = list(preview.left_out)
    for question in preview.questions:
        try:
            added = answers.add_question(session, job, question.label, required=question.required,
                                         category=category(question))
        except answers.AnswerError as exc:
            left_out.append(LeftOut(question.label[:200], str(exc)[:300]))
            continue
        fields[added.id] = QuestionField(field_type=question.field_type, options=list(question.options),
                                         greenhouse_type=question.greenhouse_type)
    job.posting_import = PostingImport(board=preview.board, job_id=preview.job_id, fetched_at=preview.fetched_at,
                                       fields=fields, left_out=left_out)
    session.commit()
