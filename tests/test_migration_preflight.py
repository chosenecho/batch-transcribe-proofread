"""Offline migration inventory must verify all generations without source writes."""

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from bili_asr.archive_maintenance import archive_access
from bili_asr.publication import create_edition, edit_edition, review_edition, withdraw_release
from tests.support.legacy_publication import publish_legacy_edition
from bili_asr.services import migration_preflight as service
from bili_asr.services.migration_preflight import MigrationPreflightError, migration_preflight
from bili_asr.storage import database, snapshots
from tests.fixtures.migration_archive import build_migration_archive
from tests.test_archive_snapshot import _seed_archive, _seed_publication
from tests.test_publication import approve, seeded_publication


def test_inventory_preserves_two_root_precedence_and_records_exclusions(tmp_path, monkeypatch):
    root, products = tmp_path / "archive", tmp_path / "products"
    _seed_archive(root, products)
    (root / "audio").mkdir()
    (root / "audio/BVtest.m4a").write_bytes(b"shadowed legacy audio")
    (root / "audio/fallback.m4a").write_bytes(b"fallback")
    (root / ".env").write_text("secret-cookie-do-not-read", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs/private.log").write_text("not part of archive facts", encoding="utf-8")
    source_before = (root / "archive.db").read_bytes()
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == ".env" or "logs" in path.parts:
            raise AssertionError("excluded credentials and logs must not be read")
        return real_open(path, *args, **kwargs)

    def refuse_current_contract(*args, **kwargs):
        raise AssertionError("legacy inventory must not depend on the current schema")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(database, "initialize_schema", refuse_current_contract)
    monkeypatch.setattr(database, "open_database", refuse_current_contract)
    monkeypatch.setattr(snapshots, "required_artifacts", refuse_current_contract)
    report = migration_preflight(root, artifact_root=products)
    files = {item["path"]: item for item in report["files"]}
    assert report["valid"] and not report["conversion_performed"] and not report["target_created"]
    assert report["totals"]["file_count"] == 5
    assert files["audio/BVtest.m4a"]["base"] == str(products)
    assert files["audio/fallback.m4a"]["base"] == str(root)
    assert report["shadowed"] == [{
        "path": "audio/BVtest.m4a", "base": str(root), "size": 21,
        "sha256": hashlib.sha256(b"shadowed legacy audio").hexdigest(), "selected_base": str(products),
    }]
    assert {item["category"] for item in report["excluded"]} == {"credentials", "operational-logs"}
    assert "secret-cookie" not in json.dumps(report)
    assert (root / "archive.db").read_bytes() == source_before
    assert migration_preflight(root, artifact_root=products)["source"]["fingerprint"] == report["source"]["fingerprint"]


@pytest.mark.parametrize("change,match", [
    ("missing", "missing legacy artifact"), ("corrupt", "hash or size mismatch"),
    ("wrong_size", "hash or size mismatch"),
])
def test_all_registered_audio_is_required(tmp_path, change, match):
    root = tmp_path / "archive"
    audio = _seed_archive(root)
    if change == "missing":
        audio.unlink()
    elif change == "corrupt":
        audio.write_bytes(b"bad")
    else:
        with sqlite3.connect(root / "archive.db") as connection:
            connection.execute("UPDATE audio_objects SET byte_size=99")
    with pytest.raises(MigrationPreflightError, match=match):
        migration_preflight(root)


def test_successful_audio_attempt_dedup_reference_is_required(tmp_path):
    root = tmp_path / "archive"
    _seed_archive(root)
    with sqlite3.connect(root / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='succeeded'")
        connection.execute(
            "INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,outcome,result_json) "
            "VALUES ('attempt','queued','worker',1,2,'succeeded',?)",
            (json.dumps({"storage_key": "audio/dedup.m4a", "sha256": "a" * 64}),),
        )
    with pytest.raises(MigrationPreflightError, match="audio/dedup"):
        migration_preflight(root)


@pytest.mark.parametrize("damage", ["missing_marker", "bad_hash", "old_four_files"])
def test_workflow_bundle_requires_complete_marker_and_matching_bytes(tmp_path, damage):
    root = tmp_path / "archive"
    _seed_archive(root)
    marker = _seed_publication(root)
    if damage == "missing_marker":
        marker.unlink()
    elif damage == "bad_hash":
        (marker.parent / "bundle.raw.json").write_text("changed", encoding="utf-8")
    else:
        with sqlite3.connect(root / "archive.db") as connection:
            raw = connection.execute("SELECT artifact_json FROM workflow_publications").fetchone()[0]
            value = json.loads(raw)
            value.pop("raw_path")
            connection.execute("UPDATE workflow_publications SET artifact_json=?", (json.dumps(value),))
    with pytest.raises(MigrationPreflightError, match="marker|five-file|bundle-ready"):
        migration_preflight(root)


def _historical_archive(root):
    connection, revision, roots = seeded_publication(root)
    first = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")
    approve(connection, first, authorize=False)
    released = publish_legacy_edition(connection, edition_id=first["edition_id"], artifact_roots=roots,
                               write_root=root, actor="publisher")
    second = edit_edition(connection, edition_id=first["edition_id"], markdown_text="second", actor="editor", note="edit")
    approve(connection, second, authorize=False)
    newer = publish_legacy_edition(connection, edition_id=second["edition_id"], artifact_roots=roots,
                            write_root=root, actor="publisher", expected_release_id=released["release_id"])
    withdraw_release(connection, release_id=newer["release_id"], actor="publisher", note="withdraw")
    connection.close()
    return released, newer


@pytest.mark.parametrize("generation", [0, 1])
def test_superseded_and_withdrawn_release_files_are_both_required(tmp_path, generation):
    root = tmp_path / "archive"
    releases = _historical_archive(root)
    report = migration_preflight(root)
    assert all(release["relative_path"] in {item["path"] for item in report["files"]} for release in releases)
    (root / releases[generation]["relative_path"]).unlink()
    with pytest.raises(MigrationPreflightError, match="missing legacy artifact"):
        migration_preflight(root)


@pytest.mark.parametrize("missing", ["one", "all_used"])
def test_ai_documents_require_paired_registration_and_hashes(tmp_path, missing):
    root = tmp_path / "archive"
    connection, revision, roots = seeded_publication(root)
    if missing == "one":
        connection.execute("DELETE FROM document_artifacts WHERE artifact_name='review.md'")
    else:
        create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")
        connection.execute("DELETE FROM document_artifacts")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationPreflightError, match="complete paired"):
        migration_preflight(root)


def _tamper(root, sql):
    with sqlite3.connect(root / "archive.db") as connection:
        triggers = connection.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger'").fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(sql)
        for _, definition in triggers:
            connection.execute(definition)


@pytest.mark.parametrize("sql,match", [
    ("UPDATE publication_releases SET content_sha256='" + "a" * 64 + "'", "approved edition"),
    ("UPDATE publication_releases SET relative_path='publications/wrong.md' WHERE status='withdrawn'", "approved edition"),
    ("UPDATE publication_heads SET current_release_id=(SELECT release_id FROM publication_releases WHERE status='withdrawn')", "head identity"),
    ("UPDATE publication_editions SET content_json=replace(content_json,'second','tampered')", "frozen content"),
    ("DELETE FROM publication_events WHERE event_type='withdrawn'", "audit history"),
    ("UPDATE publication_releases SET published_by='different-actor'", "audit identity"),
])
def test_frozen_publication_binding_tampering_is_rejected(tmp_path, sql, match):
    root = tmp_path / "archive"
    _historical_archive(root)
    _tamper(root, sql)
    with pytest.raises(MigrationPreflightError, match=match):
        migration_preflight(root)


@pytest.mark.parametrize("statement", [
    "UPDATE editorial_inputs SET prepared_json='{}'",
    "UPDATE editorial_revisions SET blocks_json='{}'",
    "UPDATE publication_editions SET content_json='{}'",
])
def test_malformed_frozen_shapes_have_bounded_content_free_errors(tmp_path, statement):
    root = tmp_path / "archive"
    _historical_archive(root)
    _tamper(root, statement)
    with pytest.raises(MigrationPreflightError, match="frozen|revision identity"):
        migration_preflight(root)


@pytest.mark.parametrize("statement", [
    "DELETE FROM publication_events WHERE event_type='created'",
    "DELETE FROM publication_events WHERE event_type='in-review'",
    "UPDATE publication_events SET from_status='rejected' WHERE event_type='in-review'",
    "UPDATE publication_events SET to_status='approved' WHERE event_type='in-review'",
    "UPDATE publication_events SET review_id=(SELECT review_id FROM publication_edition_reviews "
    "WHERE review_id!=publication_events.review_id LIMIT 1) WHERE event_type='in-review'",
    "UPDATE publication_events SET content_sha256='" + "a" * 64 + "' WHERE event_type='in-review'",
    "UPDATE publication_events SET actor='wrong-creator' WHERE event_type='created'",
])
def test_earlier_review_audit_damage_is_rejected_even_with_valid_final_approval(tmp_path, statement):
    root = tmp_path / "archive"
    _historical_archive(root)
    _tamper(root, statement)
    with pytest.raises(MigrationPreflightError, match="review.*audit"):
        migration_preflight(root)


def test_edited_pending_review_and_repeated_review_cycle_are_valid(tmp_path):
    root = tmp_path / "archive"
    connection, revision, roots = seeded_publication(root)
    first = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor", note="creation note")
    second = edit_edition(connection, edition_id=first["edition_id"], markdown_text="second", actor="editor", note="edit note")
    connection.close()
    assert migration_preflight(root)["valid"]
    connection = database.open_database(root)
    for old, new in [("pending-review", "in-review"), ("in-review", "changes-requested"),
                     ("changes-requested", "in-review"), ("in-review", "approved")]:
        review_edition(connection, edition_id=second["edition_id"], status=new,
                       content_sha256=second["content_sha256"], expected_status=old,
                       actor="reviewer", note="review note", issue_url="https://example.test/review")
    connection.close()
    assert migration_preflight(root)["valid"]


@pytest.mark.parametrize("statement", [
    "UPDATE editorial_inputs SET prepared_json='{}'",
    "UPDATE editorial_inputs SET prepared_json=json_set(prepared_json,'$.snapshot.metadata.title','changed')",
    "UPDATE editorial_revisions SET blocks_json='[]'",
    "DELETE FROM editorial_job_inputs",
    "UPDATE workflow_jobs SET kind='index' WHERE kind='proofread'",
])
def test_unrendered_revision_still_requires_valid_frozen_identity_and_job_binding(tmp_path, statement):
    root = tmp_path / "archive"
    connection, _, _ = seeded_publication(root)
    connection.execute("DELETE FROM document_artifacts")
    connection.commit()
    connection.close()
    _tamper(root, statement)
    with pytest.raises(MigrationPreflightError, match="frozen"):
        migration_preflight(root)


def test_valid_committed_revision_without_registered_artifacts_is_allowed(tmp_path):
    root = tmp_path / "archive"
    connection, _, _ = seeded_publication(root)
    connection.execute("DELETE FROM document_artifacts")
    connection.commit()
    connection.close()
    assert migration_preflight(root)["valid"]


@pytest.mark.parametrize("separate_artifacts", [False, True])
def test_complete_preservation_baseline_passes_preflight(tmp_path, separate_artifacts):
    fixture = build_migration_archive(tmp_path / "source", tmp_path / "products" if separate_artifacts else None)
    assert migration_preflight(fixture.archive_root, artifact_root=fixture.artifact_roots[0])["valid"]


def test_frozen_input_without_revision_is_checked(tmp_path):
    root = tmp_path / "archive"
    connection, _, _ = seeded_publication(root)
    connection.execute("DELETE FROM document_artifacts")
    connection.execute("DELETE FROM editorial_revisions")
    connection.commit()
    connection.close()
    assert migration_preflight(root)["valid"]
    _tamper(root, "UPDATE editorial_inputs SET prepared_json='{}'")
    with pytest.raises(MigrationPreflightError, match="frozen"):
        migration_preflight(root)


def test_bad_database_reference_path_is_not_echoed_in_error(tmp_path):
    root = tmp_path / "archive"
    _seed_archive(root)
    with sqlite3.connect(root / "archive.db") as connection:
        connection.execute("UPDATE audio_objects SET storage_key='https://host/audio?token=private-value'")
    with pytest.raises(MigrationPreflightError, match="unsafe legacy artifact reference") as caught:
        migration_preflight(root)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("nested", ["products", "nested/products"])
def test_explicit_nested_artifact_root_preserves_fallback_and_rejects_unknown_siblings(tmp_path, nested):
    root = tmp_path / "archive"
    products = root / nested
    _seed_archive(root, products)
    (root / "audio").mkdir()
    (root / "audio/fallback.m4a").write_bytes(b"old")
    report = migration_preflight(root, artifact_root=products)
    assert report["totals"]["file_count"] == 5
    sibling = products.parent / "unknown.txt"
    sibling.write_bytes(b"unknown")
    with pytest.raises(MigrationPreflightError, match="unsupported source root entry"):
        migration_preflight(root, artifact_root=products)


def test_nested_artifact_root_in_managed_directory_is_explicitly_refused(tmp_path):
    root = tmp_path / "archive"
    products = root / "audio/products"
    _seed_archive(root, products)
    with pytest.raises(MigrationPreflightError, match="overlap managed"):
        migration_preflight(root, artifact_root=products)


@pytest.mark.parametrize("relative", ["unknown.json", "audio/.workflow-audio-job/part.m4a", "audio/CON.m4a"])
def test_unknown_paths_and_unfinished_stages_are_refused(tmp_path, relative):
    root = tmp_path / "archive"
    _seed_archive(root)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == "CON.m4a":
        # Reserved names cannot be created on Windows; inject into inventory instead.
        if __import__("os").name == "nt":
            pytest.skip("Windows itself prevents creation of CON.m4a")
    path.write_bytes(b"not a secret")
    with pytest.raises(MigrationPreflightError, match="unsupported|temporary|non-portable"):
        migration_preflight(root)


def test_case_collision_across_roots_is_rejected(tmp_path):
    root, products = tmp_path / "archive", tmp_path / "products"
    _seed_archive(root, products)
    (root / "audio").mkdir()
    (root / "audio/bvtest.m4a").write_bytes(b"different case")
    with pytest.raises(MigrationPreflightError, match="colliding|collision"):
        migration_preflight(root, artifact_root=products)


def test_existing_writer_lock_blocks_preflight(tmp_path):
    root = tmp_path / "archive"
    _seed_archive(root)
    with archive_access(root):
        with pytest.raises(MigrationPreflightError, match="archive_busy"):
            migration_preflight(root)


@pytest.mark.parametrize("change", ["new_file", "modify_read_file", "new_wal", "database_write"])
def test_source_changes_during_file_scan_are_rejected(tmp_path, monkeypatch, change):
    root = tmp_path / "archive"
    audio = _seed_archive(root)
    original = service._hash_file
    changed = False

    def changing_hash(path):
        nonlocal changed
        result = original(path)
        if path == audio and not changed:
            changed = True
            if change == "new_file":
                (root / "audio/new.m4a").write_bytes(b"new")
            elif change == "modify_read_file":
                audio.write_bytes(b"changed after hash")
            elif change == "new_wal":
                (root / "archive.db-wal").write_bytes(b"pending WAL")
            else:
                with sqlite3.connect(root / "archive.db") as connection:
                    connection.execute("UPDATE videos SET title='changed' ")
        return result

    monkeypatch.setattr(service, "_hash_file", changing_hash)
    with pytest.raises(MigrationPreflightError, match="source changed|WAL"):
        migration_preflight(root)


def test_database_and_roots_cannot_be_links(tmp_path):
    root = tmp_path / "archive"
    _seed_archive(root)
    original = root / "original.db"
    (root / "archive.db").rename(original)
    try:
        (root / "archive.db").symlink_to(original)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(MigrationPreflightError, match="symlink|regular file"):
        migration_preflight(root)


@pytest.mark.parametrize("parameter", ["source_root", "artifact_root"])
def test_link_parent_is_rejected_before_dotdot_normalization(tmp_path, parameter):
    root = tmp_path / "archive"
    _seed_archive(root)
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    link = tmp_path / "linked-parent"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    requested = link / ".." / "archive"
    with pytest.raises(MigrationPreflightError, match="symlink|junction"):
        if parameter == "source_root":
            migration_preflight(requested)
        else:
            migration_preflight(root, artifact_root=requested)
