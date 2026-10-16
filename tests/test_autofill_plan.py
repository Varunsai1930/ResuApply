"""The deterministic field mappings behind Greenhouse autofill (no browser needed). Fictional data only."""

from __future__ import annotations

from app.services import autofill_plan as ap
from app.services.autofill_plan import FillData, FormField, PackageAnswer


def data(*answers: PackageAnswer, resume: str | None = "Resume.pdf", **contact) -> FillData:
    values = {"name": "Jordan Example", "email": "jordan@example.com", "phone": "+1 555 0100", "location": "",
              "linkedin": "linkedin.com/in/jordan-example", "github": "", "portfolio": ""} | contact
    return FillData(contact=values, answers=answers, resume_file=resume, resume_size=1234, package_hash="p",
                    resume_sha256="s")


def answer(question: str, text: str, category: str = "open", resolved: bool = True, skipped: bool = False):
    return PackageAnswer(question, category, text, resolved, skipped)


def text(key: str, label: str, **kw) -> FormField:
    return FormField(key=f"id:{key}", tag="input", type=kw.pop("type", "text"), label=label, **kw)


def actions(fields, fill_data=None) -> dict[str, tuple[str, str]]:
    return {i.label: (i.action, i.value or i.reason) for i in ap.plan(fields, fill_data or data())}


def test_contact_fields_and_links_come_from_the_approved_resume():
    result = actions([text("email", "Email*", type="email"), text("phone", "Phone", type="tel"),
                      text("q1", "LinkedIn Profile"), text("q2", "GitHub"), text("name", "Full name")])
    assert result == {"Email": ("fill", "jordan@example.com"), "Phone": ("fill", "+1 555 0100"),
                      "LinkedIn Profile": ("fill", "linkedin.com/in/jordan-example"),
                      "GitHub": ("leave", "Not in your approved package: answer it yourself."),
                      "Full name": ("fill", "Jordan Example")}


def test_first_and_last_names_only_from_the_users_own_answers():
    fields = [text("first_name", "First Name"), text("last_name", "Last Name")]
    assert actions(fields)["First Name"][0] == "leave"  # the full name is never split
    result = actions(fields, data(answer("First Name", "Jordan", "factual"), answer("Last name", "Example", "factual")))
    assert result == {"First Name": ("fill", "Jordan"), "Last Name": ("fill", "Example")}


def test_custom_questions_match_the_package_word_for_word():
    fields = [FormField(key="id:q3", tag="textarea", label="Why do you want to work here? *"),
              text("q4", "Why do you want to work here, really?"), text("q5", "How did you hear about us?")]
    result = actions(fields, data(answer("why do you  want to work here?", "Line one.\nLine two.")))
    assert result["Why do you want to work here?"] == ("fill", "Line one.\nLine two.")
    assert result["Why do you want to work here, really?"][0] == "leave"
    assert result["How did you hear about us?"] == ("leave", "Not in your approved package: answer it yourself.")


def test_sensitive_questions_and_sections_are_left_even_with_an_answer():
    fields = [text("q1", "Are you legally authorized to work in the United States?"),
              text("q2", "Desired salary"), text("q3", "Pronouns"),
              text("q4", "Anything else?", sensitive_section=True),
              FormField(key="id:c", tag="input", type="checkbox", label="I agree to the privacy policy")]
    fill_data = data(answer("Are you legally authorized to work in the United States?", "Yes", "sensitive_factual"),
                     answer("Desired salary", "Plenty", "sensitive"), answer("Anything else?", "No"))
    result = actions(fields, fill_data)
    assert all(action == "leave" for action, _ in result.values())
    assert result["Desired salary"][1] == "A sensitive question: answer it yourself."
    assert result["I agree to the privacy policy"][1] == "A consent or declaration: only you can give it."


def test_unsupported_controls_are_left_with_a_reason():
    fields = [FormField(key="id:s", tag="select", label="Country"),
              text("loc", "Location (City)", role="combobox", autocomplete=True),
              text("d", "Start date", type="date"), text("n", "Years of experience", type="number"),
              FormField(key="id:r", tag="input", type="radio", label="Remote"),
              FormField(key="id:cl", tag="input", type="file", label="Cover Letter"),
              FormField(key="id:t", tag="input", type="file", label="Transcript")]
    reasons = {label: reason for label, (action, reason) in actions(fields).items()}
    assert reasons["Country"] == reasons["Location (City)"] == "A drop-down or search field: choose it yourself."
    assert "date field" in reasons["Start date"] and "number field" in reasons["Years of experience"]
    assert reasons["Remote"] == "A tick box or choice: choose it yourself."
    assert reasons["Cover Letter"].startswith("A cover letter upload")
    assert reasons["Transcript"] == "A file upload: attach it yourself."


def test_ambiguous_unlabelled_hidden_locked_and_unaddressable_fields():
    fields = [text("a", "Email"), text("b", "Email"), text("c", ""), text("d", "Phone", visible=False),
              text("e", "LinkedIn Profile", editable=False), FormField(key="index:7", tag="input", label="GitHub"),
              text("f", "Website / Portfolio", labels=2)]
    items = ap.plan(fields, data(portfolio="example.com"))
    assert [i.key for i in items] == ["id:a", "id:b", "id:c", "id:e", "index:7", "id:f"]  # hidden: not listed
    assert all(i.action == "leave" for i in items)
    reasons = [i.reason for i in items]
    assert reasons[0] == reasons[1] and "More than one label or field" in reasons[0]
    assert "no label" in reasons[2] and "Locked" in reasons[3] and "find this field again" in reasons[4]
    assert "More than one label" in reasons[5]


def test_skipped_unresolved_and_conflicting_answers_are_left():
    fields = [text("w", "Website"), text("c", "Current company"), text("e", "Email"), text("x", "Referral")]
    fill_data = data(answer("Website", "", "factual", skipped=True), answer("Current company", "", resolved=False),
                     answer("Email", "other@example.com", "factual"),
                     answer("Referral", "A", "open"), answer("Referral", "B", "open"))
    result = actions(fields, fill_data)
    assert result["Website"][1] == "You skipped this optional question in your package."
    assert result["Current company"][1] == "Your package has no approved answer for it."
    assert result["Email"][1] == "Your profile and your package's answer differ for this field."
    assert "more than one question" in result["Referral"][1]


def test_values_must_fit_the_field():
    fields = [text("a", "Why us?"), FormField(key="id:b", tag="textarea", label="Tell us more", max_length=10),
              text("c", "Summary", max_length=5)]
    fill_data = data(answer("Why us?", "One.\nTwo."), answer("Tell us more", "Twelve chars"),
                     answer("Summary", "👋👋👋"))
    result = actions(fields, fill_data)
    assert result["Why us?"][1] == "Your answer has line breaks, which this one-line field can't hold."
    assert result["Tell us more"][1] == "Your answer has 12 characters; this field takes 10."
    assert result["Summary"][1] == "Your answer has 6 characters; this field takes 5."  # emoji count as 2, as forms do


def test_existing_values_are_kept_unless_replaced():
    fields = [text("email", "Email", value="jordan@example.com "), text("phone", "Phone", value="555"),
              FormField(key="id:resume", tag="input", type="file", label="Resume/CV", value="old.pdf")]
    items = {i.label: i for i in ap.plan(fields, data())}
    assert items["Email"].action == "same"
    assert items["Phone"].action == "occupied" and items["Phone"].existing == "555"
    assert items["Resume/CV"].action == "occupied" and items["Resume/CV"].file


def test_the_resume_upload_needs_the_attached_pdf_and_one_resume_field():
    upload = FormField(key="id:resume", tag="input", type="file", label="Resume/CV", visible=False)
    item = ap.plan([upload], data())[0]
    assert (item.action, item.value, item.file) == ("attach", "Resume.pdf", True)
    assert ap.plan([upload], data(resume=None))[0].action == "leave"
    second = FormField(key="id:cv", tag="input", type="file", label="CV")
    assert {i.action for i in ap.plan([upload, second], data())} == {"leave"}


def test_plan_token_ignores_typed_values_but_not_the_form_package_or_pdf():
    fields = [text("email", "Email"), text("q", "Why us?")]
    fill_data = data(answer("Why us?", "Because."))
    shape = ap.structure(fields)
    token = ap.plan_token(shape, ap.plan(fields, fill_data), fill_data)
    typed = [text("email", "Email", value="me@example.com"), text("q", "Why us?")]
    assert ap.structure(typed) == shape
    assert ap.plan_token(shape, ap.plan(typed, fill_data), fill_data) == token
    changed = data(answer("Why us?", "Because, really."))
    assert ap.plan_token(shape, ap.plan(fields, changed), changed) != token
    other_pdf = FillData(**{**fill_data.__dict__, "resume_sha256": "t"})
    assert ap.plan_token(shape, ap.plan(fields, other_pdf), other_pdf) != token
    assert ap.structure([*fields, text("new", "New question")]) != shape


def test_page_descriptions_are_bounded_and_typed():
    raw = {"key": "id:x", "tag": "INPUT", "type": "TEXT", "label": "L" * 5000, "labels": "2", "value": 7,
           "maxLength": -1, "visible": "yes", "editable": True}
    field = FormField.from_page(raw)
    assert field.tag == "input" and field.type == "text" and len(field.label) == 2000
    assert field.labels == 0 and field.value == "" and field.max_length is None and field.visible is False
    assert FormField.from_page({"tag": "input"}) is None and FormField.from_page("x") is None


def test_missing_or_empty_contact_details_stay_empty(monkeypatch):
    from types import SimpleNamespace

    from app.services import autofill, package, resume_attachment

    resolved = {"resume": {"contact": {"name": "Jordan Example", "email": None, "links": {"linkedin": None}}},
                "questions": []}
    monkeypatch.setattr(package, "resolved_package", lambda *args: resolved)
    monkeypatch.setattr(resume_attachment, "current", lambda *args, **kwargs: None)
    data = autofill.fill_data(SimpleNamespace(), SimpleNamespace(approval=None), None)
    assert data.contact["name"] == "Jordan Example"
    assert all(data.contact[key] == "" for key in ("email", "phone", "linkedin", "github", "portfolio"))


def test_the_review_state_is_worked_out_once(monkeypatch):
    from types import SimpleNamespace

    from app.schemas.tracking import ReviewState
    from app.services import autofill, package, resume_attachment

    calls = []
    monkeypatch.setattr(package, "review_state", lambda *args: calls.append(1) or ReviewState.APPROVED)
    attachment = SimpleNamespace(package_hash="h")
    application = SimpleNamespace(resume_attachment=attachment, approval=SimpleNamespace(content_hash="h"))
    job = SimpleNamespace(url="https://job-boards.greenhouse.io/examplerobotics/jobs/4012345")

    ready = autofill.readiness(job, application, None, state=ReviewState.APPROVED)  # the page already has it
    assert calls == [] and ready.approved and ready.attachment is attachment
    assert resume_attachment.current(job, application, None, state=ReviewState.APPROVED) is attachment and calls == []
    autofill.readiness(job, application, None)
    assert calls == [1]  # worked out once, for both the approval and the attachment
