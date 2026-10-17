"""Which hosted application forms autofill supports, and where each one's form is."""

from __future__ import annotations

import pytest

from app.services import postings
from app.services.postings import ASHBY, GREENHOUSE, LEVER, FormRef

UUID = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


@pytest.mark.parametrize("url, ref", [
    ("https://job-boards.greenhouse.io/examplerobotics/jobs/4012345", FormRef(GREENHOUSE, "examplerobotics", "4012345")),
    (f"https://jobs.lever.co/ExampleRobotics/{UUID}", FormRef(LEVER, "examplerobotics", UUID)),
    (f"https://jobs.lever.co/examplerobotics/{UUID}/apply", FormRef(LEVER, "examplerobotics", UUID)),
    (f"https://jobs.eu.lever.co/examplerobotics/{UUID.upper()}/", FormRef(LEVER, "examplerobotics", UUID)),
    (f"https://jobs.ashbyhq.com/examplerobotics/{UUID}", FormRef(ASHBY, "examplerobotics", UUID)),
    (f"https://jobs.ashbyhq.com/examplerobotics/{UUID}/application?utm=1", FormRef(ASHBY, "examplerobotics", UUID)),
])
def test_supported_links_name_their_posting(url, ref):
    assert postings.form_ref(url) == ref


@pytest.mark.parametrize("url", [
    f"http://jobs.lever.co/examplerobotics/{UUID}",  # not https
    f"https://jobs.lever.co.evil.example/examplerobotics/{UUID}",
    f"https://user@jobs.lever.co/examplerobotics/{UUID}",
    f"https://jobs.lever.co:8443/examplerobotics/{UUID}",
    "https://jobs.lever.co/examplerobotics",  # the board, not a posting
    "https://jobs.lever.co/examplerobotics/not-a-uuid/apply",
    f"https://jobs.ashbyhq.com/examplerobotics/{UUID}/other",
    f"https://careers.example.com/examplerobotics/{UUID}",
])
def test_other_links_are_not_supported(url):
    assert postings.form_ref(url) is None


def test_the_form_is_opened_on_its_own_page():
    greenhouse = "https://job-boards.greenhouse.io/examplerobotics/jobs/4012345"
    assert postings.form_url(greenhouse) == greenhouse
    assert postings.form_url(f"https://jobs.lever.co/examplerobotics/{UUID}") == \
        f"https://jobs.lever.co/examplerobotics/{UUID}/apply"
    assert postings.form_url(f"https://jobs.lever.co/examplerobotics/{UUID}/apply/") == \
        f"https://jobs.lever.co/examplerobotics/{UUID}/apply"
    assert postings.form_url(f"https://jobs.ashbyhq.com/examplerobotics/{UUID}?src=board") == \
        f"https://jobs.ashbyhq.com/examplerobotics/{UUID}/application?src=board"
    assert postings.form_ref(postings.form_url(f"https://jobs.ashbyhq.com/examplerobotics/{UUID}")).platform == ASHBY
