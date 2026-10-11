"""Importing a job from Greenhouse on the Add a job page, over HTTP. The network is never used."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import inspect, text

from app.db import init_db, make_engine, make_session_factory
from app.models import Job
from app.schemas.package import Question
from app.services import jobs as job_service
from tests.conftest import DEMO_JOB
from tests.greenhouse_fake import API_URL, POSTING_URL, FakeGreenhouse, posting
from tests.test_profile_sections import form_fields
from tests.test_routes import hidden, profile_form, review_and_save


@pytest.fixture
def greenhouse(client):
    """A stand-in for Greenhouse; queue its answers with ``.queue.append``."""
    fake = FakeGreenhouse()
    client.app.state.greenhouse_transport = fake.transport
    return fake


def jobs(client) -> list[Job]:
    with client.app.state.session_factory() as session:
        return session.query(Job).all()


def job_fields(page: str) -> dict[str, str]:
    """What the Add a job form posts, as a browser would send it."""
    form = page[page.index('<form method="post" action="/jobs" '):]
    form = form[:form.index("</form>")]
    fields = form_fields('action="/profile/review"' + form + "</form>")
    if 'name="add_questions" value="1" checked' not in form:
        fields.pop("add_questions", None)
    return fields


def test_the_add_job_page_offers_the_import_and_says_what_is_fetched(client):
    page = client.get("/jobs/new").text
    assert '<form method="post" action="/jobs/import"' in page
    assert "fetches the posting and its application questions from Greenhouse" in page
    assert "Saved as a reference only: saving never fetches it." in page
    assert "Nothing is fetched." not in page
    assert 'action="/jobs/import"' not in client.get(f"/jobs/{_saved_job(client)}/edit").text  # adding only


def _saved_job(client) -> int:
    response = client.post("/jobs", data={"title": "Analyst", "company": "Example Corp", "description": "Text"},
                           follow_redirects=False)
    return int(response.headers["location"].split("/")[2].split("?")[0])


def test_importing_fills_the_form_and_lists_the_questions_without_saving(client, greenhouse):
    greenhouse.queue.append(posting())
    response = client.post("/jobs/import", data={"url": POSTING_URL})
    assert response.status_code == 200 and jobs(client) == []  # nothing saved yet
    assert [str(r.url) for r in greenhouse.requests] == [API_URL]
    page = response.text
    assert "Imported from Greenhouse. Check everything before saving." in page
    fields = job_fields(page)
    assert (fields["title"], fields["company"], fields["location"], fields["url"]) == (
        "Software Engineering Intern", "Example Robotics", "Austin, TX", POSTING_URL)
    assert fields["description"].startswith("About the role\n\nExample Robotics builds fictional warehouse robots.")
    assert "Its application form asks 14 questions." in page
    assert "<strong>Why do you want to work at Example Robotics?</strong> <span class=\"meta\">· Required · Long text · Open-ended" in page
    assert "<strong>How would you describe yourself?</strong> <span class=\"meta\">· Optional · Choose any · Sensitive" in page
    assert "Preferred interview date</strong> <span class=\"meta\">· Optional · A field ResuApply doesn&#39;t know (date_picker) · Unrecognized" in page
    assert "Resume/CV: Attach the resume from your approved package on the employer&#39;s form." in page
    assert "&lt;script&gt;" not in page and "alert(1)" not in page


def test_saving_adds_the_reviewed_questions_with_their_flags_and_categories(client, greenhouse):
    greenhouse.queue.append(posting())
    fields = job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text)
    saved = client.post("/jobs", data=fields, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"].endswith("?step=questions&msg=job_imported")
    assert len(greenhouse.requests) == 1  # saving uses what was reviewed: no second fetch
    (job,) = jobs(client)
    assert job.title == "Software Engineering Intern" and job.url == POSTING_URL
    questions = {q.text: q for q in job.questions}
    assert len(questions) == 14
    assert (questions["Email"].required, questions["Email"].category, questions["Email"].factual_key) == (
        True, "factual", "email")
    assert (questions["Phone"].required, questions["Phone"].category) == (False, "factual")
    sponsorship = questions["Will you now or in the future require visa sponsorship to work in the United States?"]
    assert (sponsorship.category, sponsorship.factual_key) == ("sensitive_factual", "sponsorship")
    assert questions["How would you describe yourself?"].category == "sensitive"
    assert questions["Please identify your self-reported status"].category == "sensitive"
    assert questions["Gender"].category == "sensitive"
    assert questions["Preferred interview date"].category == "unknown"
    imported = job.posting_import
    assert (imported.board, imported.job_id) == ("examplerobotics", "4012345")
    field = imported.fields[questions["Which of these tools have you used?"].id]
    assert (field.field_type, field.options) == ("multi_select", ["Git", "Docker"])
    assert [item.label for item in imported.left_out] == ["Resume/CV", "Portfolio sample"]
    page = client.get(saved.headers["location"]).text
    assert "Job saved with the questions from its Greenhouse posting." in page


def test_unticking_the_questions_saves_only_the_job(client, greenhouse):
    greenhouse.queue.append(posting())
    fields = job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text)
    fields.pop("add_questions")
    assert client.post("/jobs", data=fields, follow_redirects=False).status_code == 303
    (job,) = jobs(client)
    assert job.questions == [] and job.posting_import is None


def test_the_questions_belong_to_the_imported_link(client, greenhouse):
    greenhouse.queue.append(posting())
    fields = job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text)
    refused = client.post("/jobs", data=fields | {"url": "https://boards.greenhouse.io/other/jobs/1"})
    assert refused.status_code == 422 and "This isn&#39;t the link of the posting you imported" in refused.text
    assert jobs(client) == []
    assert "Imported from Greenhouse" in refused.text  # the review stays on screen


def test_a_form_problem_keeps_the_import_under_review(client, greenhouse):
    greenhouse.queue.append(posting())
    fields = job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text)
    refused = client.post("/jobs", data=fields | {"title": "x" * 201})
    assert refused.status_code == 422 and jobs(client) == []
    fixed = job_fields(refused.text) | {"title": "Software Engineering Intern"}
    assert client.post("/jobs", data=fixed, follow_redirects=False).status_code == 303
    assert len(jobs(client)[0].questions) == 14


@pytest.mark.parametrize("damaged", ["not json", "{}", json.dumps({"board": "x/y", "job_id": "1",
                                                                     "fetched_at": "2026-10-01T00:00:00Z"})])
def test_a_damaged_carried_import_adds_no_questions(client, damaged):
    response = client.post("/jobs", data={"title": "Analyst", "company": "Example Corp", "description": "Text",
                                          "url": POSTING_URL, "imported": damaged, "add_questions": "1"},
                           follow_redirects=False)
    assert response.status_code == 303
    (job,) = jobs(client)
    assert job.questions == [] and job.posting_import is None


@pytest.mark.parametrize("url", [
    "https://www.example.com/careers/analyst",
    "http://boards.greenhouse.io/examplerobotics/jobs/4012345",
    "https://boards.greenhouse.io.example.com/examplerobotics/jobs/4012345",
    "",
])
def test_other_links_are_not_fetched(client, greenhouse, url):
    response = client.post("/jobs/import", data={"url": url})
    assert response.status_code == 422 and "Only a Greenhouse posting link can be imported" in response.text
    assert greenhouse.requests == [] and jobs(client) == []


def test_other_links_are_still_saved_as_references(client):
    response = client.post("/jobs", data={"title": "Analyst", "company": "Example Corp", "description": "Text",
                                          "url": "https://www.example.com/careers/analyst"}, follow_redirects=False)
    assert response.status_code == 303 and jobs(client)[0].url == "https://www.example.com/careers/analyst"


@pytest.mark.parametrize("answer, message", [
    (httpx.ReadTimeout("slow"), "Greenhouse took longer than 10 seconds"),
    (httpx.Response(404, json={}), "Greenhouse has no such job"),
    (httpx.Response(200, text="not json"), "wasn&#39;t a job posting"),
    (httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)), "over 2 MB"),
])
def test_a_failed_import_says_why_and_changes_nothing(client, greenhouse, answer, message):
    before = _saved_job(client)
    greenhouse.queue.append(answer)
    response = client.post("/jobs/import", data={"url": POSTING_URL})
    assert response.status_code == 502 and message in response.text
    assert f'value="{POSTING_URL}"' in response.text  # the link stays, to try again
    assert [job.id for job in jobs(client)] == [before] and jobs(client)[0].questions == []


def test_an_existing_database_gains_the_import_column_and_questions_keep_their_shape(client, greenhouse, tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        job_service.create(session, job_service.clean_input(**DEMO_JOB))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN posting_import"))
    init_db(engine)
    assert "posting_import" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT posting_import FROM jobs")).scalar_one() is None
    engine.dispose()

    # An imported job's questions are stored exactly as hand-added ones: nothing new in their JSON.
    greenhouse.queue.append(posting())
    client.post("/jobs", data=job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text))
    with client.app.state.engine.connect() as conn:
        stored = json.loads(conn.execute(text("SELECT questions FROM jobs")).scalar_one())
    assert stored and all(set(q) == set(Question.model_fields) for q in stored)


def _import_and_save(client, greenhouse) -> str:
    greenhouse.queue.append(posting())
    saved = client.post("/jobs", data=job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text),
                        follow_redirects=False)
    return saved.headers["location"]


def test_the_questions_step_shows_each_imported_field_and_what_was_left_out(client, greenhouse, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    page = client.get(_import_and_save(client, greenhouse)).text
    assert "Questions imported from the job's Greenhouse posting on" in page
    assert "Left for you on the employer's form (2)" in page
    assert "<li>Portfolio sample: A file upload: attach it on the employer&#39;s form.</li>" in page
    assert "Choose any on the employer's form: Git · Docker. Answer with the choices as written there." in page
    assert "Choose one on the employer's form: Yes · No. Answer with the choice as written there." in page
    assert "a kind of field ResuApply doesn't know (date_picker)" in page
    assert page.count('<span class="tag">Short text</span>') == 6


def test_a_job_entered_by_hand_shows_no_import_details(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    page = client.get(f"/jobs/{_saved_job(client)}?step=questions").text
    assert "Greenhouse" not in page and "Left for you" not in page


def test_without_a_profile_the_imported_questions_are_counted(client, greenhouse):
    page = client.get(_import_and_save(client, greenhouse)).text
    assert "before answering the 14 questions saved with this job" in page


def test_a_posting_with_hundreds_of_upload_fields_is_imported_with_them_summed_up(client, greenhouse):
    data = posting()
    data["questions"] += [{"label": f"Attachment {n}", "required": False, "fields": [{"name": f"file_{n}",
                                                                                     "type": "input_file"}]}
                          for n in range(350)]
    greenhouse.queue.append(data)
    response = client.post("/jobs/import", data={"url": POSTING_URL})
    assert response.status_code == 200
    assert "Attachment 0: A file upload" in response.text and "302 more fields" in response.text


def test_an_answer_that_is_not_one_of_the_forms_choices_is_shown_and_blocks_approval(client, greenhouse, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    location = _import_and_save(client, greenhouse)
    [job] = jobs(client)
    qid = next(q.id for q in job.questions if q.text == "Which of these tools have you used?")
    client.post(f"/jobs/{job.id}/questions/{qid}/category", data={"category": "open"}, follow_redirects=False)
    client.post(f"/jobs/{job.id}/questions/{qid}/answer", data={"text": "Lots of tools, daily."}, follow_redirects=False)
    page = client.get(location).text
    assert "This answer isn&#39;t made of the choices on the employer&#39;s form (Git · Docker)." in page
    assert "Not one of the form&#39;s choices" in page
    client.post(f"/jobs/{job.id}/questions/{qid}/answer", data={"text": "Git\nDocker"}, follow_redirects=False)
    assert "made of the choices" not in client.get(location).text


def test_a_failure_part_way_through_the_questions_saves_neither_the_job_nor_any_question(client, greenhouse,
                                                                                          monkeypatch):
    from app.services import answers

    greenhouse.queue.append(posting())
    fields = job_fields(client.post("/jobs/import", data={"url": POSTING_URL}).text)
    made = []
    original = answers.new_question

    def failing(*args, **kwargs):
        if len(made) == 3:
            raise RuntimeError("database locked")
        made.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(answers, "new_question", failing)
    with pytest.raises(RuntimeError):
        client.post("/jobs", data=fields, follow_redirects=False)
    assert jobs(client) == []  # sending the form again can't make a duplicate job


def test_a_posting_without_questions_offers_none_and_saves_a_plain_job(client, greenhouse):
    uploads_only = posting()
    uploads_only["questions"] = [q for q in uploads_only["questions"] if q["label"] == "Resume/CV"]
    uploads_only["location_questions"], uploads_only["compliance"] = [], []
    uploads_only.pop("demographic_questions", None)
    greenhouse.queue.append(uploads_only)
    page = client.post("/jobs/import", data={"url": POSTING_URL}).text
    assert "Its application form has no questions to add." in page
    assert "Add these 0 questions" not in page and 'name="add_questions"' not in page
    saved = client.post("/jobs", data=job_fields(page), follow_redirects=False)
    assert saved.headers["location"].endswith("?msg=job_created")  # not "saved with the questions"
    (job,) = jobs(client)
    assert job.questions == [] and job.posting_import is None
