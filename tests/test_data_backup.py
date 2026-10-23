"""Portable backup round trips and failure recovery (synthetic data)."""
import base64
import copy
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Job
from app.services import backup, jobs, package, profile, resume_attachment
from tests.conftest import BASE_URL, NO_NETWORK
from tests.resume_files import pdf_bytes
from tests.synthetic import DEMO_JOB
from tests.test_package import approve, record_applied
from tests.test_resume_export_routes import accepted_job
from tests.test_routes import hidden


def exported(client):
    response = client.get('/data/backup')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def review(client, data):
    return client.post('/data/restore/review', files={'backup': ('workspace.json', backup.encode(data), 'application/json')})


def schedule(client, data):
    response = review(client, data)
    assert response.status_code == 200, response.text
    response = client.post('/data/restore/confirm', data={'token': hidden(response.text, 'token'), 'confirm': 'replace'})
    assert response.status_code == 200 and 'Restore scheduled.' in response.text


def add_job(client):
    with client.app.state.session_factory() as session:
        return jobs.create(session, jobs.clean_input(**{**DEMO_JOB, 'title': 'A later job'})).id


def populated(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    content = pdf_bytes([sample_profile['contact']['name'], 'Synthetic resume attachment'])
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        approve(session, job, candidate)
        resume_attachment.attach(session, client.app.state.settings.resolved_data_dir, job, candidate,
                                 content, 'Resume.pdf', package.package_token(job, job.application, candidate))
        record_applied(session, job, candidate, note='Submitted synthetic application')
    return job_id, content


def test_empty_backup_and_discoverable_exports(client):
    assert 'Your data &amp; backups' in client.get('/jobs').text
    assert 'Download backup' in client.get('/data').text
    data = exported(client)
    parsed, rows = backup.validate(backup.encode(data))
    assert parsed == data and all(not value for value in rows.values())
    assert 'openrouter' not in json.dumps(data)


def test_restart_restores_facts_history_snapshot_and_attachment_and_keeps_recovery(settings, sample_profile):
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as client:
        job_id, content = populated(client, sample_profile)
        original = exported(client)
        add_job(client)
        before = exported(client)
        schedule(client, original)
        assert len(exported(client)['tables']['jobs']) == 2
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as restarted:
        restored = exported(restarted)
        assert restored == original
        assert restored['tables']['candidates'] == original['tables']['candidates']
        assert restored['tables']['jobs'] == original['tables']['jobs']
        app = restored['tables']['applications'][0]
        original_app = original['tables']['applications'][0]
        assert app['resume_versions'] == original_app['resume_versions']
        assert app['submitted_snapshots'][0]['profile'] == original_app['submitted_snapshots'][0]['profile']
        assert app['submitted_snapshots'][0]['resume'] == original_app['submitted_snapshots'][0]['resume']
        assert app['notes'] == original_app['notes']
        assert resume_attachment.path_for(settings.resolved_data_dir, job_id).read_bytes() == content
        files = list((settings.resolved_data_dir / 'backups').glob('before-restore-*.resuapply.json'))
        assert len(files) == 1 and json.loads(files[0].read_bytes()) == before
        import os
        if os.name != 'nt':
            assert files[0].stat().st_mode & 0o077 == 0
        assert restarted.get('/data/recovery/' + files[0].name).json() == before
        assert restarted.get('/data/recovery/something.json').status_code == 404
        assert 'Restore completed.' in restarted.get('/data').text
        assert not (settings.resolved_data_dir / backup.PENDING).exists()


@pytest.mark.parametrize('mutation', ['schema', 'missing_table', 'columns', 'foreign_key', 'layout', 'job_url', 'attachment'])
def test_invalid_backup_never_schedules_or_changes_workspace(client, sample_profile, mutation):
    populated(client, sample_profile)
    before = exported(client)
    invalid = copy.deepcopy(before)
    if mutation == 'schema':
        invalid['schema'] = 'future-version'
    elif mutation == 'missing_table':
        del invalid['tables']['candidates']
    elif mutation == 'columns':
        invalid['tables']['jobs'][0]['unexpected'] = 'value'
    elif mutation == 'foreign_key':
        invalid['tables']['applications'][0]['job_id'] = 999
    elif mutation == 'layout':
        invalid['tables']['applications'][0]['resume_layout'] = '../templates'
    elif mutation == 'job_url':
        invalid['tables']['jobs'][0]['url'] = 'javascript:alert(1)'
    else:
        invalid['attachments'][next(iter(invalid['attachments']))] = base64.b64encode(b'changed').decode()
    assert review(client, invalid).status_code == 422
    assert exported(client) == before
    assert not (client.app.state.settings.resolved_data_dir / backup.PENDING).exists()


def test_snapshot_uploaded_html_is_rebuilt_from_frozen_escaped_facts(client, sample_profile):
    populated(client, sample_profile)
    data = exported(client)
    saved = data['tables']['applications'][0]['submitted_snapshots'][0]
    saved['resume_html'] = '<script>uploadedCode()</script>'
    saved['resume']['contact']['name'] = '<script>factText()</script>'
    _, rows = backup.validate(backup.encode(data))
    rebuilt = rows['applications'][0]['submitted_snapshots'][0].resume_html
    assert 'uploadedCode' not in rebuilt and '<script>' not in rebuilt
    assert '&lt;script&gt;factText()&lt;/script&gt;' in rebuilt


def test_restore_requires_fresh_review_and_explicit_confirmation(client):
    response = review(client, exported(client))
    token = hidden(response.text, 'token')
    assert client.post('/data/restore/confirm', data={'token': token}).status_code == 422
    assert client.post('/data/restore/confirm', data={'token': 'wrong', 'confirm': 'replace'}).status_code == 409
    add_job(client)
    assert client.post('/data/restore/confirm', data={'token': token, 'confirm': 'replace'}).status_code == 409
    assert not (client.app.state.settings.resolved_data_dir / backup.PENDING).exists()


def test_expired_review_is_refused_and_scheduled_restore_can_be_cancelled(client):
    data = exported(client)
    response = review(client, data)
    client.app.state.restore_review['expires'] = 0
    assert client.post('/data/restore/confirm', data={'token': hidden(response.text, 'token'), 'confirm': 'replace'}).status_code == 409
    schedule(client, data)
    assert client.post('/data/restore/cancel', follow_redirects=False).status_code == 303
    assert not (client.app.state.settings.resolved_data_dir / backup.PENDING).exists()


def test_changes_after_confirmation_cancel_restore_on_startup(client):
    schedule(client, exported(client))
    add_job(client)
    before = exported(client)
    folder = client.app.state.settings.resolved_data_dir
    backup.apply_pending(client.app.state.engine, folder)
    assert exported(client) == before
    assert 'workspace changed' in (folder / backup.RESULT).read_text()
    assert not (folder / backup.PENDING).exists()


def test_attachment_install_failure_rolls_back_database_and_previous_files(client, sample_profile, monkeypatch):
    empty = exported(client)
    job_id, content = populated(client, sample_profile)
    before = exported(client)
    schedule(client, empty)
    replace = backup.os.replace
    def fail_install(source, target):
        if 'restore-stage-' in str(source):
            raise OSError('Synthetic disk failure')
        return replace(source, target)
    monkeypatch.setattr(backup.os, 'replace', fail_install)
    folder = client.app.state.settings.resolved_data_dir
    backup.apply_pending(client.app.state.engine, folder)
    assert exported(client) == before
    assert resume_attachment.path_for(folder, job_id).read_bytes() == content
    assert 'current workspace was kept' in (folder / backup.RESULT).read_text()


def test_csv_quotes_rows_neutralizes_formulas_and_does_not_need_pdf_files(client, sample_profile):
    job_id, _ = populated(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.company = '=SUM(1,2)'
        job.title = 'Role, with comma'
        session.commit()
    resume_attachment.path_for(client.app.state.settings.resolved_data_dir, job_id).unlink()
    result = client.get('/data/applications.csv')
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    rows = list(csv.DictReader(io.StringIO(result.content.decode('utf-8-sig'))))
    assert rows[0]['Company'] == "'=SUM(1,2)" and rows[0]['Role'] == 'Role, with comma'
    assert client.get('/data/backup').status_code == 422


def test_invalid_and_oversized_uploads_are_refused(client, monkeypatch):
    assert client.post('/data/restore/review', data={}).status_code == 422
    assert client.post('/data/restore/review', files={'backup': ('bad.json', b'not json')}).status_code == 422
    monkeypatch.setattr(backup, 'MAX_BYTES', 300)
    assert client.post('/data/restore/review', files={'backup': ('big.json', b' ' * 301)}).status_code in (413, 422)


def test_unrestorable_recovery_copy_refuses_review_and_startup_restore(client, monkeypatch):
    empty = exported(client)
    add_job(client)
    before = exported(client)
    # The incoming backup is small enough, but its recovery copy would not be.
    monkeypatch.setattr(backup, 'MAX_BYTES', len(backup.encode(empty)) + 100)
    response = review(client, empty)
    assert response.status_code == 422 and 'restorable recovery backup' in response.text
    folder = client.app.state.settings.resolved_data_dir
    assert not (folder / backup.PENDING).exists()
    # The same check must run before the destructive startup work, too.
    backup.schedule(folder, empty, backup.fingerprint(before))
    backup.apply_pending(client.app.state.engine, folder)
    assert 'restorable recovery backup' in (folder / backup.RESULT).read_text()
    monkeypatch.setattr(backup, 'MAX_BYTES', 32 * 1024 * 1024)
    assert exported(client) == before
