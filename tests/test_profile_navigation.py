"""Browser checks for the profile outline, anchors and landing-page logo link."""

import re
from urllib.parse import urlsplit

import pytest

from tests.autofill_site import chromium
from tests.test_routes import profile_form, review_and_save


@pytest.mark.parametrize("width,height,motion", [
    (1273, 741, "no-preference"),
    (390, 844, "no-preference"),
    (1273, 741, "reduce"),
])
def test_profile_outline_follows_scrolling_and_keeps_active_links_visible(client, sample_profile, width, height, motion):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        executable = chromium()
    except LookupError:
        pytest.skip("Optional Chromium is not installed")
    review_and_save(client, profile_form(sample_profile))
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable)
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion=motion)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                url = urlsplit(route.request.url)
                response = client.get(url.path + ("?" + url.query if url.query else ""), follow_redirects=False)
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get("content-type", "text/plain"))

            # All requests go through the isolated test app, never the user's database.
            page.route("**/*", serve)
            page.goto("http://127.0.0.1:8000/profile")
            nav = page.get_by_role("navigation", name="Profile details")
            playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-contact")
            for section in ("experience", "projects", "education", "skills", "authorization", "preferences"):
                at_bottom = page.evaluate("""id => {
                    const section = document.getElementById(id);
                    const offset = document.querySelector('.site-header').getBoundingClientRect().bottom + 24;
                    window.scrollTo({top: section.getBoundingClientRect().top + scrollY - offset, behavior: 'instant'});
                    return scrollY + innerHeight >= document.documentElement.scrollHeight - 2;
                }""", "profile-" + section)
                expected = "preferences" if at_bottom else section
                playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-" + expected)
            if width < 760:
                page.wait_for_function("""() => {
                    const nav = document.querySelector('.profile-jump').getBoundingClientRect();
                    const active = document.querySelector('.profile-jump [aria-current]').getBoundingClientRect();
                    return active.left >= nav.left - 1 && active.right <= nav.right + 1;
                }""")
                assert page.evaluate("scrollY > 500"), "Highlighting the mobile outline pulled the page back up"

            # Native deep links and browser Back must still select the right section.
            page.goto("http://127.0.0.1:8000/profile#profile-projects")
            playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-projects")
            nav.get_by_role("link", name="Experience", exact=True).click()
            playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-experience")
            playwright.expect(page).to_have_url("http://127.0.0.1:8000/profile#profile-experience")
            page.go_back()
            playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-projects")

            # Certifications share the Skills link even though they are a separate card.
            page.get_by_role("heading", name=re.compile(r"^Certifications")).evaluate("""heading => {
                const offset = document.querySelector('.site-header').getBoundingClientRect().bottom + 24;
                scrollTo({top: heading.closest('section').getBoundingClientRect().top + scrollY - offset, behavior: 'instant'});
            }""")
            playwright.expect(nav.locator('[aria-current="location"]')).to_have_attribute("href", "#profile-skills")
            page.get_by_role("link", name="ResuApply", exact=True).click()
            playwright.expect(page).to_have_url("http://127.0.0.1:8000/welcome")
            assert not errors
        finally:
            browser.close()
