"""Browser drafts recover edits without saving facts or trusting stored markup."""

from urllib.parse import urlsplit

import pytest

from app.services.profile import get_candidate
from tests.autofill_site import chromium
from tests.test_routes import profile_form, review_and_save


@pytest.fixture
def editor(client):
    api = pytest.importorskip('playwright.sync_api')
    try:
        executable = chromium()
    except LookupError:
        pytest.skip('Optional Chromium is not installed')
    with api.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable)
        page = browser.new_page()
        page.on('dialog', lambda dialog: dialog.accept())
        requests = []
        def serve(route):
            request = route.request
            url = urlsplit(request.url)
            requests.append((request.method, url.path))
            response = client.request(request.method, url.path + ('?' + url.query if url.query else ''),
                                      content=request.post_data_buffer, follow_redirects=True,
                                      headers={'content-type': request.headers.get('content-type', '')})
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get('content-type', 'text/plain'))
        page.route('**/*', serve)
        yield page, api.expect, requests
        browser.close()


def test_profile_recovers_added_rows_bullets_and_literal_text_without_saving(client, editor):
    page, expect, requests = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.locator('#contact-name').fill('Fictional Draft Author')
    page.get_by_label('Summary', exact=True).fill('<img src=x onerror=alert(1)> a draft, not markup')
    page.get_by_role('button', name='Add experience', exact=True).click()
    entry = page.locator('#experience-list > [data-item]').last
    entry.locator('input[name$="-organization"]').fill('Draft Studio')
    entry.get_by_role('button', name='Add bullet', exact=True).click()
    entry.get_by_label('Bullet', exact=True).nth(1).fill('Recovered second bullet')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    page.reload()
    expect(page.get_by_role('button', name='Restore draft', exact=True)).to_be_visible()
    expect(page.locator('#contact-name')).to_have_value('')
    page.get_by_role('button', name='Restore draft', exact=True).click()
    expect(page.locator('#contact-name')).to_have_value('Fictional Draft Author')
    expect(entry.locator('input[name$="-organization"]')).to_have_value('Draft Studio')
    expect(entry.get_by_label('Bullet', exact=True).nth(1)).to_have_value('Recovered second bullet')
    assert page.locator('img[src=x]').count() == 0
    assert not any(method == 'POST' for method, _ in requests)
    with client.app.state.session_factory() as session:
        assert get_candidate(session) is None


def test_stale_profile_draft_can_be_downloaded_but_cannot_replace_new_facts(client, sample_profile, editor):
    review_and_save(client, profile_form(sample_profile))
    page, expect, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.get_by_label('Summary', exact=True).fill('Older browser draft')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    updated = profile_form(sample_profile)
    updated['summary'] = 'Newer verified profile'
    review_and_save(client, updated)
    page.reload()
    expect(page.get_by_label('Summary', exact=True)).to_have_value('Newer verified profile')
    expect(page.get_by_role('button', name='Restore draft', exact=True)).to_be_hidden()
    expect(page.get_by_role('button', name='Download older draft', exact=True)).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role('button', name='Download older draft', exact=True).click()
    assert download.value.suggested_filename == 'resuapply-recovered-draft.txt'


def test_discard_and_expired_drafts_do_not_restore(editor):
    page, expect, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.locator('#contact-name').fill('Discard me')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    page.get_by_role('button', name='Discard saved draft', exact=True).click()
    page.reload()
    expect(page.get_by_role('button', name='Restore draft', exact=True)).to_be_hidden()
    page.locator('#contact-name').fill('Expired draft')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    page.add_init_script("""(() => {
        const key = Object.keys(localStorage).find(key => key.startsWith('resuapply:draft:'));
        const draft = JSON.parse(localStorage.getItem(key));
        draft.saved -= 8 * 24 * 3600 * 1000;
        localStorage.setItem(key, JSON.stringify(draft));
    })()""")
    page.reload()
    expect(page.get_by_role('button', name='Restore draft', exact=True)).to_be_hidden()


def test_storage_failure_preserves_editing_and_review_submission(editor):
    page, expect, requests = editor
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('disabled')}})")
    page.goto('http://127.0.0.1:8000/profile/edit')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('unavailable')
    page.locator('#contact-name').fill('Can still save')
    page.get_by_role('button', name='Review changes', exact=True).click()
    expect(page.get_by_role('heading', name='Review your profile changes', exact=True)).to_be_visible()
    assert ('POST', '/profile/review') in requests


def test_tampered_draft_keys_are_rejected_without_creating_markup(editor):
    page, expect, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.locator('#contact-name').fill('Legitimate draft')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    page.add_init_script("""(() => {
        const key = Object.keys(localStorage).find(key => key.startsWith('resuapply:draft:'));
        const draft = JSON.parse(localStorage.getItem(key));
        draft.groups = [['experience-list', ['\"><img src=x onerror=alert(1)>']]];
        localStorage.setItem(key, JSON.stringify(draft));
    })()""")
    page.reload()
    expect(page.get_by_role('button', name='Restore draft', exact=True)).to_be_hidden()
    assert page.locator('img[src=x]').count() == 0


def test_corrupt_record_does_not_disable_future_backups(editor):
    page, expect, _ = editor
    page.add_init_script("localStorage.setItem('resuapply:draft:v1:profile-all', 'not json')")
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.locator('#contact-name').fill('New valid draft')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')


@pytest.mark.parametrize('path', ['back_from_review', 'validation_error'])
def test_unsaved_server_responses_preserve_recovery_baseline(client, editor, path):
    page, expect, requests = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    if path == 'back_from_review':
        page.locator('#contact-name').fill('Unsaved author')
    page.get_by_label('Summary', exact=True).fill('Keep this until Save profile')
    expect(page.locator('.draft-recovery [role=status]')).to_contain_text('Draft saved')
    page.get_by_role('button', name='Review changes', exact=True).click()
    if path == 'back_from_review':
        page.get_by_role('button', name='Back to editing', exact=True).click()
    expect(page.get_by_label('Summary', exact=True)).to_have_value('Keep this until Save profile')
    page.goto('http://127.0.0.1:8000/welcome')
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.get_by_role('button', name='Restore draft', exact=True).click()
    expect(page.get_by_label('Summary', exact=True)).to_have_value('Keep this until Save profile')
    assert not any(url == '/profile/save' for _, url in requests)
    with client.app.state.session_factory() as session:
        assert get_candidate(session) is None
