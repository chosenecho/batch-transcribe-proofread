"""Evidence-bound title supplementation keeps prose, reviews and history intact."""
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.canonical_json import digest
from bili_asr.export_snapshot import ExportSnapshotError, _validate_snapshot
from bili_asr.publication import edit_edition, get_edition, publish_edition
from bili_asr.publication_export import export_publication_drafts, export_publications
from bili_asr.publication_origins import PROFILE
from bili_asr.services.archive_migration import check_migrated_archive
from bili_asr.services.archive_snapshot import (
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from bili_asr.services.source_supplement import (
    apply_source_supplement,
    check_source_supplement,
    plan_source_supplement,
)
from bili_asr.storage.publication import PublicationConflictError, PublicationRepository
from bili_asr.storage.source_supplements import edition_supplement
from tests.support.content_permissions import grant_test_permission
from tests.fixtures.migrated_archive import copy_archive
from tests.test_publication import approve


@pytest.fixture
def preserved(tmp_path, preserved_archive_template):
    template, edition_ids = preserved_archive_template
    root = copy_archive(template, tmp_path / "archive")
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        editions = [get_edition(connection, identity) for identity in edition_ids]
        for edition in editions:
            grant_test_permission(connection, edition, (root,))
        yield connection, root, editions


def plan_for(connection, root, editions):
    return plan_source_supplement(connection, edition_ids=[edition["edition_id"] for edition in editions], artifact_roots=(root,))


def apply(connection, root, plan):
    return apply_source_supplement(connection, plan=plan, artifact_roots=(root,), actor="operator")


def table_rows(connection, tables):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}


def test_title_only_change_preserves_current_human_edits_and_historical_ai(preserved):
    connection, root, editions = preserved
    edited = edit_edition(connection, edition_id=editions[0]["edition_id"], markdown_text="Already edited by a human.\n",
                          metadata={"title": "Editorial heading", "summary": "A human summary", "tags": ["editorial"]},
                          actor="editor", note="clarification")
    tables = ("editorial_inputs", "editorial_revisions", "editorial_model_calls", "document_artifacts",
              "workflow_jobs", "workflow_attempts", "manuscript_import_baselines", "publication_releases")
    before = table_rows(connection, tables)
    plan = plan_for(connection, root, [edited])
    assert plan["fieldStats"]["eligible"] == 1 and not plan["blocked"]
    result = apply(connection, root, plan)
    current = get_edition(connection, result["editions"][0]["editionId"])
    expected = deepcopy(edited["content"])
    expected["source"]["metadata"]["partTitle"] = "概要"
    assert current["content"] == expected
    assert current["review_status"] == "pending-review"
    assert current["revision_id"] == edited["revision_id"]
    assert current["current_release_id"] == edited["current_release_id"]
    assert table_rows(connection, tables) == before
    assert get_edition(connection, edited["edition_id"])["content"] == edited["content"]
    checked = check_source_supplement(connection, edition_id=current["edition_id"], artifact_roots=(root,))
    assert checked["valid"] and not checked["bodyPreserved"] and checked["observedAt"] is None
    later = edit_edition(connection, edition_id=current["edition_id"], markdown_text="Another human edit.\n", actor="editor", note="later")
    assert get_edition(connection, later["edition_id"])["content"]["source"]["metadata"]["partTitle"] == "概要"
    assert edition_supplement(connection, later["edition_id"])["supplement_id"] == checked["supplementId"]
    assert apply(connection, root, plan)["editions"] == result["editions"]
    assert get_edition(connection, later["edition_id"])["current_edition_id"] == later["edition_id"]
    assert plan_for(connection, root, [later])["skipped"][0]["reason"] == "already-known"
    from bili_asr.publication_tags import sync_source_tags
    synced = sync_source_tags(connection, edition_id=later["edition_id"], actor="editor", note="tags")
    assert check_source_supplement(connection, edition_id=synced["edition_id"], artifact_roots=(root,))["supplementId"] == checked["supplementId"]
    with connection:
        connection.execute("UPDATE video_parts SET title='Later source observation' WHERE video_part_id=?", (synced["video_part_id"],))
    assert get_edition(connection, synced["edition_id"])["content"]["source"]["metadata"]["partTitle"] == "概要"
    assert check_migrated_archive(root)["valid"]


@pytest.mark.parametrize("field,value", [("platform", "youtube"), ("externalVideoId", "BVwrong"),
    ("partIndex", 99), ("videoPartId", 99), ("cid", 99), ("value", "Invented"), ("observedAt", 123)])
def test_rehashed_invented_evidence_is_rejected_without_partial_commit(preserved, field, value):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    plan["entries"][-1]["supplement"]["evidence"][field] = value
    plan["entries"][-1]["supplement"]["supplementId"] = digest(plan["entries"][-1]["supplement"]["evidence"])
    plan["planDigest"] = digest({key: value for key, value in plan.items() if key != "planDigest"})
    tables = ("publication_editions", "publication_events", "publication_heads", "source_metadata_supplements",
              "publication_source_supplements", "source_supplement_batches")
    before = table_rows(connection, tables)
    with pytest.raises(ValueError, match="evidence changed or was invented"):
        apply(connection, root, plan)
    assert table_rows(connection, tables) == before


def test_head_and_live_evidence_changes_block_stale_plan(preserved):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    with connection:
        connection.execute("UPDATE video_parts SET title='Changed upstream title' WHERE video_part_id=?", (editions[0]["video_part_id"],))
    with pytest.raises(ValueError, match="evidence changed"):
        apply(connection, root, plan)
    plan = plan_for(connection, root, editions)
    edit_edition(connection, edition_id=editions[0]["edition_id"], markdown_text="Concurrent edit", actor="editor", note="race")
    with pytest.raises(PublicationConflictError, match="head changed"):
        apply(connection, root, plan)


def test_release_head_changes_block_plan(preserved):
    connection, root, editions = preserved
    edition = editions[0]
    approve(connection, edition)
    plan = plan_for(connection, root, [edition])
    publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=(root,), write_root=root,
                    actor="publisher", expected_release_id=edition["current_release_id"])
    with pytest.raises(PublicationConflictError, match="head changed"):
        apply(connection, root, plan)


def test_title_fallback_unknown_and_no_evidence_are_reported(preserved):
    connection, root, editions = preserved
    with connection:
        connection.execute("UPDATE video_parts SET title=(SELECT title FROM videos WHERE videos.bvid=video_parts.bvid) WHERE video_part_id=?",
                           (editions[0]["video_part_id"],))
        connection.execute("UPDATE video_parts SET title='' WHERE video_part_id=?", (editions[1]["video_part_id"],))
    plan = plan_for(connection, root, editions)
    assert len(plan["blocked"]) == len(editions)
    assert not plan["entries"]
    with pytest.raises(ValueError, match="blocked"):
        apply(connection, root, plan)


def test_mid_batch_failure_rolls_back_and_retry_creates_one_result(preserved, monkeypatch):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    original = PublicationRepository.insert_edition
    calls = 0

    def fail_second(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated second insert failure")
        return original(self, **kwargs)

    tables = ("publication_editions", "publication_edition_reviews", "publication_events", "publication_heads",
              "publication_import_origins", "source_metadata_supplements", "publication_source_supplements", "source_supplement_batches")
    before = table_rows(connection, tables)
    monkeypatch.setattr(PublicationRepository, "insert_edition", fail_second)
    with pytest.raises(RuntimeError, match="second insert failure"):
        apply(connection, root, plan)
    assert table_rows(connection, tables) == before
    monkeypatch.setattr(PublicationRepository, "insert_edition", original)
    result = apply(connection, root, plan)
    assert apply(connection, root, plan)["editions"] == result["editions"]
    assert connection.execute("SELECT COUNT(*) FROM source_supplement_batches").fetchone()[0] == 1


def test_export_pair_and_snapshot_restore_keep_title_evidence(preserved, tmp_path):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    result = apply(connection, root, plan)
    current = [get_edition(connection, item["editionId"]) for item in result["editions"]]
    grant_test_permission(connection, current[1], (root,))
    assert all(check_source_supplement(connection, edition_id=item["edition_id"], artifact_roots=(root,))["bodyPreserved"] for item in current)
    approve(connection, current[0])
    publish_edition(connection, edition_id=current[0]["edition_id"], artifact_roots=(root,), write_root=root,
                    actor="publisher", expected_release_id=current[0]["current_release_id"])
    for export, kind in ((export_publications, "publication-export"), (export_publication_drafts, "publication-draft-export")):
        output = tmp_path / kind
        export(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE)
        assert _validate_snapshot(output, kind)
        origins = json.loads((output / "origins.json").read_text(encoding="utf-8"))
        assert all(item["policyVersion"] == "legacy-part-title-supplement-v1" for item in origins["entries"])
        catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
        assert catalog["articles"][0]["sourceMetadata"]["partTitle"] in {"概要", "如何进行真正的哲学反思"}
        assert catalog["articles"][0]["sourceMetadata"]["metadataObservedAt"] is None
        from bili_asr.publication_origins import validate_origins
        broken = deepcopy(origins)
        broken["entries"][0]["metadataSupplement"]["evidence"]["partIndex"] += 1
        with pytest.raises(ExportSnapshotError, match="supplement"):
            validate_origins(broken, catalog["articles"], origins["manuscriptType"])
    for table in ("source_metadata_supplements", "publication_source_supplements", "source_supplement_batches"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {table}")
        connection.rollback()
    connection.close()
    package = tmp_path / "supplemented.zip"
    save_snapshot(root, package)
    assert check_snapshot(package)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(package, restored)
    with ArchiveSession(restored, mode=ArchiveAccessMode.READ) as session:
        for item in result["editions"]:
            assert check_source_supplement(session.connection, edition_id=item["editionId"], artifact_roots=(restored,))["valid"]


def test_cli_plan_apply_check_and_no_overwrite(preserved, tmp_path, capsys):
    from bili_asr.cli import main
    root, editions = preserved[1:]
    selection, plan_file = tmp_path / "selection.json", tmp_path / "plan.json"
    selection.write_text(json.dumps({"editionIds": [item["edition_id"] for item in editions]}), encoding="utf-8")
    command = ["publication", "supplement-source"]
    common = ["--archive-root", str(root), "--format", "json"]
    assert main(command + ["plan", *common, "--selection-file", str(selection), "--out", str(plan_file)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["fieldStats"]["eligible"] == len(editions)
    assert main(command + ["plan", *common, "--selection-file", str(selection), "--out", str(plan_file)]) == 1
    capsys.readouterr()
    assert main(command + ["apply", *common, "--plan-file", str(plan_file), "--actor", "operator"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fieldStats"]["unknownAfter"] == 0
    assert main(command + ["check", *common, "--edition-id", result["editions"][0]["editionId"]]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(command + ["apply", *common, "--plan-file", str(plan_file), "--actor", "operator"]) == 0
    assert json.loads(capsys.readouterr().out)["idempotent"]


CHILD_APPLY = '''
import json, os, sys
from pathlib import Path
from contextlib import contextmanager
from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.storage.publication import PublicationRepository
from bili_asr.services.source_supplement import apply_source_supplement
root, plan_path, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if stage == "before":
    original = PublicationRepository.insert_edition
    def interrupted(repository, **kwargs):
        original(repository, **kwargs)
        os._exit(79)
    PublicationRepository.insert_edition = interrupted
elif stage == "after":
    original = PublicationRepository.transaction
    @contextmanager
    def interrupted(repository):
        with original(repository):
            yield
        os._exit(79)
    PublicationRepository.transaction = interrupted
with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
    result = apply_source_supplement(session.connection, plan=json.loads(plan_path.read_text(encoding="utf-8")),
                                    artifact_roots=(root,), actor="subprocess-probe")
    print(json.dumps(result))
'''


@pytest.mark.parametrize("stage", ["before", "after"])
def test_process_exit_before_and_after_commit_recovers_one_receipt(preserved, tmp_path, stage):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    child = subprocess.run([sys.executable, "-c", CHILD_APPLY, str(root), str(plan_path), stage], capture_output=True, timeout=40, check=False)
    assert child.returncode == 79, child.stderr.decode(errors="replace")
    committed = stage == "after"
    assert connection.execute("SELECT COUNT(*) FROM source_supplement_batches").fetchone()[0] == int(committed)
    result = apply(connection, root, plan)
    assert result["idempotent"] == committed
    assert connection.execute("SELECT COUNT(*) FROM source_supplement_batches").fetchone()[0] == 1
    for item in result["editions"]:
        assert check_source_supplement(connection, edition_id=item["editionId"], artifact_roots=(root,))["valid"]


def test_concurrent_identical_batches_share_the_same_editions(preserved, tmp_path):
    connection, root, editions = preserved
    plan = plan_for(connection, root, editions)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    children = [subprocess.Popen([sys.executable, "-c", CHILD_APPLY, str(root), str(plan_path), "normal"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    results = []
    for child in children:
        output, errors = child.communicate(timeout=40)
        assert child.returncode == 0, errors.decode(errors="replace")
        results.append(json.loads(output))
    assert results[0]["editions"] == results[1]["editions"]
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert connection.execute("SELECT COUNT(*) FROM source_supplement_batches").fetchone()[0] == 1


def test_native_edition_is_skipped_and_keeps_its_frozen_source(preserved):
    from dataclasses import replace

    from bili_asr.editorial import EditorialConfig
    from bili_asr.platform_identity import ContentRef
    from bili_asr.publication import create_edition
    from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
    from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
    connection, root, editions = preserved
    with connection:
        part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(ContentRef("youtube", "dQw4w9WgXcQ", 0), "Native source", 2000))
    transcript_id = connection.execute("SELECT max(transcript_id)+1 FROM transcripts").fetchone()[0]
    insert_record(connection, replace(record(("Native source prose.",), transcript_id=transcript_id), video_part_id=part_id, language="en"))
    workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(transcript_id, None, EditorialConfig())
    job_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
    assert executor.run().failed == 0
    native = create_edition(connection, revision_id=repository.revision_for_job(job_id), artifact_roots=(root,), actor="editor")
    plan = plan_for(connection, root, [*editions, native])
    assert plan["skipped"] == [{"editionId": native["edition_id"], "reason": "not-preserved-legacy"}]
    result = apply(connection, root, plan)
    assert len(result["editions"]) == len(editions)
    assert get_edition(connection, native["edition_id"])["content"] == native["content"]
    assert get_edition(connection, native["edition_id"])["current_edition_id"] == native["edition_id"]


@pytest.mark.parametrize("damage", ["evidence", "lost-inheritance", "wrong-cid"])
def test_storage_reader_rejects_damaged_evidence_and_inheritance(preserved, damage):
    connection, root, editions = preserved
    result = apply(connection, root, plan_for(connection, root, editions))
    edition = get_edition(connection, result["editions"][0]["editionId"])
    if damage == "lost-inheritance":
        edition = edit_edition(connection, edition_id=edition["edition_id"], markdown_text="Later body", actor="editor", note="edit")
    connection.close()
    # Simulate offline tampering, including restoring the original immutable
    # trigger definitions, so shape validation alone cannot catch the damage.
    with sqlite3.connect(root / "archive.db") as tampered:
        if damage == "wrong-cid":
            tampered.execute("UPDATE video_parts SET cid=cid+1 WHERE video_part_id=?", (edition["video_part_id"],))
        else:
            trigger = "supplements_no_update" if damage == "evidence" else "edition_supplements_no_delete"
            original = tampered.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger,)).fetchone()[0]
            tampered.execute(f"DROP TRIGGER {trigger}")
            if damage == "evidence":
                row = tampered.execute("SELECT supplement_id,evidence_json FROM source_metadata_supplements LIMIT 1").fetchone()
                evidence = json.loads(row[1])
                evidence["value"] = "Tampered title"
                tampered.execute("UPDATE source_metadata_supplements SET evidence_json=? WHERE supplement_id=?", (json.dumps(evidence), row[0]))
            else:
                tampered.execute("DELETE FROM publication_source_supplements WHERE edition_id=?", (edition["edition_id"],))
            tampered.execute(original)
    with ArchiveSession(root, mode=ArchiveAccessMode.READ) as session, pytest.raises(ValueError, match="supplement|import-integrity"):
        get_edition(session.connection, edition["edition_id"])
    from bili_asr.services.archive_snapshot import SnapshotError
    with pytest.raises(SnapshotError, match="supplement|import-integrity"):
        save_snapshot(root, root.parent / "damaged.zip")
