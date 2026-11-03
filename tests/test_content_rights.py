"""Public distribution requires actual scoped evidence independent of reviews."""
import json
import sqlite3
import time

import pytest

from bili_asr import cli
from bili_asr.content_rights import (
    ContentRightsError,
    distribution_lock,
    initialize_rights,
    record_rights,
    require_publication_enabled,
    rights_status,
)
from bili_asr.export_snapshot import ExportSnapshotError
from bili_asr.publication import (
    create_edition,
    edit_edition,
    publish_edition,
    review_edition,
    withdraw_release,
)
from bili_asr.publication_export import (
    export_editorial,
    export_publication_drafts,
    export_publications,
)
from bili_asr.storage.database import connect_database
from bili_asr.storage.snapshots import (
    SnapshotDatabaseError,
    create_database_snapshot,
    validate_snapshot_database,
)
from tests.support.content_permissions import grant_test_permission
from tests.test_publication import seeded_publication

pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


@pytest.fixture
def manuscript(tmp_path):
    root = tmp_path / 'private'
    connection, revision, roots = seeded_publication(root)
    edition = create_edition(connection, revision_id=revision, artifact_roots=roots, actor='editor')
    try:
        yield connection, edition, root, roots, tmp_path / 'public'
    finally:
        connection.close()


def editorial_approve(connection, edition):
    for status, expected in [('in-review', 'pending-review'), ('approved', 'in-review')]:
        review_edition(connection, edition_id=edition['edition_id'], status=status,
                       content_sha256=edition['content_sha256'], expected_status=expected, actor='reviewer')


@pytest.mark.parametrize('value', [None, '', '0', 'true', ' 1'])
def test_default_policy_publishes_and_exports_without_inventing_permission(manuscript, monkeypatch, value):
    connection, edition, root, roots, output = manuscript
    if value is None:
        monkeypatch.delenv('BILI_COMPLIANCE_ENABLED', raising=False)
    else:
        monkeypatch.setenv('BILI_COMPLIANCE_ENABLED', value)
    monkeypatch.delenv('BILI_PUBLICATION_ENABLED', raising=False)
    require_publication_enabled()
    drafts = output.parent / 'default-drafts'
    assert export_publication_drafts(connection, artifact_roots=roots, output=drafts) == 1
    assert not (drafts / 'content-rights.json').exists()
    assert rights_status(connection, edition) == {'status': 'unknown', 'public': False}
    editorial_approve(connection, edition)
    publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                    write_root=root, actor='publisher')
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    assert not (output / 'content-rights.json').exists()
    initialize_rights(connection)
    record_rights(connection, action='hold', video_part_id=edition['video_part_id'],
                  actor='operator', note='Recorded while checks are optional')
    assert rights_status(connection, edition)['status'] == 'held'
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    monkeypatch.setenv('BILI_COMPLIANCE_ENABLED', '1')
    monkeypatch.setenv('BILI_PUBLICATION_ENABLED', '1')
    assert export_publications(connection, artifact_roots=roots, output=output) == 0


def test_default_cli_allows_registration_and_text_export_without_private_flag(manuscript, monkeypatch, capsys):
    connection, _edition, root, _roots, _output = manuscript
    connection.close()
    monkeypatch.delenv('BILI_COMPLIANCE_ENABLED', raising=False)
    monkeypatch.delenv('BILI_PUBLICATION_ENABLED', raising=False)
    assert cli.main(['publication', 'rights', 'init', '--archive-root', str(root)]) == 0
    capsys.readouterr()
    assert cli.main(['export', '--archive-root', str(root), '--format', 'json', '--with-text']) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_content_permission_and_publication_are_disabled_without_explicit_opt_in(manuscript, monkeypatch):
    connection, edition, _root, roots, output = manuscript
    monkeypatch.delenv('BILI_PUBLICATION_ENABLED', raising=False)
    with pytest.raises(ContentRightsError, match='disabled by default'):
        initialize_rights(connection)
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'content_rights_events'").fetchone() is None
    with pytest.raises(ContentRightsError, match='disabled by default'):
        record_rights(connection, action='grant', edition_id=edition['edition_id'],
                      actor='admin', note='Disabled', permission={})
    with pytest.raises(ContentRightsError, match='disabled by default'):
        export_publication_drafts(connection, artifact_roots=roots, output=output)
    with pytest.raises(ContentRightsError, match='disabled by default'):
        publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                        write_root=_root, actor='publisher')
    assert not output.exists()
    export_editorial(connection, revision_id=edition['revision_id'], edition_id=edition['edition_id'],
                     artifact_roots=roots, output=output.parent / 'private-disabled-review')
    assert (output.parent / 'private-disabled-review' / 'review.md').exists()
    with pytest.raises(ContentRightsError, match='disabled by default'):
        require_publication_enabled()


def test_approval_does_not_authorize_publication_and_private_review_remains_available(manuscript):
    connection, edition, root, roots, output = manuscript
    editorial_approve(connection, edition)
    before = connection.total_changes
    with pytest.raises(ContentRightsError, match='unknown'):
        publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                        write_root=root, actor='publisher')
    assert connection.total_changes == before
    assert not (root / 'publications').exists()
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 0
    assert not list(output.rglob('*.md'))
    export_editorial(connection, revision_id=edition['revision_id'], edition_id=edition['edition_id'],
                     artifact_roots=roots, output=output.parent / 'private-review')
    assert (output.parent / 'private-review' / 'review.md').exists()


def test_deactivation_blocks_existing_authorized_release_without_mutating_it(manuscript, monkeypatch):
    connection, edition, root, roots, output = manuscript
    grant_test_permission(connection, edition, roots)
    editorial_approve(connection, edition)
    release = publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                              write_root=root, actor='publisher')
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    catalog = (output / 'catalog.json').read_bytes()
    changes = connection.total_changes
    monkeypatch.delenv('BILI_PUBLICATION_ENABLED')
    with pytest.raises(ContentRightsError, match='disabled by default'):
        publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                        write_root=root, actor='publisher')
    with pytest.raises(ContentRightsError, match='disabled by default'):
        export_publications(connection, artifact_roots=roots, output=output)
    assert connection.total_changes == changes
    assert (output / 'catalog.json').read_bytes() == catalog
    withdraw_release(connection, release_id=release['release_id'], actor='operator', note='Stop publication')


def test_authorized_publication_revocation_and_hold_remove_old_managed_files(manuscript):
    connection, edition, root, roots, output = manuscript
    grant = grant_test_permission(connection, edition, roots)
    editorial_approve(connection, edition)
    publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots, write_root=root, actor='publisher')
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    notices = json.loads((output / 'content-rights.json').read_text())
    assert notices['notices'][0]['license'] == 'CC-BY-SA-4.0'
    assert 'permission_reference' not in (output / 'content-rights.json').read_text()
    record_rights(connection, action='hold', video_part_id=edition['video_part_id'], actor='operator', note='Complaint')
    assert export_publications(connection, artifact_roots=roots, output=output) == 0
    assert {p.name for p in output.rglob('*') if p.is_file()} == {'catalog.json', 'publication-export-manifest.json'}
    record_rights(connection, action='clear', video_part_id=edition['video_part_id'], actor='operator', note='Resolved')
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    record_rights(connection, action='revoke', edition_id=edition['edition_id'], actor='operator', note='Stop distribution')
    assert export_publications(connection, artifact_roots=roots, output=output) == 0
    with pytest.raises(ContentRightsError, match='revoked'):
        publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots, write_root=root, actor='publisher')
    assert grant['permission']['permission_reference'] == 'test:own-work'


def test_edited_content_does_not_inherit_permission(manuscript):
    connection, edition, _root, roots, output = manuscript
    grant_test_permission(connection, edition, roots)
    changed = edit_edition(connection, edition_id=edition['edition_id'], markdown_text='Independent new edition.',
                           actor='editor', note='Changed text')
    assert rights_status(connection, changed) == {'status': 'unknown', 'public': False}
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 0
    grant_test_permission(connection, changed, roots)
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 1


def test_expiry_is_checked_each_export_and_source_reference_is_required(manuscript, monkeypatch):
    connection, edition, _root, roots, output = manuscript
    grant = grant_test_permission(connection, edition, roots)
    permission = {**grant['permission'], 'expires_at': int(time.time()) + 100}
    record_rights(connection, action='grant', edition_id=edition['edition_id'], permission=permission,
                  artifact_roots=roots, actor='operator', note='Temporary distribution')
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 1
    monkeypatch.setattr('bili_asr.content_rights.time.time', lambda: permission['expires_at'] + 1)
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 0


@pytest.mark.parametrize('field', ['public_distribution', 'adaptation', 'commercial_use', 'open_licensing', 'includes_source_transcript'])
def test_incomplete_scope_cannot_be_granted(manuscript, field):
    connection, edition, _root, roots, _output = manuscript
    grant = grant_test_permission(connection, edition, roots)
    permission = {**grant['permission'], field: False}
    with pytest.raises(ContentRightsError, match=field):
        record_rights(connection, action='grant', edition_id=edition['edition_id'], permission=permission,
                      artifact_roots=roots, actor='operator', note='Insufficient scope')


def test_rights_ledger_is_additive_idempotent_and_append_only(manuscript):
    connection, edition, _root, roots, _output = manuscript
    before = connection.execute('SELECT content_json FROM publication_editions').fetchall()
    initialize_rights(connection)
    initialize_rights(connection)
    assert connection.execute('SELECT content_json FROM publication_editions').fetchall() == before
    grant_test_permission(connection, edition, roots)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        connection.execute('DELETE FROM content_rights_events')
    connection.rollback()


def test_rights_cli_and_private_export_acknowledgment(manuscript, capsys):
    connection, edition, root, _roots, _output = manuscript
    connection.close()
    # Arguments belong to the selected nested action.
    assert cli.main(['publication', 'rights', 'init', '--archive-root', str(root)]) == 0
    capsys.readouterr()
    assert cli.main(['publication', 'rights', 'show', '--archive-root', str(root), '--edition-id', edition['edition_id'], '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'unknown'
    assert cli.main(['export', '--archive-root', str(root), '--format', 'json', '--with-text']) == 1
    assert '--private' in capsys.readouterr().err


@pytest.mark.parametrize('exporter', [export_publications, export_publication_drafts])
def test_public_export_rejects_snapshot_pinned_before_committed_hold(manuscript, exporter):
    connection, edition, root, roots, output = manuscript
    connection.execute('PRAGMA journal_mode=WAL')
    grant_test_permission(connection, edition, roots)
    reader = connect_database(root / 'archive.db', readonly=True, must_exist=True)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT COUNT(*) FROM content_rights_events').fetchone()
        record_rights(connection, action='hold', video_part_id=edition['video_part_id'],
                      actor='operator', note='Complaint')
        with pytest.raises(ContentRightsError, match='caller transaction'):
            exporter(reader, artifact_roots=roots, output=output)
        assert not output.exists()
        reader.rollback()
        assert exporter(reader, artifact_roots=roots, output=output) == 0
    finally:
        reader.close()


def test_held_release_is_removed_even_if_private_artifact_is_missing(manuscript):
    connection, edition, root, roots, output = manuscript
    grant_test_permission(connection, edition, roots)
    editorial_approve(connection, edition)
    release = publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                              write_root=root, actor='publisher')
    assert export_publications(connection, artifact_roots=roots, output=output) == 1
    record_rights(connection, action='hold', video_part_id=edition['video_part_id'],
                  actor='operator', note='Complaint')
    (root / release['relative_path']).unlink()
    assert export_publications(connection, artifact_roots=roots, output=output) == 0
    assert not list(output.rglob('*.md'))


def test_public_export_and_rights_writes_share_a_lock_beside_archive(manuscript):
    connection, edition, root, roots, output = manuscript
    grant_test_permission(connection, edition, roots)
    editorial_approve(connection, edition)
    release = publish_edition(connection, edition_id=edition['edition_id'], artifact_roots=roots,
                              write_root=root, actor='publisher')
    with distribution_lock(connection):
        assert (root.parent / f'.{root.name}.content-distribution.export.lock').is_file()
        with pytest.raises(ExportSnapshotError, match='another export'):
            export_publication_drafts(connection, artifact_roots=roots, output=output)
        with pytest.raises(ExportSnapshotError, match='another export'):
            record_rights(connection, action='hold', video_part_id=edition['video_part_id'],
                          actor='operator', note='Complaint')
        with pytest.raises(ExportSnapshotError, match='another export'):
            withdraw_release(connection, release_id=release['release_id'], actor='operator', note='Stop')
        with pytest.raises(ExportSnapshotError, match='another export'):
            edit_edition(connection, edition_id=edition['edition_id'], markdown_text='Changed draft',
                         actor='editor', note='Change')
    assert not list(root.glob('*.lock'))


@pytest.mark.parametrize('existing_output', [False, True])
def test_permission_expiring_during_staging_is_removed(manuscript, monkeypatch, existing_output):
    from bili_asr import export_snapshot
    connection, edition, _root, roots, output = manuscript
    grant = grant_test_permission(connection, edition, roots)
    expiry = int(time.time()) + 100
    permission = {**grant['permission'], 'expires_at': expiry}
    record_rights(connection, action='grant', edition_id=edition['edition_id'], permission=permission,
                  artifact_roots=roots, actor='operator', note='Temporary')
    if existing_output:
        assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 1
    real_write = export_snapshot._write_file
    def expire_after_write(path, data):
        real_write(path, data)
        monkeypatch.setattr('bili_asr.content_rights.time.time', lambda: expiry + 1)
    monkeypatch.setattr(export_snapshot, '_write_file', expire_after_write)
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 0
    assert not list(output.rglob('*.md'))


def test_rights_ledger_survives_database_snapshot_and_altered_schema_is_rejected(manuscript, tmp_path):
    connection, edition, root, roots, _output = manuscript
    original_contract = validate_snapshot_database(root / 'archive.db')
    grant_test_permission(connection, edition, roots)
    rights_contract = validate_snapshot_database(root / 'archive.db')
    assert rights_contract != original_contract
    target = tmp_path / 'restored.db'
    assert create_database_snapshot(root / 'archive.db', target) == rights_contract
    restored = connect_database(target, must_exist=True)
    try:
        assert rights_status(restored, edition)['public']
        restored.execute('DROP TRIGGER immutable_content_rights_update')
        restored.commit()
        with pytest.raises(SnapshotDatabaseError, match='missing immutable_content_rights_update'):
            validate_snapshot_database(target)
    finally:
        restored.close()


@pytest.mark.parametrize('field,value', [('license', 'unknown'), ('scope', ['citation']),
                                       ('attribution', ''), ('videoPartId', True),
                                       ('sourceUrl', 'https://example.test/other'), ('expiresAt', True)])
def test_public_notices_cannot_omit_or_misstate_scope_attribution_or_identity(manuscript, field, value):
    from bili_asr.export_snapshot import json_bytes, replace_snapshot
    connection, edition, _root, roots, output = manuscript
    grant_test_permission(connection, edition, roots)
    assert export_publication_drafts(connection, artifact_roots=roots, output=output) == 1
    files = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob('*')
             if p.is_file() and not p.name.endswith('-manifest.json')}
    notices = json.loads(files['content-rights.json'])
    notices['notices'][0][field] = value
    files['content-rights.json'] = json_bytes(notices)
    with pytest.raises(ExportSnapshotError, match='rights notice'):
        replace_snapshot(output.parent / 'invalid-notice', kind='publication-draft-export', files=files)
