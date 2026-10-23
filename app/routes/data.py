"""Local backup downloads, human-readable export and reviewed restore scheduling."""
import csv
import io
import re
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..services import backup
from ..templating import templates
from .forms import read_form, upload_too_large

router = APIRouter(prefix='/data')
HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


def current(request, *, include_attachments=True):
    with request.app.state.engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        return backup.snapshot(connection, request.app.state.settings.resolved_data_dir,
                               include_attachments=include_attachments)


def page(request, error='', status_code=200, message=''):
    folder = request.app.state.settings.resolved_data_dir
    recovery = sorted((folder / 'backups').glob('before-restore-*.resuapply.json'), reverse=True)[:20]
    result = folder / backup.RESULT
    return templates.TemplateResponse(request, 'data.html', {
        'active': None, 'error': error, 'message': message,
        'pending': (folder / backup.PENDING).exists(),
        'result': result.read_text(encoding='utf-8') if result.exists() else '',
        'recovery': [p.name for p in recovery],
    }, status_code=status_code, headers=HEADERS)


def download(content, filename, media_type='application/json'):
    return Response(content, media_type=media_type,
                    headers={**HEADERS, 'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('')
def overview(request: Request):
    return page(request)


@router.get('/backup')
def export_backup(request: Request):
    try:
        content = backup.encode(current(request))
        if len(content) > backup.MAX_BYTES:
            raise backup.BackupError('This workspace exceeds the 32 MB portable backup limit. Keep a copy of the local data folder instead.')
        return download(content, 'resuapply-backup.resuapply.json')
    except backup.BackupError as exc:
        return page(request, str(exc), 422)


@router.get('/applications.csv')
def export_csv(request: Request):
    rows = current(request, include_attachments=False)['tables']
    jobs = {job['id']: job for job in rows['jobs']}
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(('Company', 'Role', 'Location', 'Status', 'Priority', 'Deadline', 'Applied on', 'Follow up after', 'Posting'))
    def cell(value):
        value = str(value or '')
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
    for app in rows['applications']:
        job = jobs[app['job_id']]
        writer.writerow(map(cell, [job['company'], job['title'], job['location'], app['tracking_status'], app['priority'],
                                  app['deadline'], app['applied_on'], app['follow_up_after'], job['url']]))
    return download(output.getvalue().encode('utf-8-sig'), 'resuapply-applications.csv', 'text/csv')


@router.get('/recovery/{filename}')
def recovery(request: Request, filename: str):
    if not re.fullmatch(r'before-restore-[0-9]{8}T[0-9]{12}Z\.resuapply\.json', filename):
        raise HTTPException(404, 'Recovery backup not found.')
    path = request.app.state.settings.resolved_data_dir / 'backups' / filename
    if not path.is_file():
        raise HTTPException(404, 'Recovery backup not found.')
    return download(path.read_bytes(), filename)


@router.post('/restore/review')
async def review_restore(request: Request):
    if upload_too_large(request, backup.MAX_BYTES):
        return page(request, 'Keep the backup under 32 MB.', 413)
    form = await read_form(request)
    upload = form.get('backup')
    if not isinstance(upload, UploadFile):
        return page(request, 'Choose a ResuApply backup file.', 422)
    try:
        content = await upload.read(backup.MAX_BYTES + 1)
    finally:
        await upload.close()
    try:
        data, _ = await run_in_threadpool(backup.validate, content)
        before = await run_in_threadpool(current, request)
        await run_in_threadpool(backup.recovery_content, before)
    except backup.BackupError as exc:
        return page(request, str(exc), 422)
    token = secrets.token_urlsafe(24)
    request.app.state.restore_review = {'token': token, 'data': data, 'base': backup.fingerprint(before), 'expires': time.monotonic() + 900}
    return templates.TemplateResponse(request, 'data_restore.html', {'active': None, 'token': token,
        'incoming': backup.summary(data), 'existing': backup.summary(before)}, headers=HEADERS)


@router.post('/restore/confirm')
async def confirm_restore(request: Request):
    form = await read_form(request)
    reviewed = getattr(request.app.state, 'restore_review', None)
    if not reviewed or reviewed['expires'] < time.monotonic() or not secrets.compare_digest(str(form.get('token', '')), reviewed['token']):
        return page(request, 'The restore review expired or changed. Choose the backup again.', 409)
    if form.get('confirm') != 'replace':
        return page(request, 'Confirm that the backup will replace this workspace.', 422)
    try:
        before = await run_in_threadpool(current, request)
        if backup.fingerprint(before) != reviewed['base']:
            raise backup.BackupError('Your workspace changed during review. Choose the backup again to review the current data.')
        await run_in_threadpool(backup.schedule, request.app.state.settings.resolved_data_dir, reviewed['data'], reviewed['base'])
    except backup.BackupError as exc:
        return page(request, str(exc), 409)
    request.app.state.restore_review = None
    return page(request, message='Restore scheduled. Restart ResuApply to apply it. Any saved changes before restarting will cancel this restore.')


@router.post('/restore/cancel')
def cancel_restore(request: Request):
    (request.app.state.settings.resolved_data_dir / backup.PENDING).unlink(missing_ok=True)
    request.app.state.restore_review = None
    return RedirectResponse('/data', status_code=303)
