"""Editing one profile section at a time, through the same review and save steps."""

from __future__ import annotations

import re
from html import unescape

import pytest

from tests.test_routes import client_profile, hidden, profile_form, review_and_save

SECTIONS = ["contact", "education", "experience", "projects", "skills", "authorization", "preferences"]


def saved_profile(client, sample_profile):
    _, saved = review_and_save(client, profile_form(sample_profile))
    assert saved.status_code == 303


def form_fields(html: str) -> dict[str, str]:
    """Every field the form posts, as a browser would send it, hidden sections included (names are unique)."""
    form = html[html.index('action="/profile/review"'):html.index("</form>")]
    fields = []
    for match in re.finditer(r'<(input|textarea|select)[^>]*name="([^"]+)"[^>]*>', form):
        tag, name = match.group(1), match.group(2)
        if tag == "input":
            value = re.search(r'value="([^"]*)"', match.group(0))
            fields.append((name, unescape(value.group(1)) if value else ""))
        elif tag == "textarea":
            fields.append((name, unescape(form[match.end():form.index("</textarea>", match.end())])))
        else:
            body = form[match.end():form.index("</select>", match.end())]
            selected = re.search(r'<option value="([^"]*)"\s+selected', body)
            fields.append((name, selected.group(1) if selected else ""))
    assert len({n for n, _ in fields}) == len(fields)
    return dict(fields)


def cards(html: str) -> dict[str, bool]:
    """Each card's heading -> whether it is shown."""
    found = re.findall(r'<section class="card"( hidden)?>\s*<h2>([^<]+)</h2>', html)
    return {title: not hidden_attr for hidden_attr, title in found}


@pytest.mark.parametrize("section, shown", [
    ("contact", {"Contact", "Summary"}), ("experience", {"Experience"}),
    ("skills", {"Skills", "Certifications"}), ("preferences", {"Preferences and availability"}),
])
def test_a_section_shows_only_its_cards(client, sample_profile, section, shown):
    saved_profile(client, sample_profile)
    page = client.get(f"/profile/edit?section={section}").text
    assert {title for title, visible in cards(page).items() if visible} == shown
    assert len(cards(page)) == 9  # the other sections are still in the form
    assert f'<input type="hidden" name="section" value="{section}">' in page
    assert re.search(rf'href="/profile/edit\?section={section}" aria-current="page"', page)


def test_the_whole_profile_is_the_default_and_a_new_profile_is_always_whole(client, sample_profile):
    assert all(cards(client.get("/profile/edit?section=experience").text).values())  # no profile yet
    saved_profile(client, sample_profile)
    page = client.get("/profile/edit").text
    assert all(cards(page).values()) and 'name="section"' not in page


def test_an_unknown_section_is_a_bad_link(client, sample_profile):
    saved_profile(client, sample_profile)
    assert client.get("/profile/edit?section=hobbies").status_code == 400


def test_editing_one_section_changes_only_that_section(client, sample_profile):
    saved_profile(client, sample_profile)
    before = client_profile(client)
    fields = form_fields(client.get("/profile/edit?section=experience").text)
    name = next(n for n, v in fields.items() if n.endswith("-text") and v.startswith("Built a Flask"))
    fields[name] = "Built and documented a Flask REST API"
    review = client.post("/profile/review", data=fields)
    assert review.status_code == 200
    assert "Built and documented a Flask REST API" in review.text
    assert '<input type="hidden" name="section" value="experience">' in review.text  # back to editing keeps it
    saved = client.post("/profile/save", data={"payload": hidden(review.text, "payload"),
                                               "base_revision": hidden(review.text, "base_revision")},
                        follow_redirects=False)
    assert saved.status_code == 303
    after = client_profile(client)
    assert after["experience"][0]["bullets"][0]["text"] == "Built and documented a Flask REST API"
    after["experience"][0]["bullets"][0]["text"] = before["experience"][0]["bullets"][0]["text"]
    assert after == before


def test_back_to_editing_returns_to_the_section(client, sample_profile):
    saved_profile(client, sample_profile)
    fields = form_fields(client.get("/profile/edit?section=skills").text)
    review = client.post("/profile/review", data=fields)
    back = client.post("/profile/edit", data={"payload": hidden(review.text, "payload"), "section": "skills"})
    assert {t for t, visible in cards(back.text).items() if visible} == {"Skills", "Certifications"}


def test_a_problem_in_the_section_keeps_it_and_one_outside_shows_everything(client, sample_profile):
    saved_profile(client, sample_profile)
    fields = form_fields(client.get("/profile/edit?section=experience").text)
    inside = fields | {"experience-0-end": "2020-01"}  # ends before it starts
    response = client.post("/profile/review", data=inside)
    assert response.status_code == 422
    assert {t for t, visible in cards(response.text).items() if visible} == {"Experience"}
    outside = fields | {"contact-name": ""}  # the name is required
    response = client.post("/profile/review", data=outside)
    assert response.status_code == 422 and all(cards(response.text).values())
    assert "There&#39;s a problem outside this section, so the whole profile is shown." in response.text


def test_a_damaged_section_value_means_the_whole_profile(client, sample_profile):
    saved_profile(client, sample_profile)
    fields = form_fields(client.get("/profile/edit").text) | {"section": "hobbies"}
    review = client.post("/profile/review", data=fields)
    assert review.status_code == 200 and 'name="section"' not in review.text


def test_the_profile_page_links_each_section_to_its_editor(client, sample_profile):
    saved_profile(client, sample_profile)
    page = client.get("/profile").text
    for section in SECTIONS:
        assert f'class="section-edit" href="/profile/edit?section={section}"' in page


def test_hidden_sections_stay_hidden_whatever_their_layout(client):
    css = client.get("/static/style.css").text
    # The profile cards get display: grid on wide screens, which would override the browser's own [hidden] rule.
    assert "[hidden] { display: none !important; }" in css
    assert css.count("[hidden]") == 1  # the one global rule; no per-class copies to keep in step


def test_a_section_opened_before_another_save_never_reverts_it(client, sample_profile):
    saved_profile(client, sample_profile)
    stale = form_fields(client.get("/profile/edit?section=experience").text)  # opened at revision 1
    assert stale["opened_revision"] == "1"
    # Another tab adds a portfolio link: revision 2.
    sample_profile["contact"]["links"]["portfolio"] = "https://jordan.example"
    review_and_save(client, profile_form(sample_profile))
    name = next(n for n, v in stale.items() if n.endswith("-text") and v.startswith("Built a Flask"))
    stale[name] = "Built and documented a Flask REST API"
    refused = client.post("/profile/review", data=stale)
    assert refused.status_code == 409
    assert "The profile changed since you opened this section (revision 1 → 2)" in unescape(refused.text)
    fresh = form_fields(refused.text)
    assert fresh["contact-links-portfolio"] == "https://jordan.example"  # the other sections as saved now
    assert fresh[name] == "Built and documented a Flask REST API"  # this section's edits kept
    assert fresh["opened_revision"] == "2"
    review = client.post("/profile/review", data=fresh)
    assert review.status_code == 200
    client.post("/profile/save", data={"payload": hidden(review.text, "payload"),
                                       "base_revision": hidden(review.text, "base_revision")})
    after = client_profile(client)
    assert after["contact"]["links"]["portfolio"] == "https://jordan.example"
    assert after["experience"][0]["bullets"][0]["text"] == "Built and documented a Flask REST API"


@pytest.mark.parametrize("opened", ["", "x", "99999999999"])
def test_a_missing_or_damaged_opened_revision_is_treated_as_stale(client, sample_profile, opened):
    saved_profile(client, sample_profile)
    fields = form_fields(client.get("/profile/edit?section=skills").text) | {"opened_revision": opened}
    assert client.post("/profile/review", data=fields).status_code == 409


def test_section_and_step_names_keep_str_title_working():
    from app.routes.profile import Section
    from app.services.workspace import Step
    from app.templating import templates

    # Both are str enums: a "title" property would hide str.title() and break Jinja's title filter.
    assert Section.CONTACT.title() == "Contact" and Section.CONTACT.label == "Contact and summary"
    assert Step.TRACK.title() == "Track" and Step.TRACK.label == "Submit and track"
    assert templates.env.from_string("{{ s | title }}").render(s=Section.SKILLS) == "Skills"
