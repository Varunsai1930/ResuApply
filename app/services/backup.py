"""Portable local data backups; restore only during startup before workers exist."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, Date, Integer, String, create_engine, delete, select

from ..db import Base, MAX_ID, PydanticJSON, UTCDateTime, head_revision
from ..schemas.tracking import ReviewState, TrackingStatus
from ..unicode_text import has_invalid_unicode
from . import jobs, package, profile, resume_attachment

MAX_BYTES = 32 * 1024 * 1024
PENDING = "restore-pending.json"
RESULT = "restore-result.txt"


class BackupError(ValueError):
    pass


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _json_value(column, value):
    if value is None:
        return None
    if isinstance(column.type, PydanticJSON):
        return json.loads(column.type.adapter.dump_json(value, context={"preserve_stored_snapshot_profile": True}))
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def snapshot(connection, data_dir: Path, *, include_attachments=True) -> dict:
    tables = {}
    for table in Base.metadata.sorted_tables:
        tables[table.name] = [{c.name: _json_value(c, row[c.name]) for c in table.columns}
                              for row in connection.execute(select(table).order_by(table.c.id)).mappings()]
    files = {}
    if not include_attachments:
        return {"tables": tables}
    for application in tables['applications']:
        attachment = application['resume_attachment']
        if attachment:
            path = resume_attachment.path_for(data_dir, application['job_id'])
            try:
                content = path.read_bytes()
            except OSError:
                raise BackupError("An attached PDF is missing. Attach it again or remove its attachment before backing up.") from None
            if hashlib.sha256(content).hexdigest() != attachment['sha256']:
                raise BackupError("An attached PDF changed on disk. Attach it again before backing up.")
            files[str(application['job_id'])] = base64.b64encode(content).decode('ascii')
    return {"format": "resuapply-backup", "version": 1, "schema": head_revision(), "tables": tables, "attachments": files}


def fingerprint(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def _cell(column, value):
    if value is None:
        if not column.nullable:
            raise ValueError('Required value missing')
        return None
    kind = column.type
    if isinstance(kind, PydanticJSON):
        return kind.adapter.validate_python(value)
    if isinstance(kind, UTCDateTime):
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError('Timestamp has no timezone')
        return result
    if isinstance(kind, Boolean):
        if type(value) is not bool:
            raise ValueError('Invalid boolean')
    elif isinstance(kind, Integer):
        if type(value) is not int or not -MAX_ID <= value <= MAX_ID or (column.primary_key and value < 1):
            raise ValueError('Invalid integer')
    elif isinstance(kind, Date):
        return date.fromisoformat(value)
    elif isinstance(kind, String):
        if not isinstance(value, str) or (kind.length and len(value) > kind.length):
            raise ValueError('Invalid text')
    return value


def validate(content: bytes) -> tuple[dict, dict]:
    """Validate into a fresh temporary database, never execute uploaded SQL or HTML."""
    if len(content) > MAX_BYTES:
        raise BackupError("Keep the backup under 32 MB.")
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or has_invalid_unicode(data):
            raise ValueError('Invalid document')
        if (data.get('format'), data.get('version'), data.get('schema')) != ('resuapply-backup', 1, head_revision()):
            raise ValueError('Unsupported backup version')
        if set(data['tables']) != set(Base.metadata.tables) or not isinstance(data['attachments'], dict):
            raise ValueError('Invalid tables')
        rows = {}
        for table in Base.metadata.sorted_tables:
            items = data['tables'][table.name]
            if not isinstance(items, list) or len(items) > 20_000:
                raise ValueError('Invalid row count')
            rows[table.name] = []
            for item in items:
                if not isinstance(item, dict) or set(item) != set(table.columns.keys()):
                    raise ValueError('Invalid columns')
                rows[table.name].append({c.name: _cell(c, item[c.name]) for c in table.columns})
        if len(rows['candidates']) > 1:
            raise ValueError('Only one profile is supported')
        for candidate in rows['candidates']:
            profile.normalize(candidate['profile'].model_dump(), current=candidate['profile'], id_counters=candidate['id_counters'])
            if candidate['revision'] < 1:
                raise ValueError('Invalid revision')
        for job in rows['jobs']:
            jobs.clean_input(**{key: job[key] for key in ('title', 'company', 'description', 'location', 'url')})
            if job['revision'] < 1:
                raise ValueError('Invalid revision')
        attached_jobs = set()
        for application in rows['applications']:
            ReviewState(application['review_state'])
            TrackingStatus(application['tracking_status'])
            if application['priority'] not in ('high', 'normal', 'low') or application['resume_layout'] not in ('classic', 'modern', 'compact'):
                raise ValueError('Invalid presentation metadata')
            # Rebuild trusted markup from escaped frozen facts; uploaded HTML is never trusted.
            snapshots = []
            for saved in application['submitted_snapshots']:
                jobs.clean_input(**{key: saved.job.get(key, '') for key in ('title', 'company', 'description', 'location', 'url')})
                layout = saved.resume.get('_layout', 'classic')
                if layout not in ('classic', 'modern', 'compact'):
                    raise ValueError('Invalid snapshot layout')
                snapshots.append(saved.model_copy(update={'resume_html': package._resume_html(saved.resume)}))
            application['submitted_snapshots'] = snapshots
            attachment = application['resume_attachment']
            if attachment:
                key = str(application['job_id'])
                attached_jobs.add(key)
                payload = base64.b64decode(data['attachments'][key], validate=True)
                if len(payload) != attachment.size or hashlib.sha256(payload).hexdigest() != attachment.sha256:
                    raise ValueError('Attachment checksum mismatch')
                resume_attachment.read_pdf(payload)
        if set(data['attachments']) != attached_jobs:
            raise ValueError('Unexpected attachments')
        with tempfile.TemporaryDirectory(prefix='resuapply-backup-check-') as folder:
            engine = create_engine('sqlite:///' + str(Path(folder) / 'check.db'))
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as connection:
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                    for table in Base.metadata.sorted_tables:
                        if rows[table.name]:
                            connection.execute(table.insert(), rows[table.name])
                    if connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Broken references')
                    if len(rows['jobs']) != len(rows['applications']):
                        raise ValueError('Missing applications')
            finally:
                engine.dispose()
        return data, rows
    except BackupError:
        raise
    except Exception:
        raise BackupError("This backup is damaged or belongs to a different app version. Nothing was restored.") from None


def summary(data) -> dict:
    return {key: len(data['tables'][key]) for key in ('candidates', 'jobs', 'applications', 'answer_bank')}


def recovery_content(data) -> bytes:
    content = encode(data)
    try:
        validate(content)
    except BackupError:
        raise BackupError('The current workspace cannot be saved as a restorable recovery backup. '
                          'Restore was cancelled; keep a copy of the local data folder first.') from None
    return content


def private_write(path: Path, content: bytes):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def schedule(data_dir: Path, data, base: str):
    target = data_dir / PENDING
    if target.exists():
        raise BackupError("A restore is already scheduled. Cancel it before choosing another backup.")
    private_write(target, encode({'backup': data, 'base': base}))


def apply_pending(engine, data_dir: Path) -> None:
    """Run at startup only; keep recovery data and refuse a workspace changed after confirmation."""
    pending = data_dir / PENDING
    if not pending.exists():
        return
    staging = None
    old_folder = None
    installed = False
    try:
        if pending.stat().st_size > MAX_BYTES + 1024:
            raise BackupError("The scheduled restore is too large.")
        queued = json.loads(pending.read_bytes())
        data, rows = validate(encode(queued['backup']))
        staging = Path(tempfile.mkdtemp(prefix='restore-stage-', dir=data_dir))
        replacement = staging / 'attachments'
        replacement.mkdir()
        for key, value in data['attachments'].items():
            private_write(resume_attachment.path_for(staging, int(key)), base64.b64decode(value, validate=True))
        with engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            current = snapshot(connection, data_dir)
            if fingerprint(current) != queued['base']:
                raise BackupError("The workspace changed after restore was confirmed. Restore was cancelled; choose the backup again.")
            recovery_bytes = recovery_content(current)
            folder = data_dir / 'backups'
            folder.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            recovery = folder / f'before-restore-{stamp}.resuapply.json'
            private_write(recovery, recovery_bytes)
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(delete(table))
            for table in Base.metadata.sorted_tables:
                if rows[table.name]:
                    connection.execute(table.insert(), rows[table.name])
            attachments = data_dir / 'attachments'
            if attachments.exists():
                old_folder = folder / ('attachments-' + stamp)
                os.replace(attachments, old_folder)
            try:
                os.replace(replacement, attachments)
                installed = True
                connection.commit()
            except BaseException:
                if installed:
                    os.replace(attachments, replacement)
                if old_folder is not None:
                    os.replace(old_folder, attachments)
                raise
        (data_dir / RESULT).write_text('Restore completed. A recovery backup was kept in the backups folder.', encoding='utf-8')
    except Exception as exc:
        message = str(exc) if isinstance(exc, BackupError) else 'Restore could not finish. The current workspace was kept.'
        (data_dir / RESULT).write_text(message, encoding='utf-8')
    finally:
        pending.unlink(missing_ok=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
