import os
import gzip

from app.templating import asset
from tests.test_resume_export_routes import accepted_job


def test_versioned_text_assets_are_compressed_and_reusable(client):
    with client.stream('GET', asset('style.css'), headers={'Accept-Encoding': 'gzip'}) as response:
        compressed = b''.join(response.iter_raw())
    assert response.status_code == 200
    assert response.headers['Content-Encoding'] == 'gzip'
    assert len(compressed) < len(gzip.decompress(compressed)) / 2
    assert response.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    again = client.get(asset('style.css'), headers={'If-None-Match': response.headers['ETag']})
    assert again.status_code == 304 and 'immutable' in again.headers['Cache-Control']
    assert client.get('/static/style.css?v=old').headers['Cache-Control'] == 'no-cache'
    image = client.get(asset('resume-examples/classic.webp'))
    assert 'Content-Encoding' not in image.headers


def test_asset_versions_change_for_edits_within_a_second(tmp_path, monkeypatch):
    from app import templating
    monkeypatch.setattr(templating, 'STATIC_DIR', tmp_path)
    path = tmp_path / 'test.js'
    path.write_text('old')
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = templating.asset('test.js')
    path.write_text('new')
    os.utime(path, ns=(1_100_000_000, 1_100_000_000))
    assert templating.asset('test.js') != before


def test_only_pages_with_editors_and_previews_load_their_assets(client, sample_profile):
    home = client.get('/welcome').text
    assert 'drafts.js' not in home and 'resume-layouts.css' not in home
    assert 'drafts.js' in client.get('/profile/edit').text
    job = accepted_job(client, sample_profile)
    for step in ('requirements', 'review', 'track'):
        page = client.get(f'/jobs/{job}?step={step}').text
        assert 'resume.js' not in page and 'drafts.js' not in page
    resume = client.get(f'/jobs/{job}?step=resume').text
    assert 'resume.js' in resume and 'resume-layouts.css' in resume
    assert 'drafts.js' in client.get(f'/jobs/{job}?step=questions').text
