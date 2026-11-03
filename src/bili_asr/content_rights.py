"""Evidence-bound content permissions, independent of editorial approval."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from functools import lru_cache, wraps
from importlib import resources
from pathlib import Path

from bili_asr.canonical_json import canonical, digest
from bili_asr.compliance import compliance_enabled
from bili_asr.publication import get_ai_artifacts, get_edition
from bili_asr.publication_content import normalize_text
from bili_asr.storage.publication import PublicationRepository

LICENSE_URLS = {
    'CC-BY-4.0': 'https://creativecommons.org/licenses/by/4.0/',
    'CC-BY-SA-4.0': 'https://creativecommons.org/licenses/by-sa/4.0/',
    'CC0-1.0': 'https://creativecommons.org/publicdomain/zero/1.0/',
}
PERMISSION_FIELDS = frozenset({
    'basis', 'license', 'rights_holder', 'source_creator', 'attribution',
    'permission_reference', 'evidence_sha256', 'platform_access_reference', 'expires_at',
    'public_distribution', 'adaptation', 'commercial_use', 'open_licensing',
    'includes_source_transcript',
})
_SCHEMA = resources.files('bili_asr.storage').joinpath('schema-rights.sql')
PUBLICATION_ENABLED_ENV_VAR = 'BILI_PUBLICATION_ENABLED'
_HELD_DISTRIBUTION_LOCKS: set[Path] = set()


class ContentRightsError(ValueError):
    """Missing, stale, expired or suspended public-content permission."""


def require_publication_enabled() -> None:
    """Apply the publication opt-in only under the compliance policy."""
    if compliance_enabled() and os.environ.get(PUBLICATION_ENABLED_ENV_VAR) != '1':
        raise ContentRightsError(
            f'content-rights: content permission and public publication are disabled by default; '
            f'set {PUBLICATION_ENABLED_ENV_VAR}=1 only after reviewing the release and access basis'
        )


@contextmanager
def distribution_lock(connection, *, blocking: bool = False):
    """Serialize authorization changes with the complete export installation."""
    from bili_asr.file_ops import exclusive_lock as _exclusive_lock
    database = next((r[2] for r in connection.execute('PRAGMA database_list') if r[1] == 'main'), '')
    # Keep the marker beside the root so a read-only archive can still export.
    archive = Path(database).parent if database else None
    lock_path = archive.with_name(archive.name + '.content-distribution') if archive else None
    if lock_path is None:
        with nullcontext():
            yield
        return
    # A nested mutation in this process must fail fast, while independent
    # processes may wait for the transaction that currently owns the lock.
    nested = lock_path in _HELD_DISTRIBUTION_LOCKS
    with _exclusive_lock(lock_path, blocking=blocking and not nested):
        _HELD_DISTRIBUTION_LOCKS.add(lock_path)
        try:
            yield
        finally:
            _HELD_DISTRIBUTION_LOCKS.discard(lock_path)


def serialized_distribution(function):
    @wraps(function)
    def wrapped(connection, *args, **kwargs):
        require_publication_enabled()
        if connection.in_transaction:
            raise ContentRightsError('content-rights: finish caller transaction before public export')
        with distribution_lock(connection):
            return function(connection, *args, **kwargs)
    return wrapped


def initialize_rights(connection: sqlite3.Connection) -> None:
    """Explicit additive initialization; never infer permissions for old releases."""
    require_publication_enabled()
    PublicationRepository(connection)
    if connection.in_transaction:
        raise ValueError('content-rights: finish caller transaction before initialization')
    if _has_schema(connection):
        return
    connection.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA.read_text(encoding='utf-8') + '\nCOMMIT;')


@lru_cache(maxsize=1)
def _expected_schema() -> dict:
    with sqlite3.connect(':memory:') as reference:
        reference.executescript(_SCHEMA.read_text(encoding='utf-8'))
        return {r[0]: r[1] for r in reference.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND "
            "(name = 'content_rights_events' OR name LIKE 'idx_content_rights_%' OR name LIKE 'immutable_content_rights_%')"
        )}


def _has_schema(connection: sqlite3.Connection) -> bool:
    actual = {r[0]: r[1] for r in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND "
        "(name = 'content_rights_events' OR name LIKE 'idx_content_rights_%' OR name LIKE 'immutable_content_rights_%')"
    )}
    if not actual:
        return False
    expected = _expected_schema()
    def normalize(sql):
        return ' '.join(sql.replace('IF NOT EXISTS ', '').split())
    if set(actual) != set(expected) or any(normalize(actual[k]) != normalize(v) for k, v in expected.items()):
        raise ContentRightsError('content-rights: altered rights schema')
    return True


def normalize_permission(permission: dict) -> dict:
    if not isinstance(permission, dict) or set(permission) != PERMISSION_FIELDS:
        raise ContentRightsError('content-rights: permission fields differ from the contract')
    result = dict(permission)
    for key in ('basis', 'license', 'rights_holder', 'source_creator', 'attribution',
                'permission_reference', 'evidence_sha256', 'platform_access_reference'):
        result[key] = normalize_text(result[key], key, nonempty=True, multiline=False)
    if result['basis'] not in {'written-permission', 'open-license', 'own-work'}:
        raise ContentRightsError('content-rights: unsupported permission basis')
    if result['license'] not in LICENSE_URLS:
        raise ContentRightsError('content-rights: an identified supported open content license is required')
    evidence = result['evidence_sha256']
    if len(evidence) != 64 or any(c not in '0123456789abcdef' for c in evidence):
        raise ContentRightsError('content-rights: evidence SHA-256 must be lowercase hex')
    expiry = result['expires_at']
    if expiry is not None and (type(expiry) is not int or expiry <= int(time.time())):
        raise ContentRightsError('content-rights: expiry must be null or a future Unix timestamp')
    for key in ('public_distribution', 'adaptation', 'commercial_use', 'open_licensing', 'includes_source_transcript'):
        if result[key] is not True:
            raise ContentRightsError(f'content-rights: {key} must be explicitly confirmed')
    return result


def record_rights(connection: sqlite3.Connection, *, action: str, actor: str, note: str,
                  edition_id: str | None = None, video_part_id: int | None = None,
                  permission: dict | None = None, artifact_roots=()) -> dict:
    require_publication_enabled()
    if action not in {'grant', 'revoke', 'hold', 'clear'}:
        raise ContentRightsError('content-rights: unsupported decision')
    actor = normalize_text(actor, 'actor', nonempty=True, multiline=False)
    note = normalize_text(note, 'note', nonempty=True)
    if not _has_schema(connection):
        raise ContentRightsError('content-rights: run publication rights init first')
    repository = PublicationRepository(connection)
    with repository.transaction():
        edition = None
        if action in {'grant', 'revoke'}:
            if not edition_id or video_part_id is not None:
                raise ContentRightsError('content-rights: edition ID is required for grant/revoke')
            edition = get_edition(connection, edition_id)
            video_part_id = edition['video_part_id']
        elif edition_id is not None or type(video_part_id) is not int or video_part_id < 1:
            raise ContentRightsError('content-rights: positive video part ID is required for hold/clear')
        payload = {'action': action, 'actor': actor, 'note': note, 'recorded_at': int(time.time()),
                   'video_part_id': video_part_id, 'edition_id': edition_id}
        if action == 'grant':
            artifacts = get_ai_artifacts(connection, edition['revision_id'], artifact_roots)
            payload.update(permission=normalize_permission(permission), content_sha256=edition['content_sha256'],
                           review_sha256=hashlib.sha256(artifacts['review.md']).hexdigest())
        elif permission is not None:
            raise ContentRightsError('content-rights: permission is only valid for grant')
        cursor = connection.execute(
            'INSERT INTO content_rights_events(video_part_id, edition_id, action, payload_json, payload_sha256) '
            'VALUES (?, ?, ?, ?, ?)', (video_part_id, edition_id, action, canonical(payload), digest(payload))
        )
        return {'event_id': cursor.lastrowid, **payload}


def _event(connection, *, part_id: int, edition_id: str | None) -> dict | None:
    row = connection.execute(
        'SELECT * FROM content_rights_events WHERE video_part_id = ? AND edition_id IS ? '
        'ORDER BY event_id DESC LIMIT 1', (part_id, edition_id)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row['payload_json'])
        if (canonical(payload) != row['payload_json'] or digest(payload) != row['payload_sha256']
                or payload['action'] != row['action'] or payload['video_part_id'] != part_id
                or payload['edition_id'] != edition_id):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise ContentRightsError('content-rights: corrupt decision evidence') from exc
    return {'event_id': row['event_id'], **payload}


def rights_status(connection, edition: dict, review_document: bytes | None = None) -> dict:
    """Missing optional schema and missing grants always mean private-only."""
    if not _has_schema(connection):
        return {'status': 'unknown', 'public': False}
    hold = _event(connection, part_id=edition['video_part_id'], edition_id=None)
    if hold and hold['action'] == 'hold':
        return {'status': 'held', 'public': False, 'event_id': hold['event_id']}
    event = _event(connection, part_id=edition['video_part_id'], edition_id=edition['edition_id'])
    if not event:
        return {'status': 'unknown', 'public': False}
    if event['action'] == 'revoke':
        return {'status': 'revoked', 'public': False, 'event_id': event['event_id']}
    if event.get('content_sha256') != edition['content_sha256']:
        raise ContentRightsError('content-rights: permission content hash mismatch')
    expiry = event['permission']['expires_at']
    if expiry is not None and expiry <= int(time.time()):
        return {'status': 'expired', 'public': False, 'event_id': event['event_id']}
    normalize_permission(event['permission'])
    if review_document is not None and event.get('review_sha256') != hashlib.sha256(review_document).hexdigest():
        raise ContentRightsError('content-rights: source reference is not covered by permission')
    return {'status': 'granted', 'public': True, 'event_id': event['event_id'], 'permission': event['permission']}


def require_public_rights(connection, edition: dict, review_document: bytes) -> dict:
    status = distribution_status(connection, edition, review_document)
    if not status['public']:
        raise ContentRightsError(f"content-rights: public distribution denied ({status['status']}); record permission first")
    return status


def distribution_status(connection, edition: dict, review_document: bytes | None = None) -> dict:
    """Release eligibility is policy-specific; ledger facts remain independent."""
    if not compliance_enabled():
        return {'status': 'unchecked', 'public': True}
    return rights_status(connection, edition, review_document)


def public_notice(edition: dict, status: dict) -> dict:
    permission = status['permission']
    return {'editionId': edition['edition_id'], 'videoPartId': edition['video_part_id'],
            'contentSha256': edition['content_sha256'], 'license': permission['license'],
            'licenseUrl': LICENSE_URLS[permission['license']], 'rightsHolder': permission['rights_holder'],
            'sourceCreator': permission['source_creator'], 'attribution': permission['attribution'],
            'sourceUrl': edition['content']['source']['url'],
            'scope': ['reader-document', 'full-source-transcript'], 'expiresAt': permission['expires_at']}
