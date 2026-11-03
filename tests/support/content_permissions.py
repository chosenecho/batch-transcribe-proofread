"""Explicit permission for project-authored offline test material."""
from pathlib import Path

from bili_asr.content_rights import initialize_rights, record_rights


def grant_test_permission(connection, edition, roots=None):
    if roots is None:
        roots = tuple(Path(r[2]).parent for r in connection.execute('PRAGMA database_list') if r[2])
    initialize_rights(connection)
    return record_rights(connection, action='grant', edition_id=edition['edition_id'],
                         actor='synthetic-test-author', note='Original offline fixture, never a platform permission',
                         artifact_roots=roots, permission={
                             'basis': 'own-work', 'license': 'CC-BY-SA-4.0',
                             'rights_holder': 'Synthetic fixture authors', 'source_creator': 'Test author',
                             'attribution': 'Original synthetic test text',
                             'permission_reference': 'test:own-work', 'evidence_sha256': 'a' * 64,
                             'platform_access_reference': 'test:offline-only', 'expires_at': None,
                             'public_distribution': True, 'adaptation': True, 'commercial_use': True,
                             'open_licensing': True, 'includes_source_transcript': True,
                         })
