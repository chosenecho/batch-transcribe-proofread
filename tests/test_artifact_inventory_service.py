"""Offline inventory evidence: no fixture is a production corpus."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import artifact_inventory_service as service
from bili_asr.services.artifact_inventory_service import (
    ArtifactInventoryServiceError,
    ArtifactSelection,
    inventory_artifacts,
    plan_artifact_offload,
    validate_offload_plan,
)
from bili_asr.storage.database import initialize_schema


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    database = root / "archive.db"
    connection = sqlite3.connect(database)
    initialize_schema(connection)
    connection.execute("INSERT INTO bilibili_users VALUES (1,'creator',1,1)")
    connection.execute("INSERT INTO videos VALUES ('BVTEST',1,1,'video',1,1,1)")
    connection.execute("INSERT INTO video_parts VALUES (1,'BVTEST',0,1,'part',1000,'discovered',1,1)")
    connection.execute("INSERT INTO video_parts VALUES (2,'BVTEST',1,2,'part2',1000,'discovered',1,1)")
    connection.commit()
    yield root, database, connection
    connection.close()


def write(root, key, content=b"sound"):
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def audio(connection, key="audio/test.m4a", content=b"sound", part=1):
    cursor = connection.execute("INSERT INTO audio_objects VALUES (NULL,?,?, 'm4a',1000,?,1)",
                                (digest(content), len(content), key))
    connection.execute("INSERT INTO part_audio_objects VALUES (?,?,1,'test')", (part, cursor.lastrowid))
    connection.commit()
    return cursor.lastrowid


def job(connection, identity, *, kind="asr", status="queued", profile=1, part=1):
    if kind == "asr":
        connection.execute("INSERT OR IGNORE INTO workflow_asr_profiles VALUES (?,?,'model','revision','aligner','cpu',NULL,?,1)",
                           (profile, str(profile), str(profile) * 64))
    connection.execute(
        "INSERT INTO workflow_jobs(job_id,kind,video_part_id,profile_id,dedupe_key,payload_json,status,available_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'{}',?,1,1,1)", (identity, kind, part, profile if kind == "asr" else None, identity, status))
    connection.commit()


def attempt(connection, identity, job_id, *, key="audio/attempt.m4a", content=b"sound", outcome="succeeded"):
    connection.execute("INSERT INTO workflow_attempts VALUES (?,?, 'worker',1,?,?,NULL,?)",
                       (identity, job_id, None if outcome == "running" else 2, outcome,
                        json.dumps({"storage_key": key, "sha256": digest(content)})))
    connection.commit()


def scan(archive, **options):
    root, database, _ = archive
    return inventory_artifacts(database, ArtifactRoots.of(root), **options)


def test_catalog_and_attempts_enumerate_every_root_without_space_dedup_by_digest(archive, tmp_path):
    root, database, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    external = tmp_path / "artifacts"
    write(external, "audio/test.m4a")
    job(connection, "download", kind="audio", status="succeeded")
    attempt(connection, "download-ok", "download")
    write(root, "audio/attempt.m4a")
    inventory = inventory_artifacts(database, ArtifactRoots.of(root, external), deep=True, external_holds={})
    assert len(inventory["objects"]) == 1
    assert len(inventory["objects"][0]["references"]) == 2
    assert inventory["summary"]["physical_content_bytes"] == 15
    assert inventory["summary"]["logical_bytes"] == 5
    assert inventory["summary"]["reclaimable_content_bytes"] == 15
    assert inventory["summary"]["target_payload_bytes"] == 5
    existing = [copy for copy in inventory["copies"] if copy["state"] == "verified"]
    assert len(existing) == 3
    assert any(copy["state"] == "missing" and copy["root"] == str(external) for copy in inventory["copies"])


def test_quick_scan_never_reads_payload_and_cannot_freeze(archive, monkeypatch):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    original = service.os.fdopen

    class NoRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, *args):
            raise AssertionError("quick inventory must not read payloads")

    monkeypatch.setattr(service.os, "fdopen", lambda *args: NoRead(original(*args)))
    inventory = scan(archive, external_holds={})
    assert inventory["summary"]["files_hashed"] == 0
    assert inventory["copies"][0]["state"] == "observed"
    assert "deep_verification_required" in inventory["copies"][0]["blockers"]
    with pytest.raises(ArtifactInventoryServiceError, match="deep"):
        plan_artifact_offload(inventory, target_id="external-disk")


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_all_unfinished_profiles_hold_shared_audio(archive, status):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    job(connection, "finished", profile=1, status="succeeded")
    job(connection, "other-profile", profile=2, status=status)
    inventory = scan(archive, deep=True, external_holds={})
    copy = inventory["copies"][0]
    assert f"consumer:other-profile:asr:{status}" in copy["blockers"]
    assert not copy["candidate"]
    assert inventory["summary"]["reclaimable_content_bytes"] == 0


def test_external_hold_unknown_verified_empty_and_explicit_hold_are_distinct(archive):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    unknown = scan(archive, deep=True)
    assert "external_hold_unverified" in unknown["copies"][0]["blockers"]
    checked = scan(archive, deep=True, external_holds={})
    assert checked["copies"][0]["candidate"]
    held = scan(archive, deep=True, external_holds={"part:1": ("investigation",)})
    assert "external_hold:part:1:investigation" in held["copies"][0]["blockers"]
    assert not held["copies"][0]["candidate"]


def test_dependency_retains_shared_input_outside_selection(archive):
    root, _, connection = archive
    identity = audio(connection)
    connection.execute("INSERT INTO part_audio_objects VALUES (2,?,1,'test')", (identity,))
    connection.commit()
    write(root, "audio/test.m4a")
    job(connection, "other-part", part=2, status="failed")
    inventory = scan(archive, deep=True, external_holds={}, selection=ArtifactSelection(part_ids=(1,)))
    assert inventory["copies"][0]["selected"]
    assert "consumer:other-part:asr:failed" in inventory["copies"][0]["blockers"]


def test_missing_unreadable_and_unavailable_device_are_different(archive, monkeypatch):
    root, _, connection = archive
    audio(connection)
    assert scan(archive, deep=True)["copies"][0]["state"] == "missing"
    target = write(root, "audio/test.m4a")
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    assert scan(archive, deep=True)["copies"][0]["state"] == "unreadable"

    def disconnected(path, *args, **kwargs):
        if path == target:
            raise OSError(errno.EIO, "device unavailable", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", disconnected)
    assert scan(archive, deep=True)["copies"][0]["state"] == "storage_unavailable"


def test_hash_corruption_and_same_path_historical_conflict_hold_every_version(archive):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a", b"wrong")
    inventory = scan(archive, deep=True, external_holds={})
    assert "digest_mismatch" in inventory["copies"][0]["issues"]
    job(connection, "audio", kind="audio", status="succeeded")
    attempt(connection, "old-generation", "audio", key="audio/test.m4a", content=b"other")
    conflict = scan(archive, deep=True, external_holds={})
    assert "historical_identity_conflict" in conflict["copies"][0]["issues"]
    assert len(conflict["objects"]) == 2
    assert not plan_artifact_offload(conflict, target_id="disk")["items"]


def test_unregistered_and_staging_paths_never_become_cleanup_candidates(archive):
    root, _, _ = archive
    write(root, "audio/unregistered.m4a")
    write(root, "audio/.audio-stage-fixture/temp.m4a")
    write(root, "unknown/leave.txt")
    inventory = scan(archive, deep=True, external_holds={})
    assert not any(copy["candidate"] for copy in inventory["copies"])
    assert all("unregistered" in copy["issues"] for copy in inventory["copies"])
    assert any("staging_artifact" in copy["issues"] for copy in inventory["copies"])
    assert any(item["state"] == "outside_scope" and item["path"] == "unknown" for item in inventory["diagnostics"])


def test_hardlink_physical_space_is_counted_once_and_unknown_alias_blocks_release(archive, tmp_path):
    root, _, connection = archive
    audio(connection)
    source = write(root, "audio/test.m4a")
    duplicate = root / "audio/duplicate.m4a"
    try:
        os.link(source, duplicate)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    job(connection, "audio", kind="audio", status="succeeded")
    attempt(connection, "duplicate", "audio", key="audio/duplicate.m4a")
    inventory = scan(archive, deep=True, external_holds={})
    assert inventory["summary"]["path_bytes"] == 10
    assert inventory["summary"]["physical_content_bytes"] == 5
    assert inventory["summary"]["reclaimable_content_bytes"] == 5
    os.link(source, tmp_path / "outside.m4a")
    held = scan(archive, deep=True, external_holds={})
    assert held["summary"]["reclaimable_content_bytes"] == 0
    assert all("hardlink_aliases_outside_inventory" in copy["blockers"] for copy in held["copies"])


def test_unavailable_configured_root_does_not_hide_archive_fallback(archive, tmp_path):
    root, database, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    inventory = inventory_artifacts(database, ArtifactRoots.of(root, tmp_path / "unmounted"), deep=True, external_holds={})
    assert inventory["roots"][0]["state"] == "unavailable"
    assert inventory["copies"][0]["state"] == "root_unavailable"
    assert inventory["copies"][1]["candidate"]
    assert inventory["summary"]["reclaimable_content_bytes"] == 5


def test_mutable_bundle_generations_are_reported_without_inventing_historic_bytes(archive):
    root, _, connection = archive
    paths = {key: f"transcripts/BVTEST.p0/{name}" for key, name in
             {"srt_path": "bundle.srt", "vtt_path": "bundle.vtt", "txt_path": "bundle.txt", "md_path": "bundle.md", "raw_path": "bundle.raw.json"}.items()}
    for transcript in (1, 2):
        connection.execute("INSERT INTO transcripts VALUES (?,1,'subtitle-cc','zh',NULL,?,?,1)", (transcript, transcript, str(transcript) * 64))
        connection.execute("INSERT INTO workflow_publications VALUES (?,1,?,1,?)", (transcript, transcript, json.dumps(paths)))
    connection.commit()
    for key in paths.values():
        write(root, key, b"current generation")
    write(root, "transcripts/BVTEST.p0/.bundle-ready", b"marker")
    inventory = scan(archive, deep=True, external_holds={})
    assert len(inventory["objects"]) == 12
    assert len(inventory["copies"]) == 6
    assert all("mutable_bundle_version_unverified" in obj["retention_reasons"] for obj in inventory["objects"])
    assert all(not copy["candidate"] for copy in inventory["copies"])


def test_readonly_scans_and_plans_preserve_database_files_jobs_attempts(archive):
    root, database, connection = archive
    audio(connection)
    source = write(root, "audio/test.m4a")
    job(connection, "audio", kind="audio", status="succeeded")
    attempt(connection, "success", "audio", key="audio/test.m4a")
    before = database.read_bytes()
    progress = []
    first = scan(archive, deep=True, external_holds={}, progress=progress.append, max_bytes_per_second=1024 * 1024)
    second = scan(archive, deep=True, external_holds={})
    assert first == second
    assert progress and progress[0]["bytes_read"] == 5
    assert database.read_bytes() == before
    assert source.read_bytes() == b"sound"
    plan = plan_artifact_offload(first, target_id="disk:backup")
    assert plan == plan_artifact_offload(second, target_id="disk:backup")
    assert plan["items"][0]["sha256"] == digest(b"sound")
    assert plan["items"][0]["versions"] == ["attempt:success", "audio:1"]
    validate_offload_plan(json.loads(json.dumps(plan)))


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(target_id="changed"),
    lambda value: value["items"][0].update(sha256=None),
    lambda value: value["items"][0].update(path="../outside"),
    lambda value: value["items"][0].update(size=-1),
    lambda value: value["items"].append(deepcopy(value["items"][0])),
    lambda value: value.update(external_holds_verified=False),
])
def test_frozen_plan_rejects_tampering_and_incomplete_identity(archive, mutate):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    plan = plan_artifact_offload(scan(archive, deep=True, external_holds={}), target_id="disk")
    mutate(plan)
    with pytest.raises(ArtifactInventoryServiceError):
        validate_offload_plan(plan)


def test_plan_request_rejects_invalid_hold_or_rate_contract(archive):
    with pytest.raises(ArtifactInventoryServiceError, match="positive"):
        scan(archive, max_bytes_per_second=0)
    with pytest.raises(ArtifactInventoryServiceError, match="holds"):
        scan(archive, external_holds={"part:1": "not a reason list"})


def test_documents_and_every_release_keep_their_version_and_group(archive):
    root, _, connection = archive
    job(connection, "proofread", kind="proofread", status="succeeded")
    connection.execute("INSERT INTO transcripts VALUES (1,1,'subtitle-cc','zh',NULL,1,?,1)", ("a" * 64,))
    connection.execute("INSERT INTO editorial_inputs VALUES ('input',1,1,NULL,'{}',1)")
    connection.execute("INSERT INTO editorial_revisions VALUES ('revision','input','proofread','[]','ai-unreviewed',1)")
    for name, role in (("ai-draft.md", "ai-draft"), ("review.md", "review-reference")):
        key = f"documents/part-1/revision/ai-draft-v1/{name}"
        connection.execute("INSERT INTO document_artifacts VALUES ('revision','ai-draft-v1',?,?,?,?)",
                           (name, role, key, digest(name.encode())))
        write(root, key, name.encode())
    for index, status in enumerate(("published", "superseded", "withdrawn")):
        edition = f"edition-{index}"
        review = f"review-{index}"
        release = f"release-{index}"
        key = f"publications/part-1/{release}/publish-v1/publish.md"
        connection.execute("INSERT INTO publication_editions VALUES (?,1,'revision',NULL,'{}',?,1,'tester','')", (edition, "a" * 64))
        connection.execute("INSERT INTO publication_edition_reviews VALUES (?,?,?,'approved','tester','',NULL,1)", (edition, review, "a" * 64))
        connection.execute("INSERT INTO publication_releases VALUES (?,1,?,?,?,'publish-v1',?,?,1,'tester',?)",
                           (release, edition, review, "a" * 64, key, digest(release.encode()), status))
        if index != 2:
            write(root, key, release.encode())
    connection.commit()
    inventory = scan(archive, deep=True, external_holds={})
    assert len([obj for obj in inventory["objects"] if obj["references"][0]["kind"] == "release"]) == 3
    missing_release = next(copy for copy in inventory["copies"] if "release-2" in copy["path"])
    assert missing_release["state"] == "missing"
    assert all(copy["candidate"] for copy in inventory["copies"] if copy["path"].startswith("documents/"))
    (root / "documents/part-1/revision/ai-draft-v1/review.md").unlink()
    incomplete = scan(archive, deep=True, external_holds={})
    assert all("complete_group_unverified" in copy["blockers"] for copy in incomplete["copies"] if copy["path"].startswith("documents/"))


def test_universal_source_filters_do_not_assume_bvid(tmp_path):
    from bili_asr.storage.archive_contracts import bootstrap_contract

    root = tmp_path / "universal"
    root.mkdir()
    database = root / "archive.db"
    with sqlite3.connect(database) as connection:
        bootstrap_contract(connection)
        connection.execute("INSERT INTO source_creators VALUES (1,'youtube','creator-X','creator',1,1)")
        connection.execute("INSERT INTO source_videos VALUES (1,'youtube','video-X',1,'video',NULL,NULL,'https://example.test',1,1,1)")
        connection.execute("INSERT INTO video_parts VALUES (1,NULL,0,NULL,'part',1000,'discovered',1,1,1)")
        audio(connection, key="audio/youtube.video-X.p0.m4a")
    write(root, "audio/youtube.video-X.p0.m4a")
    inventory = inventory_artifacts(database, ArtifactRoots.of(root), deep=True, external_holds={},
                                    selection=ArtifactSelection(platforms=("youtube",), video_ids=("video-X",), creator_ids=("creator-X",)))
    assert inventory["database_contract"] == "universal-v2"
    assert inventory["copies"][0]["candidate"]
    assert inventory["objects"][0]["references"][0]["source"]["bvid"] is None


def test_deep_verification_detects_change_and_reports_bounded_progress(archive):
    root, _, connection = archive
    content = b"a" * (service._CHUNK + 7)
    audio(connection, content=content)
    target = write(root, "audio/test.m4a", content)
    calls = []

    def changed(event):
        calls.append(event)
        if len(calls) == 1:
            target.write_bytes(b"replacement")

    inventory = scan(archive, deep=True, external_holds={}, progress=changed)
    assert calls[0]["bytes_read"] == service._CHUNK
    assert inventory["copies"][0]["state"] == "changed_during_scan"
    assert not inventory["copies"][0]["candidate"]


def test_unknown_bundle_marker_format_is_named(archive):
    root, _, _ = archive
    write(root, "transcripts/fixture/.bundle-ready", b'{"schema":"new-unsupported-format"}')
    inventory = scan(archive, deep=True, external_holds={})
    assert "unknown_format" in inventory["copies"][0]["issues"]


def test_portable_prefix_collisions_block_sources_even_on_case_sensitive_hosts(archive):
    root, _, connection = archive
    audio(connection, key="audio/Cases/test.m4a")
    write(root, "audio/Cases/test.m4a")
    write(root, "audio/cases/other.m4a", b"other")
    inventory = scan(archive, deep=True, external_holds={})
    named = next(copy for copy in inventory["copies"] if copy["path"] == "audio/Cases/test.m4a")
    # A case-insensitive volume may have only the catalog spelling in both
    # observed paths; its identity is already portable, not a collision.
    observed_spelling = {copy["path"] for copy in inventory["copies"]}
    if "audio/cases/other.m4a" in observed_spelling:
        assert "path_collision" in named["blockers"]
        assert not named["candidate"]


def test_source_import_evidence_is_enumerated_as_a_complete_pair(archive):
    from bili_asr.storage.archive_contracts import _resource

    root, _, connection = archive
    job(connection, "proofread", kind="proofread", status="succeeded")
    connection.execute("INSERT INTO transcripts VALUES (1,1,'subtitle-cc','zh',NULL,1,?,1)", ("a" * 64,))
    connection.execute("INSERT INTO editorial_inputs VALUES ('input',1,1,NULL,'{}',1)")
    connection.execute("INSERT INTO editorial_revisions VALUES ('revision','input','proofread','[]','ai-unreviewed',1)")
    connection.commit()
    connection.executescript(_resource("schema-preserved-body-import.sql"))
    body, review = "documents/imports/fixture/body.md", "documents/imports/fixture/review.md"
    connection.execute("INSERT INTO manuscript_import_baselines VALUES (?,1,'revision',NULL,'{}',?,?,?,?,1)",
                       ("b" * 64, body, digest(b"body"), review, digest(b"review")))
    connection.commit()
    write(root, body, b"body")
    write(root, review, b"review")
    inventory = scan(archive, deep=True, external_holds={}, selection=ArtifactSelection(kinds=("source-evidence",)))
    assert len(inventory["objects"]) == 2
    assert all(copy["candidate"] for copy in inventory["copies"])
    (root / review).unlink()
    held = scan(archive, deep=True, external_holds={})
    assert all("complete_group_unverified" in copy["blockers"] for copy in held["copies"])


def test_invalid_selection_and_space_values_rejected_even_when_digest_recomputed(archive):
    root, _, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    plan = plan_artifact_offload(scan(archive, deep=True, external_holds={}), target_id="disk")
    for mutate in (lambda item: item["selection"].update(part_ids=[True]),
                   lambda item: item["estimate"].update(target_payload_bytes=-1),
                   lambda item: item["items"][0].update(object_ids=["sha256:" + "a" * 64])):
        invalid = deepcopy(plan)
        mutate(invalid)
        invalid["plan_sha256"] = service._sha({key: value for key, value in invalid.items() if key != "plan_sha256"})
        with pytest.raises(ArtifactInventoryServiceError):
            validate_offload_plan(invalid)


def test_active_catalog_pin_blocks_plan_without_changing_persistent_state(archive):
    from bili_asr.storage.archive_contracts import _resource

    root, database, connection = archive
    audio(connection)
    write(root, "audio/test.m4a")
    connection.executescript(_resource("schema-artifact-storage.sql"))
    identity = digest(b"sound")
    connection.execute("INSERT INTO artifact_objects VALUES (?,5,1)", (identity,))
    connection.execute("INSERT INTO artifact_pins VALUES ('active',?,'investigation',1,NULL)", (identity,))
    connection.execute("INSERT INTO artifact_pins VALUES ('released',?,'finished investigation',1,2)", (identity,))
    connection.commit()
    before = database.read_bytes()
    inventory = scan(archive, deep=True, external_holds={})
    assert "persistent_pin:active:investigation" in inventory["objects"][0]["retention_reasons"]
    assert not any("persistent_pin:released" in reason for reason in inventory["objects"][0]["retention_reasons"])
    plan = plan_artifact_offload(inventory, target_id="disk")
    assert plan["items"] == []
    assert "persistent_pin:active:investigation" in plan["held"][0]["reasons"]
    assert database.read_bytes() == before
    connection.execute("UPDATE artifact_pins SET released_at=2 WHERE pin_id='active'")
    connection.commit()
    released = scan(archive, deep=True, external_holds={})
    assert released["copies"][0]["candidate"]
