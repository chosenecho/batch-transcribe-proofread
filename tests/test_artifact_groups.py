"""Whole text versions survive offload, replacement, and self-contained backup."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bili_asr.archive import archive_bundle_complete, write_archive
from bili_asr.artifacts import BUNDLE_MARKER_NAME
from bili_asr.services.archive_snapshot import (
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from bili_asr.services.artifact_groups import (
    capture_artifact_groups,
    restore_artifact_group,
)
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    inventory_artifacts,
    plan_artifact_offload,
)
from bili_asr.services.artifact_state import artifact_state
from tests.test_artifact_transfer import archive as audio_archive
from tests.test_artifact_transfer import domain_facts, transfer


def publish(archive, text="first", identity=1):
    segments = [{"start": 0.0, "end": 1.0, "text": text}]
    paths = write_archive(archive.root, entry={"bvid": "BVTEST", "cid": 1, "page_index": 0,
                          "work_id": "BVTEST:p0", "title": "part", "video_title": "video",
                          "duration_s": 1}, segments=segments, source="subtitle")
    with sqlite3.connect(archive.root / "archive.db") as connection:
        connection.execute("INSERT INTO transcripts VALUES (?,1,'subtitle-cc','zh',NULL,?,?,?)",
                           (identity, identity, hashlib.sha256(text.encode()).hexdigest(), identity))
        connection.execute("INSERT INTO transcript_segments VALUES (?,0,0,1000,?)", (identity, text))
        connection.execute("INSERT INTO workflow_publications VALUES (?,1,?,?,?)", (identity, identity, identity, json.dumps(paths)))
    return paths


@pytest.fixture
def archive(tmp_path):
    fixture = audio_archive.__wrapped__(tmp_path)
    fixture.paths = publish(fixture)
    return fixture


def text_plan(archive):
    return plan_artifact_offload(inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True,
        selection=ArtifactSelection(kinds=("bundle",)), external_holds={}), target_id="cold")


def test_complete_bundle_offload_restores_identical_group_and_preserves_domain(archive):
    original = {key: (archive.root / path).read_bytes() for key, path in archive.paths.items()}
    before = domain_facts(archive)
    captured = capture_artifact_groups(archive.roots)
    assert captured["blocked"] == []
    assert len(captured["groups"]) == 1
    frozen = text_plan(archive)
    assert len(frozen["items"]) == 12
    result = transfer(archive, frozen, mode="offload")
    assert result["released_copies"] == 12
    assert not archive_bundle_complete(archive.root, archive.paths)
    restored = restore_artifact_group(archive.roots, captured["groups"][0], storage_targets={"cold": archive.target})
    assert restored["members"] == 6
    assert archive_bundle_complete(archive.root, archive.paths)
    assert {key: (archive.root / path).read_bytes() for key, path in archive.paths.items()} == original
    assert domain_facts(archive) == before


def test_complete_snapshot_streams_offloaded_bundle_and_all_preserved_versions(archive, tmp_path):
    capture_artifact_groups(archive.roots)
    original = {key: (archive.root / path).read_bytes() for key, path in archive.paths.items()}
    publish(archive, "second", 2)
    latest = capture_artifact_groups(archive.roots)
    assert latest["blocked"] == []
    frozen = text_plan(archive)
    transfer(archive, frozen, mode="offload")
    snapshot = tmp_path / "full.zip"
    save_snapshot(archive.root, snapshot, storage_targets={"cold": archive.target})
    assert check_snapshot(snapshot)["valid"]
    assert not archive_bundle_complete(archive.root, archive.paths)
    archive.target.rename(tmp_path / "offline")
    restored = tmp_path / "restored"
    restore_snapshot(snapshot, restored)
    assert archive_bundle_complete(restored, archive.paths)
    for value in original.values():
        assert (restored / "documents/artifact-objects" / hashlib.sha256(value).hexdigest()).read_bytes() == value
    assert b"second" in (restored / archive.paths["raw_path"]).read_bytes()


@pytest.mark.parametrize("failure", ["missing", "mixed", "wrong_transcript"])
def test_unproven_bundle_never_becomes_releasable(archive, failure):
    if failure == "missing":
        (archive.root / archive.paths["vtt_path"]).unlink()
    elif failure == "mixed":
        (archive.root / archive.paths["txt_path"]).write_bytes(b"changed")
    else:
        with sqlite3.connect(archive.root / "archive.db") as connection:
            connection.execute("UPDATE transcript_segments SET text='different'")
    captured = capture_artifact_groups(archive.roots)
    assert captured["groups"] == []
    assert captured["blocked"]
    assert text_plan(archive)["items"] == []


def test_group_restore_never_overwrites_newer_publication(archive):
    group = capture_artifact_groups(archive.roots)["groups"][0]
    publish(archive, "second", 2)
    with pytest.raises(ValueError, match="different current version"):
        restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    assert archive_bundle_complete(archive.root, archive.paths)
    assert b"second" in (archive.root / archive.paths["raw_path"]).read_bytes()


def test_corrupt_external_member_never_installs_completion_marker(archive):
    group = capture_artifact_groups(archive.roots)["groups"][0]
    transfer(archive, text_plan(archive), mode="offload")
    for package in archive.target.rglob("*.zip"):
        package.write_bytes(b"broken")
    with pytest.raises(ValueError):
        restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    marker = archive.root / Path(archive.paths["srt_path"]).parent / BUNDLE_MARKER_NAME
    assert not marker.exists()
    assert not any((archive.root / path).exists() for path in archive.paths.values())


def test_file_appearing_during_group_retrieval_is_not_registered_or_marked_complete(archive, monkeypatch):
    from bili_asr.services.artifact_access import PackagedArtifact
    group = capture_artifact_groups(archive.roots)["groups"][0]
    transfer(archive, text_plan(archive), mode="offload")
    original = PackagedArtifact.copy_to
    changed = archive.root / archive.paths["txt_path"]
    def inject(packaged, destination):
        result = original(packaged, destination)
        changed.write_bytes(b"a different publication appeared during restore")
        return result
    monkeypatch.setattr(PackagedArtifact, "copy_to", inject)
    with pytest.raises(ValueError, match="different current version"):
        restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    assert changed.read_bytes() == b"a different publication appeared during restore"
    assert not (changed.parent / BUNDLE_MARKER_NAME).exists()
    with sqlite3.connect(archive.root / "archive.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_replicas WHERE relative_key=? AND presence='present'", (archive.paths["txt_path"],)).fetchone()[0] == 0


def test_offloaded_publication_stays_complete_and_reports_unavailable_storage(archive, tmp_path, monkeypatch):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.coverage_report import CoverageReport
    from bili_asr.contracts.json_schema import validate_json
    from bili_asr.integrity import IntegrityVerifier
    from bili_asr.services.artifact_access import PackagedArtifact
    from bili_asr.services.workflow_projection import workflow_records

    capture_artifact_groups(archive.roots)
    transfer(archive, text_plan(archive), mode="offload")
    archive.target.rename(tmp_path / "offline")
    before = domain_facts(archive)
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only reports must not retrieve payloads")
    monkeypatch.setattr(PackagedArtifact, "copy_to", forbidden)
    records = workflow_records(archive.root)
    entry = next(iter(records.values()))
    assert entry["status"] == "archived"
    assert entry["artifact_state"]["production"] == "published"
    assert entry["artifact_state"]["locality"] == "offloaded"
    assert entry["artifact_state"]["readiness"] == "restore_required"
    validate_json("artifact-publication-state-v1", entry["artifact_state"])
    assert CoverageReport.build(archive.root).data["cumulative"]["complete"] == 1
    assert IntegrityVerifier().verify(archive.root).defect_count == 0
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.READ) as session:
        report = artifact_state(session.connection, archive.roots, check_targets=True, storage_targets={"cold": archive.target})
    assert {obj["readiness"] for obj in report["objects"]} == {"storage_unavailable"}
    validate_json("artifact-state-v1", report)
    assert domain_facts(archive) == before


def test_ai_pair_and_every_release_status_offload_without_domain_changes(archive):
    from tests.test_artifact_inventory_service import digest, job, write

    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "proofread", kind="proofread", status="succeeded")
        connection.execute("INSERT INTO editorial_inputs VALUES ('input',1,1,NULL,'{}',1)")
        connection.execute("INSERT INTO editorial_revisions VALUES ('revision','input','proofread','[]','ai-unreviewed',1)")
        for name, role in (("ai-draft.md", "ai-draft"), ("review.md", "review-reference")):
            key = f"documents/part-1/revision/ai-draft-v1/{name}"
            connection.execute("INSERT INTO document_artifacts VALUES ('revision','ai-draft-v1',?,?,?,?)",
                               (name, role, key, digest(name.encode())))
            write(archive.root, key, name.encode())
        for index, status in enumerate(("published", "superseded", "withdrawn")):
            edition, review, release = f"edition-{index}", f"review-{index}", f"release-{index}"
            key = f"publications/part-1/{release}/publish-v1/publish.md"
            connection.execute("INSERT INTO publication_editions VALUES (?,1,'revision',NULL,'{}',?,1,'tester','')", (edition, "a" * 64))
            connection.execute("INSERT INTO publication_edition_reviews VALUES (?,?,?,'approved','tester','',NULL,1)", (edition, review, "a" * 64))
            connection.execute("INSERT INTO publication_releases VALUES (?,1,?,?,?,'publish-v1',?,?,1,'tester',?)",
                               (release, edition, review, "a" * 64, key, digest(release.encode()), status))
            write(archive.root, key, release.encode())
    captured = capture_artifact_groups(archive.roots)
    assert captured["blocked"] == []
    assert len(captured["groups"]) == 5
    before = domain_facts(archive)
    frozen = plan_artifact_offload(inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True,
        selection=ArtifactSelection(kinds=("document", "release")), external_holds={}), target_id="cold")
    assert len(frozen["items"]) == 10
    transfer(archive, frozen, mode="offload")
    with sqlite3.connect(archive.root / "archive.db") as connection:
        groups = connection.execute("SELECT group_id FROM artifact_groups WHERE owner_kind IN ('document','release')").fetchall()
        from bili_asr.services.artifact_readers import read_retained_text
        row = connection.execute("SELECT relative_path,content_sha256 FROM document_artifacts LIMIT 1").fetchone()
        with pytest.raises(ValueError, match="artifact_restore_required.*restore-group"):
            read_retained_text(connection, row[0], row[1], (archive.root,))
    for group, in groups:
        restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    assert domain_facts(archive) == before
    for item in frozen["items"]:
        if not item["path"].startswith("documents/artifact-objects/"):
            assert hashlib.sha256((archive.root / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_source_evidence_pair_is_preserved_and_restored_together(archive):
    from bili_asr.storage.archive_contracts import _resource
    from tests.test_artifact_inventory_service import digest, job, write

    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "proofread", kind="proofread", status="succeeded")
        connection.execute("INSERT INTO editorial_inputs VALUES ('input',1,1,NULL,'{}',1)")
        connection.execute("INSERT INTO editorial_revisions VALUES ('revision','input','proofread','[]','ai-unreviewed',1)")
        connection.commit()
        connection.executescript(_resource("schema-preserved-body-import.sql"))
        body, review = "documents/imports/fixture/body.md", "documents/imports/fixture/review.md"
        connection.execute("INSERT INTO manuscript_import_baselines VALUES (?,1,'revision',NULL,'{}',?,?,?,?,1)",
                           ("b" * 64, body, digest(b"body"), review, digest(b"review")))
    write(archive.root, body, b"body")
    write(archive.root, review, b"review")
    capture_artifact_groups(archive.roots)
    frozen = plan_artifact_offload(inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True,
        selection=ArtifactSelection(kinds=("source-evidence",)), external_holds={}), target_id="cold")
    assert len(frozen["items"]) == 4
    transfer(archive, frozen, mode="offload")
    with sqlite3.connect(archive.root / "archive.db") as connection:
        group = connection.execute("SELECT group_id FROM artifact_groups WHERE owner_kind='source-evidence'").fetchone()[0]
    restore_artifact_group(archive.roots, group, storage_targets={"cold": archive.target})
    assert (archive.root / body).read_bytes() == b"body"
    assert (archive.root / review).read_bytes() == b"review"
