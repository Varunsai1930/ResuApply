"""What Greenhouse autofill may fill: explicit, deterministic mappings from form fields to reviewed data.

The browser reads the form (``autofill_browser``); this module decides, in plain Python, what
each field gets. No model is involved. A field is filled only when every rule agrees:

- it is a visible, editable one-line text, email, phone or link input, or a text box, with
  exactly one label that no other field on the form shares;
- the label is a contact field the profile holds (``questions.classify``: name, email, phone,
  location, LinkedIn, GitHub, website), or it is word for word a question of the approved
  package whose resolved answer is non-sensitive, accepted and not skipped; when both apply they
  must agree;
- neither the label nor the form section it sits in is sensitive (self-identification,
  demographics, authorization, salary, consent and the rest of ``questions.SENSITIVE``);
- the value fits: no line breaks in a one-line field, and no more characters than its maxlength.

The resume file input gets the attached PDF, and a cover letter upload gets the package's accepted
letter as a Word file (when the upload takes Word files). Everything else (choices, drop-downs, search
fields, tick boxes, dates, other uploads, sensitive and ambiguous fields) is listed as left for
the user, with the reason. A field that already holds a different value is kept unless the user
ticks it for replacing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

from . import questions as q_rules
from .answers import measure
from .text import content_hash

MAX_FIELDS = 300  # a form with more controls than this is not a standard application form
TEXT_TYPES = ("text", "email", "tel", "url", "search", "")
CONTACT_KEYS = ("name", "email", "phone", "location", "linkedin", "github", "portfolio")
FILLABLE_CATEGORIES = (q_rules.FACTUAL, q_rules.OPEN)

# Plan actions
FILL, OCCUPIED, SAME, ATTACH, LEAVE = "fill", "occupied", "same", "attach", "leave"
# Which file an upload gets
RESUME_UPLOAD, COVER_LETTER_UPLOAD = "resume", "cover_letter"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_RESUME = re.compile(r"\b(?:resume|résumé|cv|curriculum vitae)\b", re.IGNORECASE)
_COVER = re.compile(r"\bcover\s*letter\b", re.IGNORECASE)
_TRAILING_MARKS = re.compile(r"(?:\s*(?:\*|\((?:required|optional)\)))+$", re.IGNORECASE)


def norm_label(text: str) -> str:
    """A label as compared: composed characters, single spaces, case folded, without a trailing * or (required)."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()
    return _TRAILING_MARKS.sub("", text).strip().casefold()


def norm_value(text: str) -> str:
    """A field value as compared: line endings as a text box keeps them, outer whitespace aside."""
    return (text or "").replace("\r\n", "\n").strip()


@dataclass(frozen=True)
class FormField:
    """One control of the application form, as the page described it. Untrusted: bounded on the way in."""

    key: str  # "id:…" or "name:…" when the control can be found again unambiguously; "index:…" otherwise
    tag: str
    type: str = ""
    role: str = ""
    label: str = ""
    labels: int = 1  # how many different label texts point at it
    required: bool = False
    visible: bool = True
    editable: bool = True  # not disabled or read-only
    value: str = ""
    max_length: int | None = None
    sensitive_section: bool = False  # inside a self-identification or demographic section
    autocomplete: bool = False  # a search field that offers choices (aria-autocomplete, a datalist)
    accept: str = ""  # a file input's accept attribute, lower case ("" accepts anything)

    @classmethod
    def from_page(cls, raw: object) -> FormField | None:
        if not isinstance(raw, dict):
            return None

        def text(name: str, limit: int) -> str:
            value = raw.get(name)
            return value[:limit] if isinstance(value, str) else ""

        max_length = raw.get("maxLength")
        labels = raw.get("labels")
        key = text("key", 300)
        if not key:
            return None
        return cls(
            key=key, tag=text("tag", 20).lower(), type=text("type", 30).lower(), role=text("role", 30).lower(),
            label=text("label", 2000), labels=labels if isinstance(labels, int) and labels >= 0 else 0,
            required=raw.get("required") is True, visible=raw.get("visible") is True,
            editable=raw.get("editable") is True, value=text("value", 20000),
            max_length=max_length if isinstance(max_length, int) and max_length > 0 else None,
            sensitive_section=raw.get("sensitiveSection") is True, autocomplete=raw.get("autocomplete") is True,
            accept=text("accept", 300).lower(),
        )

    @property
    def addressable(self) -> bool:
        return self.key.startswith(("id:", "name:"))

    @property
    def is_file(self) -> bool:
        return self.tag == "input" and self.type == "file"

    @property
    def is_text(self) -> bool:
        if self.role in ("combobox", "listbox") or self.autocomplete:
            return False
        return self.tag == "textarea" or (self.tag == "input" and self.type in TEXT_TYPES)

    @property
    def multiline(self) -> bool:
        return self.tag == "textarea"


@dataclass(frozen=True)
class PackageAnswer:
    """One question of the approved package with its resolved answer."""

    question: str
    category: str
    text: str
    resolved: bool
    skipped: bool


@dataclass(frozen=True)
class FillData:
    """The reviewed data autofill may use: the approved package's contact details, answers and resume PDF."""

    contact: dict[str, str]  # CONTACT_KEYS -> value, from the approved resume
    answers: tuple[PackageAnswer, ...]
    resume_file: str | None  # the attached PDF's file name, None without one
    resume_size: int = 0
    cover_letter_file: str | None = None  # the Word file made from the package's accepted letter, None without one
    package_hash: str = ""
    resume_sha256: str = ""


@dataclass(frozen=True)
class PlanItem:
    key: str
    label: str
    action: str  # FILL, OCCUPIED, SAME, ATTACH or LEAVE
    value: str = ""  # what would be filled (the file name, for the resume)
    existing: str = ""  # what the field held when it was inspected
    source: str = ""  # where the value comes from, in words
    reason: str = ""  # why a field is left for the user
    file: bool = False  # a file upload: ``value`` is the file's name
    upload: str = ""  # which file: RESUME_UPLOAD or COVER_LETTER_UPLOAD

    @property
    def fillable(self) -> bool:
        return self.action in (FILL, OCCUPIED, ATTACH)


def _shown_label(field: FormField) -> str:
    label = re.sub(r"\s+", " ", field.label).strip()
    label = _TRAILING_MARKS.sub("", label).strip()
    return label[:200] or "A field without a label"


def _leave(field: FormField, reason: str) -> PlanItem:
    return PlanItem(field.key, _shown_label(field), LEAVE, existing=field.value[:500], reason=reason)


def _widget_reason(field: FormField) -> str:
    sensitive = q_rules.classify(field.label)[0] in q_rules.SENSITIVE_CATEGORIES
    if field.type in ("checkbox", "radio"):
        return ("A consent or declaration: only you can give it." if sensitive
                else "A tick box or choice: choose it yourself.")
    if field.tag == "select" or field.role in ("combobox", "listbox") or field.autocomplete:
        return "A drop-down or search field: choose it yourself."
    if field.tag in ("input", "textarea"):
        return f"A {field.type or field.tag} field, which ResuApply doesn't fill: enter it yourself."
    return "A field ResuApply doesn't fill: enter it yourself."


def _contact_value(label: str, data: FillData) -> tuple[str | None, str]:
    """The profile value a contact-field label asks for, and its source in words; None when it isn't one."""
    category, key = q_rules.classify(label)
    if category != q_rules.FACTUAL or key not in CONTACT_KEYS:
        return None, ""
    value = (data.contact.get(key) or "").strip()
    return (value or None), f"Profile: {key}"


def takes_word_files(accept: str) -> bool:
    """Whether a file input's accept attribute lets a .docx file through ("" accepts anything)."""
    kinds = {kind.strip() for kind in accept.split(",") if kind.strip()}
    return not kinds or bool(kinds & {".docx", DOCX_TYPE, "application/*", "*/*", "*"})


def _is_cover_letter_upload(field: FormField) -> bool:
    return bool(_COVER.search(field.label)) or field.key in ("id:cover_letter", "name:cover_letter")


def _cover_letter_item(field: FormField, data: FillData, cover_fields: int) -> PlanItem:
    if cover_fields > 1:
        return _leave(field, "The form has more than one cover letter upload, so ResuApply can't tell which to use.")
    if data.cover_letter_file is None:
        return _leave(field, "Your approved package has no cover letter: attach one yourself if you want to.")
    if not takes_word_files(field.accept):
        return _leave(field, "This upload doesn't take Word files. Save the cover letter as a PDF from its print "
                             "view and attach it yourself.")
    return PlanItem(field.key, _shown_label(field), OCCUPIED if field.value else ATTACH,
                    value=data.cover_letter_file, existing=field.value[:500],
                    source="The cover letter in your approved package, as a Word file", file=True,
                    upload=COVER_LETTER_UPLOAD)


def _file_item(field: FormField, data: FillData, resume_fields: int, cover_fields: int) -> PlanItem:
    label = field.label or field.key
    if _is_cover_letter_upload(field):
        return _cover_letter_item(field, data, cover_fields)
    is_resume = _RESUME.search(label) or field.key in ("id:resume", "name:resume")
    if not is_resume:
        return _leave(field, "A file upload: attach it yourself.")
    if resume_fields > 1:
        return _leave(field, "The form has more than one resume upload, so ResuApply can't tell which to use.")
    if data.resume_file is None:
        return _leave(field, "Attach the PDF saved from your package in ResuApply first.")
    return PlanItem(field.key, _shown_label(field) if field.label else "Resume", OCCUPIED if field.value else ATTACH,
                    value=data.resume_file, existing=field.value[:500],
                    source="The PDF you attached to the approved package", file=True, upload=RESUME_UPLOAD)


def plan(fields: list[FormField], data: FillData) -> list[PlanItem]:
    """What happens to each visible field (and each file input, often hidden behind a button), in form order."""
    shown = [f for f in fields if f.visible or f.is_file]
    label_counts: dict[str, int] = {}
    for field in shown:
        if field.label:
            label_counts[norm_label(field.label)] = label_counts.get(norm_label(field.label), 0) + 1
    answers: dict[str, list[PackageAnswer]] = {}
    for answer in data.answers:
        answers.setdefault(norm_label(answer.question), []).append(answer)
    resume_fields = sum(1 for f in shown if f.is_file and _RESUME.search(f.label or f.key)
                        and not _COVER.search(f.label))
    cover_fields = sum(1 for f in shown if f.is_file and _is_cover_letter_upload(f))
    items = []
    for field in shown:
        items.append(_item(field, data, label_counts, answers, resume_fields, cover_fields))
    return items


def _item(field: FormField, data: FillData, label_counts: dict[str, int], answers: dict[str, list[PackageAnswer]],
          resume_fields: int, cover_fields: int = 0) -> PlanItem:
    label = norm_label(field.label)
    if not field.addressable:
        return _leave(field, "ResuApply can't find this field again reliably: fill it yourself.")
    if not field.editable:
        return _leave(field, "Locked on the employer's form.")
    if field.is_file:
        return _file_item(field, data, resume_fields, cover_fields)
    if field.sensitive_section or (label and q_rules.classify(field.label)[0] in q_rules.SENSITIVE_CATEGORIES):
        if field.type in ("checkbox", "radio"):
            return _leave(field, "A consent or declaration: only you can give it.")
        return _leave(field, "A sensitive question: answer it yourself.")
    if not field.is_text:
        return _leave(field, _widget_reason(field))
    if not label:
        return _leave(field, "This field has no label, so ResuApply can't tell what it asks.")
    if field.labels != 1 or label_counts.get(label, 0) > 1:
        return _leave(field, "More than one label or field matches here, so ResuApply can't tell what it asks.")
    matched = answers.get(label, [])
    contact, contact_source = _contact_value(field.label, data)
    value, source = contact, contact_source
    if matched:
        texts = {norm_value(a.text) for a in matched}
        if len(matched) > 1 and len(texts) > 1:
            return _leave(field, "Your package has more than one question with this label and different answers.")
        answer = matched[0]
        if answer.category in q_rules.SENSITIVE_CATEGORIES:
            return _leave(field, "A sensitive question: answer it yourself.")
        if answer.skipped:
            return _leave(field, "You skipped this optional question in your package.")
        if answer.category not in FILLABLE_CATEGORIES or not answer.resolved or not answer.text.strip():
            return _leave(field, "Your package has no approved answer for it.")
        if contact is not None and norm_value(contact) != norm_value(answer.text):
            return _leave(field, "Your profile and your package's answer differ for this field.")
        value, source = answer.text, "Your approved answer"
    if value is None:
        return _leave(field, "Not in your approved package: answer it yourself.")
    if not field.multiline and "\n" in norm_value(value):
        return _leave(field, "Your answer has line breaks, which this one-line field can't hold.")
    size = measure(value, "chars")
    if field.max_length is not None and size > field.max_length:
        return _leave(field, f"Your answer has {size} characters; this field takes {field.max_length}.")
    item = PlanItem(field.key, _shown_label(field), FILL, value=value, existing=field.value[:500], source=source)
    if not norm_value(field.value):
        return item
    if norm_value(field.value) == norm_value(value):
        return PlanItem(item.key, item.label, SAME, value, item.existing, source)
    return PlanItem(item.key, item.label, OCCUPIED, value, item.existing, source)


def structure(fields: list[FormField]) -> str:
    """The form's shape, values aside: which controls, of which kind, with which labels, in which order."""
    return content_hash([[f.key, f.tag, f.type, f.role, norm_label(f.label), f.visible, f.editable, f.autocomplete,
                          f.sensitive_section] for f in fields])


def plan_token(form_structure: str, items: list[PlanItem], data: FillData) -> str:
    """What a Fill button vouches for: the form's shape, what each field would get, the package and the PDF."""
    # What the field holds now is left out: the user may type in the browser before choosing Fill, and
    # each field's current value is checked again then (see ``autofill``).
    planned = [{k: v for k, v in asdict(item).items() if k != "existing"}
               | {"action": LEAVE if item.action == LEAVE else "target"} for item in items]
    return content_hash({"structure": form_structure, "plan": planned, "package": data.package_hash,
                         "resume": data.resume_sha256})
