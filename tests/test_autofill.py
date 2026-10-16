"""Greenhouse autofill end to end, in a real (hidden) Chromium against local Greenhouse-like forms.

No test touches the network: ``FixtureSite`` serves every page and refuses everything else. Each
test checks that nothing was submitted: no submit event, no click on Submit, no form post.
Fictional data only. Skipped when Playwright or its Chromium isn't installed.
"""

from __future__ import annotations

import ast
import copy
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("playwright.sync_api", reason="Playwright is optional: pip install -r requirements-autofill.txt")

from app.main import create_app  # noqa: E402
from app.services import answers, autofill_browser, jobs, package, profile, resume  # noqa: E402
from app.services import questions as q_rules  # noqa: E402
from app.services.autofill_browser import LaunchOptions  # noqa: E402
from app.services.profile import get_candidate  # noqa: E402
from tests.autofill_site import EMPLOYER_URL, POSTING_URL, FixtureSite, chromium  # noqa: E402
from tests.conftest import BASE_URL, NO_NETWORK  # noqa: E402
from tests.resume_files import pdf_bytes  # noqa: E402
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE  # noqa: E402

WHY = "Why do you want to work at Example Robotics?"
WHY_ANSWER = "I build Python services.\nRobotics is where I want to use them."


@pytest.fixture(scope="module")
def browser_path() -> str | None:
    try:
        return chromium()
    except LookupError:
        pytest.skip("Playwright's Chromium isn't installed: python -m playwright install chromium")


@dataclass
class World:
    client: TestClient
    app: object
    site: FixtureSite
    job_id: int

    def session(self):
        return self.app.state.session_factory()

    def page(self, script: str):
        """Run ``script`` in the open form, on the browser's own thread (as the user or the page would)."""
        browser = self.app.state.autofill.browser
        return browser._call(lambda: browser._page.evaluate(script))

    def workspace(self) -> str:
        return self.client.get(f"/jobs/{self.job_id}?step=track").text

    def token(self) -> str:
        """The package token the workspace shows now (its forms carry it while the package is approved)."""
        with self.session() as session:
            job = jobs.get(session, self.job_id)
            return package.package_token(job, job.application, get_candidate(session))

    def attach(self, data: bytes | None = None, name: str = "Jordan Example resume.pdf"):
        data = data if data is not None else pdf_bytes(["Jordan Example", "jordan@example.com", "Experience"])
        return self.client.post(f"/jobs/{self.job_id}/autofill/resume", data={"package_token": self.token()},
                                files={"file": (name, data, "application/pdf")}, follow_redirects=False)

    def open(self):
        return self.client.post(f"/jobs/{self.job_id}/autofill/open", data={"package_token": self.token()},
                                follow_redirects=False)

    def plan_token(self) -> str:
        return re.search(r'name="plan_token" value="([^"]+)"', self.workspace()).group(1)

    def fill(self, replace: list[str] | None = None):
        return self.client.post(f"/jobs/{self.job_id}/autofill/fill",
                                data={"plan_token": self.plan_token(), "replace": replace or []},
                                follow_redirects=False)

    @property
    def run(self):
        return self.app.state.autofill.run

    def assert_not_submitted(self):
        assert self.site.posts == []
        if self.run is not None and not self.app.state.autofill.browser._page.is_closed():
            assert self.page("() => window.fixture ? window.fixture.submits + window.fixture.clicks : 0") == 0


def _prepare(session, url: str) -> int:
    """An approved package for a job at ``url``: contact questions, a custom question and sensitive ones."""
    candidate = profile.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
    job = jobs.create(session, jobs.clean_input(**{**DEMO_JOB, "url": url, "company": "Example Robotics"}))
    application = job.application
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(record))
    first = answers.add_question(session, job, "First Name")
    last = answers.add_question(session, job, "Last Name")
    answers.add_question(session, job, "Email")
    answers.add_question(session, job, "Phone", required=False)
    answers.add_question(session, job, "LinkedIn Profile", required=False)
    website = answers.add_question(session, job, "Website", required=False)
    why = answers.add_question(session, job, WHY)
    company = answers.add_question(session, job, "Current company", required=False, category=q_rules.OPEN)
    auth = answers.add_question(session, job, "Are you legally authorized to work in the United States?")
    gender = answers.add_question(session, job, "Gender", required=False)
    answers.set_answer(session, job, application, first.id, "Jordan")
    answers.set_answer(session, job, application, last.id, "Example")
    answers.skip_answer(session, job, application, website.id)
    answers.set_answer(session, job, application, why.id, WHY_ANSWER)
    answers.set_answer(session, job, application, company.id, "Demo Corp")
    question = next(q for q in job.questions if q.id == auth.id)
    value = q_rules.factual_value(question.factual_key or "", question.text, candidate.profile)
    answers.confirm_answer(session, job, application, auth.id, candidate,
                           answers.confirmation_token(job, question, value, candidate))
    answers.skip_answer(session, job, application, gender.id)
    package.approve(session, job, candidate, package.package_token(job, application, candidate))
    return job.id


@pytest.fixture
def world(settings, browser_path, request) -> Iterator[World]:
    marker = request.node.get_closest_marker("pages")
    pages = marker.args[0] if marker else {POSTING_URL: "standard.html"}
    url = marker.kwargs.get("url", POSTING_URL) if marker else POSTING_URL
    site = FixtureSite(pages)
    app = create_app(settings, greenhouse_transport=NO_NETWORK, autofill_options=LaunchOptions(
        headless=True, executable_path=browser_path, prepare_context=site.install))
    with TestClient(app, base_url=BASE_URL) as client:
        with app.state.session_factory() as session:
            job_id = _prepare(session, url)
        yield World(client, app, site, job_id)


def _report(world: World) -> dict[str, tuple[str, str]]:
    return {row.label: (row.outcome, row.detail) for row in world.run.report}


def _plan(world: World) -> dict[str, str]:
    return {item.label: item.action for item in world.run.plan}


# ---------------------------------------------------------------- the standard form

def test_inspects_then_fills_verified_fields_reads_them_back_and_never_submits(world):
    assert world.attach().status_code == 303
    response = world.open()
    assert response.status_code == 303, response.text
    plan = _plan(world)
    assert plan["First Name"] == plan["Last Name"] == plan["Email"] == plan["Phone"] == "fill"
    assert plan["LinkedIn Profile"] == plan[WHY] == plan["Current company"] == "fill"
    assert plan["Resume/CV"] == "attach"
    for left in ("Website", "How did you hear about us?", "Are you legally authorized to work in the United States?",
                 "Location (City)", "Earliest start date", "Gender", "Anything else you'd like to share",
                 "I agree to the privacy policy", "Cover Letter"):
        assert plan[left] == "leave", left
    assert "Leave this empty" not in plan  # a hidden field is never touched
    # Inspecting changes nothing on the page.
    assert world.page("() => document.getElementById('email').value") == ""
    page = world.workspace()
    assert "What ResuApply will fill" in page and "Fill the verified fields" in page
    assert "A sensitive question: answer it yourself." in page and "A consent or declaration" in page

    response = world.fill()
    assert response.status_code == 303, response.text
    report = _report(world)
    assert report["First Name"] == ("filled", "Jordan")
    assert report["Email"] == ("filled", "jordan@example.com")
    assert report["Phone"] == ("filled", "+1 555 0100")
    assert report["LinkedIn Profile"] == ("filled", "linkedin.com/in/jordan-example")
    assert report[WHY] == ("filled", WHY_ANSWER)
    assert report["Current company"] == ("filled", "Demo Corp")
    assert report["Resume/CV"][0] == "attached"
    assert report["Website"] == ("left", "You skipped this optional question in your package.")
    assert report["Are you legally authorized to work in the United States?"][0] == "left"
    assert world.run.phase == "filled" and world.run.notice == ""
    # The values are really in the form, and the PDF is the one attached, under its name.
    assert world.page("() => document.getElementById('question_103').value") == WHY_ANSWER
    assert world.page("() => document.getElementById('resume-name').textContent") == "Jordan Example resume.pdf"
    assert world.page("() => document.getElementById('question_106').value") == ""
    assert world.page("() => document.getElementById('gdpr_consent').checked") is False
    assert world.page("() => document.getElementById('hp_field').value") == ""
    world.assert_not_submitted()
    page = world.workspace()
    assert "What was filled" in page and "click Submit on the employer's form yourself" in page
    # Filling never records anything: the user records Applied after submitting.
    with world.session() as session:
        application = jobs.get(session, world.job_id).application
        assert application.tracking_status == "saved" and application.submitted_snapshots == []


def test_keeps_existing_values_unless_the_user_ticks_replace(world):
    world.attach()
    world.open()
    world.page("() => { document.getElementById('question_104').value = 'Typed by me'; }")
    world.fill()
    assert _report(world)["Current company"][0] == "kept"
    assert world.page("() => document.getElementById('question_104').value") == "Typed by me"
    world.assert_not_submitted()


@pytest.mark.pages({POSTING_URL: "prefilled.html"})
def test_prefilled_form_replaces_only_ticked_fields(world):
    world.attach()
    world.open()
    plan = _plan(world)
    assert plan == {"First Name": "occupied", "Last Name": "fill", "Email": "same", "Phone": "occupied",
                    "Resume/CV": "attach"}
    assert "Replace “Jordy” with “Jordan”" in world.workspace()
    world.fill(replace=["id:first_name"])
    report = _report(world)
    assert report["First Name"] == ("filled", "Jordan")
    assert report["Phone"] == ("kept", "Kept: 555 0199")
    assert report["Email"][0] == "already"
    assert world.page("() => document.getElementById('phone').value") == "555 0199"
    world.assert_not_submitted()


def test_replace_is_refused_when_the_value_changed_after_inspection(world):
    world.attach()
    world.open()
    world.page("() => { document.getElementById('first_name').value = 'Jo'; }")
    world.client.post(f"/jobs/{world.job_id}/autofill/inspect")
    world.page("() => { document.getElementById('first_name').value = 'J.'; }")
    world.fill(replace=["id:first_name"])
    outcome, detail = _report(world)["First Name"]
    assert outcome == "kept" and "changed in the browser" in detail
    assert world.page("() => document.getElementById('first_name').value") == "J."


@pytest.mark.pages({POSTING_URL: "quirks.html"})
def test_readback_shows_mismatches_failed_uploads_and_stops_page_submissions(world):
    world.attach()
    world.open()
    world.fill()
    report = _report(world)
    assert report["Phone"] == ("mismatch", "The form shows “15550100” instead of “+1 555 0100”.")
    outcome, detail = report["Resume/CV"]
    assert outcome == "mismatch" and "no file" in detail
    assert report["Email"] == ("filled", "jordan@example.com")
    # The page tried to submit itself while the text box was filled; the guard stopped every attempt.
    assert world.page("() => window.fixture.attempts") > 0
    assert world.run.phase == "handed_over" and "tried to submit" in world.run.notice
    world.assert_not_submitted()
    # Outside a fill the guard is off again: the page (or the user) can submit as usual.
    assert world.page("() => window.__resuapplyGuard.active") is False


# ---------------------------------------------------------------- handing control back

def test_a_changed_form_structure_stops_before_anything_is_filled(world):
    world.attach()
    world.open()
    world.page("""() => { const input = document.createElement('input'); input.id = 'question_999';
        const label = document.createElement('label'); label.htmlFor = 'question_999';
        label.textContent = 'New question';
        document.getElementById('application-form').append(label, input); }""")
    world.fill()
    assert world.run.phase == "handed_over" and "fields changed" in world.run.notice
    assert world.run.report is None
    assert world.page("() => document.getElementById('email').value") == ""
    assert "The browser is yours." in world.workspace()
    response = world.client.post(f"/jobs/{world.job_id}/autofill/fill", data={"plan_token": world.run.token})
    assert response.status_code == 409 and "Check the form again" in response.text
    # After the user checks again, the new plan covers the new field and filling works.
    world.client.post(f"/jobs/{world.job_id}/autofill/inspect")
    assert _plan(world)["New question"] == "leave"
    world.fill()
    assert _report(world)["Email"][0] == "filled"
    world.assert_not_submitted()


@pytest.mark.pages({POSTING_URL: "captcha.html"})
def test_a_captcha_returns_control(world):
    world.attach()
    assert world.open().status_code == 303
    assert world.run.phase == "handed_over" and "CAPTCHA" in world.run.notice and world.run.plan == []
    page = world.workspace()
    assert "Fill the verified fields" not in page and "Complete it in the browser yourself" in page
    assert world.page("() => document.getElementById('email').value") == ""


@pytest.mark.pages({POSTING_URL: "login.html"})
def test_a_sign_in_page_returns_control(world):
    world.attach()
    world.open()
    assert world.run.phase == "handed_over" and "sign in" in world.run.notice


@pytest.mark.pages({POSTING_URL: "redirect.html", EMPLOYER_URL: "employer.html"})
def test_an_unexpected_redirect_returns_control(world):
    world.attach()
    world.open()
    assert world.run.phase == "handed_over"
    assert "careers.example.com" in world.run.notice and "unexpected redirect" in world.run.notice
    assert world.run.plan == []
    world.assert_not_submitted()


@pytest.mark.pages({POSTING_URL: (404, "employer.html")})
def test_a_closed_posting_returns_control(world):
    world.attach()
    world.open()
    assert "posting may have closed" in world.run.notice


def test_a_closed_window_is_noticed(world):
    world.attach()
    world.open()
    browser = world.app.state.autofill.browser
    browser._call(lambda: browser._page.close())
    response = world.client.post(f"/jobs/{world.job_id}/autofill/inspect")
    assert response.status_code == 409 and "browser window was closed" in response.text
    assert world.run is None


# ---------------------------------------------------------------- what autofill requires

@pytest.mark.pages({POSTING_URL: "standard.html"}, url="https://example.com/careers/robotics-intern")
def test_only_greenhouse_hosted_forms(world):
    page = world.workspace()
    assert "Autofill works only with a standard Greenhouse-hosted form" in page
    assert "Open the form in a browser" not in page
    response = world.client.post(f"/jobs/{world.job_id}/autofill/open", data={"package_token": world.token()})
    assert response.status_code == 409 and "Greenhouse-hosted form" in response.text
    assert world.site.requests == []


def test_requires_an_approved_package_and_its_pdf(world):
    page = world.workspace()
    assert "Attach the resume PDF you saved from this package" in page
    assert "Open the form in a browser" not in page
    response = world.open()
    assert response.status_code == 409 and "Attach the resume PDF" in response.text
    world.attach()
    assert "Open the form in a browser" in world.workspace()
    with world.session() as session:  # an edited answer clears approval: the PDF no longer belongs to it
        job = jobs.get(session, world.job_id)
        qid = next(q.id for q in job.questions if q.text == "Current company")
        answers.set_answer(session, job, job.application, qid, "Another Corp")
    page = world.workspace()
    assert "Approve the current package in" in page and "Open the form in a browser" not in page
    response = world.client.post(f"/jobs/{world.job_id}/autofill/open", data={"package_token": world.token()})
    assert response.status_code == 409 and "Approve the current package first" in response.text
    assert world.site.requests == [] and world.run is None


def test_refuses_a_page_that_showed_another_package(world):
    world.attach()
    response = world.client.post(f"/jobs/{world.job_id}/autofill/open", data={"package_token": "old"})
    assert response.status_code == 409 and "package changed since you reviewed it" in response.text
    assert world.site.requests == []


def test_a_pdf_changed_on_disk_is_refused(world, settings):
    world.attach()
    path = settings.resolved_data_dir / "attachments" / f"job-{world.job_id}.pdf"
    path.write_bytes(path.read_bytes() + b"\n%changed")
    response = world.open()
    assert response.status_code == 409 and "missing from the data folder or was changed" in response.text


def test_close_ends_the_browser(world):
    world.attach()
    world.open()
    browser = world.app.state.autofill.browser
    assert browser._browser is not None
    response = world.client.post(f"/jobs/{world.job_id}/autofill/close", follow_redirects=False)
    assert response.status_code == 303 and world.run is None and browser._browser is None
    assert "Open the form in a browser" in world.workspace()


def test_app_shutdown_ends_the_browser(settings, browser_path):
    site = FixtureSite({POSTING_URL: "standard.html"})
    app = create_app(settings, greenhouse_transport=NO_NETWORK, autofill_options=LaunchOptions(
        headless=True, executable_path=browser_path, prepare_context=site.install))
    with TestClient(app, base_url=BASE_URL) as client:
        with app.state.session_factory() as session:
            job_id = _prepare(session, POSTING_URL)
        world = World(client, app, site, job_id)
        world.attach()
        world.open()
        browser = app.state.autofill.browser
        assert browser._browser is not None and world.run is not None
    assert browser._playwright is None and browser._browser is None and browser._executor is None
    assert app.state.autofill.run is None


# ---------------------------------------------------------------- never submitting

def test_the_browser_layer_has_no_way_to_click_or_press_keys():
    """Autofill fills and attaches only. These Playwright calls could submit a form; none is made."""
    tree = ast.parse(Path(autofill_browser.__file__).read_text())
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    forbidden = {"click", "dblclick", "press", "type", "tap", "check", "set_checked", "select_option",
                 "dispatch_event", "press_sequentially", "keyboard", "mouse", "touchscreen", "drag_to"}
    assert used & forbidden == set()
    # The only .submit is handing work to the browser thread.
    submits = [ast.unparse(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
               and node.attr == "submit"]
    assert submits == ["self._executor"]
    assert {"fill", "set_input_files", "input_value"} <= used


def test_a_browser_error_mid_step_is_reported_not_a_server_error(world):
    from playwright.sync_api import Error

    world.attach()
    world.open()
    token = world.plan_token()
    browser = world.app.state.autofill.browser

    def destroyed(*_):
        raise Error("Execution context was destroyed, most likely because of a navigation")

    browser._fill = destroyed
    response = world.client.post(f"/jobs/{world.job_id}/autofill/fill", data={"plan_token": token},
                                 follow_redirects=False)
    assert response.status_code == 409 and "The page changed or closed while ResuApply was working" in response.text
    world.assert_not_submitted()


@pytest.mark.pages({POSTING_URL: "history.html"})
def test_same_page_url_changes_while_filling_are_not_a_navigation(world):
    world.attach()
    world.open()
    assert world.fill().status_code == 303
    assert world.run.phase == "filled" and world.run.notice == ""
    report = _report(world)
    assert report["Email"] == ("filled", "jordan@example.com") and report[WHY] == ("filled", WHY_ANSWER)
    assert "?step=" in world.page("() => location.href")  # the page did rewrite its URL meanwhile
    world.assert_not_submitted()


def test_a_fill_waits_as_long_as_its_steps_can_take():
    seen = {}
    browser = autofill_browser.FormBrowser()

    def call(function, *args, timeout=autofill_browser.CALL_SECONDS):
        seen["timeout"] = timeout

    browser._call = call
    steps = [autofill_browser.FillStep(f"id:q{i}", value="x") for i in range(30)]
    browser.fill(steps, lambda url: True)
    # 30 slow fields (10 s to fill and 10 s to read back each) can't be cut off at the 120-second default.
    assert seen["timeout"] == autofill_browser.CALL_SECONDS + 30 * 2 * autofill_browser.ACTION_SECONDS
    assert seen["timeout"] > 30 * 2 * autofill_browser.ACTION_SECONDS


def test_an_oversized_resume_pdf_is_refused_before_it_is_read(world, monkeypatch):
    from starlette.requests import Request

    from app.services.resume_import import MAX_BYTES

    read = []
    original = Request.form

    def form(self, *args, **kwargs):
        read.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", form)
    response = world.attach(b"%PDF-" + b"x" * (MAX_BYTES + 200_000))
    assert response.status_code == 413 and "over 10 MB" in response.text
    assert read == []  # the shared check refused it from its Content-Length
