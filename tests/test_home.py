"""The home page (jobs tracker) greeting."""

from __future__ import annotations

from tests.test_routes import profile_form, review_and_save


def test_home_greets_by_first_name_or_invites_a_profile(client, sample_profile):
    page = client.get("/jobs").text
    assert "Hello there." in page and 'href="/profile/edit">Create your profile</a>' in page
    review_and_save(client, profile_form(sample_profile))
    page = client.get("/jobs").text
    assert '<p class="greeting">Hello, Jordan.</p>' in page
    assert "Create your profile" not in page


def test_first_name_filter():
    from app.templating import first_name

    assert first_name("Jordan Example") == "Jordan"
    assert first_name("  ") == "there" and first_name(None) == "there"


def test_favicon_is_linked_and_served(client):
    page = client.get("/jobs").text
    assert 'rel="icon" href="/static/favicon.svg?v=' in page
    redirect = client.get("/favicon.ico", follow_redirects=False)
    assert redirect.status_code == 301 and redirect.headers["location"] == "/static/favicon.svg"
    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
