import pytest

from app.models import Job
from tests.test_draft_recovery import editor  # noqa: F401
from tests.test_resume_export_routes import accepted_job


@pytest.mark.parametrize('width', [320, 390])
def test_long_content_stays_inside_mobile_pages(client, sample_profile, editor, width, tmp_path):
    page, expect, requests = editor
    sample_profile['contact']['links']['portfolio'] = 'https://example.com/' + 'portfolio' * 20
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.title, job.company = 'PlatformEngineer' * 9, 'CompanyName' * 14
        session.commit()
    page.set_viewport_size({'width': width, 'height': 844})
    paths = ['/welcome', '/jobs', '/profile', '/profile/edit', '/resume-examples']
    paths += [f'/jobs/{job_id}?step={step}' for step in ('requirements', 'resume', 'questions', 'review', 'track')]
    for path in paths:
        page.goto('http://127.0.0.1:8000' + path)
        overflow = page.evaluate('''() => [...document.querySelectorAll('main *')].filter(e => {
          const r = e.getBoundingClientRect(); return r.width && (r.right > innerWidth + 1 || r.left < -1);
        }).slice(0, 6).map(e => e.tagName + '.' + e.className)''')
        fits = page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        if not fits:
            page.screenshot(path=str(tmp_path / 'overflow.png'), full_page=True)
        assert fits, (path, overflow, str(tmp_path / 'overflow.png'))
    page.goto('http://127.0.0.1:8000/profile/edit')
    field = page.locator('#contact-name')
    assert field.evaluate('e => parseFloat(getComputedStyle(e).fontSize)') >= 16
    field.fill('Mobile edit preserved')
    expect(field).to_have_value('Mobile edit preserved')
    review = page.get_by_role('button', name='Review changes', exact=False)
    review.scroll_into_view_if_needed()
    assert review.bounding_box()['height'] >= 44
