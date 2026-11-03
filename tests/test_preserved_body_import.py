"""Historical fixture -> exact body import -> edit/review/release/snapshot."""
from copy import deepcopy
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.canonical_json import digest
from bili_asr.export_snapshot import _validate_snapshot, ExportSnapshotError
from bili_asr.publication import edit_edition, get_edition, publish_edition, get_ai_artifacts
from bili_asr.publication_export import export_editorial, export_publication_drafts, export_publications
from bili_asr.publication_origins import PROFILE
from bili_asr.services.archive_snapshot import save_snapshot, check_snapshot, restore_snapshot
from bili_asr.services.preserved_body_import import (
    plan_preserved_body_import,
    apply_preserved_body_import, check_preserved_body_import,
)
from bili_asr.storage.import_origins import import_origin
from bili_asr.storage.publication import PublicationConflictError
from tests.support.content_permissions import grant_test_permission
from tests.fixtures.migrated_archive import copy_archive
from tests.test_publication import approve


@pytest.fixture
def historical(tmp_path, historical_archive_template):
    template, ids = historical_archive_template
    target = copy_archive(template, tmp_path / "archive")
    with ArchiveSession(target, mode=ArchiveAccessMode.WRITE) as session:
        grant_all_current(session.connection, target)
        yield session.connection, target, dict(ids)


def plan_for(connection, root, kind, identity):
    return plan_preserved_body_import(connection, selectors=[{"kind": kind, "id": identity}], artifact_roots=(root,))


def apply(connection, root, plan):
    return apply_preserved_body_import(connection, plan=plan, artifact_roots=(root,), write_root=root, actor="migration-operator")


def grant_all_current(connection, root):
    for row in connection.execute("SELECT edition_id FROM publication_editions"):
        grant_test_permission(connection, get_edition(connection, row[0]), (root,))


@pytest.mark.parametrize("selection", ["ai", "edited", "published"])
def test_import_exact_body_no_inference_or_inherited_approval(historical, tmp_path, selection):
    connection, root, ids = historical
    kind = "ai-revision" if selection == "ai" else "edition"
    identity = ids["draft_revision"] if selection == "ai" else ids["edition_current"] if selection == "edited" else ids["edition_published"]
    old_head = dict(connection.execute("SELECT * FROM publication_heads WHERE video_part_id=?", (2 if selection == "ai" else 1,)).fetchone())
    ledger_tables = ("editorial_inputs", "editorial_revisions", "editorial_model_calls", "document_artifacts", "workflow_jobs", "workflow_attempts")
    before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in ledger_tables}
    plan = plan_for(connection, root, kind, identity)
    assert not plan["blocked"] and len(plan["entries"]) == 1
    baseline = plan["entries"][0]["baseline"]
    result = apply(connection, root, plan)
    edition_id = result["editions"][0]["editionId"]
    edition = get_edition(connection, edition_id)
    assert edition["content_version"] == 2 and edition["review_status"] == "pending-review"
    assert edition["current_release_id"] == old_head["current_release_id"]
    assert edition["revision_id"] == baseline["revisionId"]
    assert hashlib.sha256(edition["content"]["markdown"].encode()).hexdigest() == baseline["bodySha256"]
    assert check_preserved_body_import(connection, edition_id=edition_id, artifact_roots=(root,))["bodyPreserved"]
    assert all(before[table] == [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in ledger_tables)
    with pytest.raises(ValueError, match="not been approved"):
        publish_edition(connection, edition_id=edition_id, artifact_roots=(root,), write_root=root, actor="publisher", expected_release_id=old_head["current_release_id"])
    assert apply(connection, root, plan)["editions"] == result["editions"]
    edited = edit_edition(connection, edition_id=edition_id, markdown_text="Human changed this body.\n", actor="editor", note="clarify")
    assert import_origin(connection, edited["edition_id"])["relation"] == "derived"
    assert not check_preserved_body_import(connection, edition_id=edited["edition_id"], artifact_roots=(root,))["bodyPreserved"]
    edited = edit_edition(connection, edition_id=edited["edition_id"], markdown_text="A second human edit.\n", actor="editor", note="second edit")
    assert apply(connection, root, plan)["idempotent"]
    assert get_edition(connection, edited["edition_id"])["current_edition_id"] == edited["edition_id"]
    private = tmp_path / "review"
    export_editorial(connection, revision_id=edited["revision_id"], edition_id=edited["edition_id"], artifact_roots=(root,), output=private)
    assert json.loads((private / "review.json").read_text(encoding="utf-8"))["ai"]["templateVersion"] == "ai-draft-v1"
    assert (private / "preserved-body.md").read_bytes() == baseline["content"]["markdown"].encode()
    approve(connection, edited)
    release = publish_edition(connection, edition_id=edited["edition_id"], artifact_roots=(root,), write_root=root, actor="publisher", expected_release_id=old_head["current_release_id"])
    assert release["template_version"] == "publish-v2"
    from bili_asr.services.archive_migration import check_migrated_archive
    assert check_migrated_archive(root)["valid"]


def test_new_profile_and_snapshot_restore_closed_loop(historical, tmp_path):
    connection, root, ids = historical
    # Convert each current legacy draft and explicitly publish the historical
    # public part so both new-profile catalogs contain only universal articles.
    selections = [{"kind": "edition", "id": row[0]} for row in connection.execute("SELECT current_edition_id FROM publication_heads")]
    plan = plan_preserved_body_import(connection, selectors=selections, artifact_roots=(root,))
    assert not plan["blocked"]
    result = apply(connection, root, plan)
    grant_all_current(connection, root)
    public_edition = next(get_edition(connection, item["editionId"]) for item in result["editions"] if get_edition(connection, item["editionId"])["video_part_id"] == 1)
    approve(connection, public_edition)
    publish_edition(connection, edition_id=public_edition["edition_id"], artifact_roots=(root,), write_root=root,
                    actor="publisher", expected_release_id=public_edition["current_release_id"])
    for export, kind in ((export_publications, "publication-export"), (export_publication_drafts, "publication-draft-export")):
        with pytest.raises(ExportSnapshotError, match="require universal-origin"):
            export(connection, artifact_roots=(root,), output=tmp_path / "bad")
        output = tmp_path / kind
        export(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE)
        assert _validate_snapshot(output, kind)
        manifest = json.loads((output / f"{kind}-manifest.json").read_text(encoding="utf-8"))
        assert manifest["schemaVersion"] == 2 and manifest["contractProfile"] == PROFILE
        origins = json.loads((output / "origins.json").read_text(encoding="utf-8"))
        assert origins["entries"][0]["kind"] == "preserved-legacy-body"
        assert origins["entries"][0]["aiTemplateVersion"] == "ai-draft-v1"
    connection.close()
    package = tmp_path / "imported.zip"
    save_snapshot(root, package)
    assert check_snapshot(package)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(package, restored)
    with ArchiveSession(restored, mode=ArchiveAccessMode.READ) as session:
        for item in result["editions"]:
            assert check_preserved_body_import(session.connection, edition_id=item["editionId"], artifact_roots=(restored,))["valid"]


def test_head_conflict_and_changed_plan_do_not_install(historical):
    connection, root, ids = historical
    plan = plan_for(connection, root, "edition", ids["edition_current"])
    edit_edition(connection, edition_id=ids["edition_current"], markdown_text="Concurrent edit.", actor="editor", note="update")
    with pytest.raises(PublicationConflictError, match="head changed"):
        apply(connection, root, plan)
    assert connection.execute("SELECT count(*) FROM manuscript_import_baselines").fetchone()[0] == 0
    altered = deepcopy(plan)
    altered["entries"][0]["baseline"]["content"]["markdown"] = "Invented prose.\n"
    altered["planDigest"] = digest({key: value for key, value in altered.items() if key != "planDigest"})
    with pytest.raises(ValueError, match="identity or fields|evidence changed"):
        apply(connection, root, altered)


def test_corrupt_reference_blocks_and_immutable_origin(historical):
    connection, root, ids = historical
    plan = plan_for(connection, root, "ai-revision", ids["draft_revision"])
    edition_id = apply(connection, root, plan)["editions"][0]["editionId"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM publication_import_origins WHERE edition_id=?", (edition_id,))
    connection.rollback()
    artifact = connection.execute("SELECT relative_path FROM document_artifacts WHERE revision_id=? AND artifact_name='review.md'", (ids["draft_revision"],)).fetchone()[0]
    (root / artifact).write_bytes(b"altered reference\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        check_preserved_body_import(connection, edition_id=edition_id, artifact_roots=(root,))
    blocked = plan_for(connection, root, "ai-revision", ids["draft_revision"])
    assert blocked["blocked"]


def test_atomic_batch_failure_and_file_install_retry(historical, monkeypatch):
    connection, root, ids = historical
    selectors = [{"kind": "edition", "id": ids["edition_current"]}, {"kind": "ai-revision", "id": ids["draft_revision"]}]
    plan = plan_preserved_body_import(connection, selectors=selectors, artifact_roots=(root,))
    from bili_asr.manuscript_files import StagedArtifact
    original, count = StagedArtifact.install, 0
    def interrupted(staged):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("simulated crash after first entry installation")
        return original(staged)
    monkeypatch.setattr(StagedArtifact, "install", interrupted)
    with pytest.raises(OSError, match="simulated crash"):
        apply(connection, root, plan)
    assert connection.execute("SELECT count(*) FROM publication_import_origins").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM manuscript_import_batches").fetchone()[0] == 0
    monkeypatch.setattr(StagedArtifact, "install", original)
    assert len(apply(connection, root, plan)["editions"]) == 2


def test_body_normalization_is_blocked(historical, monkeypatch):
    connection, root, ids = historical
    original = get_ai_artifacts(connection, ids["draft_revision"], (root,))
    monkeypatch.setattr("bili_asr.preserved_body_evidence.get_ai_artifacts", lambda *args: {**original, "ai-draft.md": b"\xef\xbb\xbfbody\r\n"})
    plan = plan_for(connection, root, "ai-revision", ids["draft_revision"])
    assert "body_not_canonical" in plan["blocked"][0]["reason"]


def test_cli_plan_apply_check(historical, tmp_path, capsys):
    connection, root, ids = historical
    connection.close()
    from bili_asr.cli import main
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"selectors": [{"kind": "ai-revision", "id": ids["draft_revision"]}]}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    common = ["publication", "import-preserved"]
    assert main([*common, "plan", "--archive-root", str(root), "--selection-file", str(selection), "--out", str(plan_path), "--format", "json"]) == 0
    capsys.readouterr()
    assert main([*common, "apply", "--archive-root", str(root), "--plan-file", str(plan_path), "--actor", "operator", "--format", "json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert main([*common, "check", "--archive-root", str(root), "--edition-id", applied["editions"][0]["editionId"], "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    for command, kind in (("export", "publication-export"), ("export-drafts", "publication-draft-export")):
        output = tmp_path / command
        assert main(["publication", command, "--archive-root", str(root), "--out", str(output),
                     "--contract-profile", PROFILE, "--empty-scope", "--format", "json"]) == 0
        capsys.readouterr()
        assert json.loads((output / "catalog.json").read_text(encoding="utf-8"))["articles"] == []
        assert _validate_snapshot(output, kind)


@pytest.mark.parametrize("identity", [None, {}, [], True, 123, "", "f" * 32])
def test_invalid_revision_selector_is_blocked_without_database_mutation(historical, identity):
    connection, root, _ = historical
    before = connection.total_changes
    plan = plan_preserved_body_import(connection, selectors=[{"kind": "ai-revision", "id": identity}], artifact_roots=(root,))
    assert plan["entries"] == [] and len(plan["blocked"]) == 1
    assert "invalid legacy source ID" in plan["blocked"][0]["reason"]
    assert connection.total_changes == before


@pytest.mark.parametrize("kind", [None, {}, [], True, 123, "unknown"])
def test_invalid_selector_kind_is_reported_as_blocked(historical, kind):
    connection, root, ids = historical
    plan = plan_preserved_body_import(connection, selectors=[{"kind": kind, "id": ids["draft_revision"]}], artifact_roots=(root,))
    assert plan["entries"] == [] and len(plan["blocked"]) == 1
    assert "explicitly select" in plan["blocked"][0]["reason"]


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_exit_recovery_has_one_receipt_and_no_missing_files(historical, tmp_path, after_commit):
    connection, root, ids = historical
    plan = plan_for(connection, root, "ai-revision", ids["draft_revision"])
    plan_file = tmp_path / "crash-plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    script = '''
import json, os, sys
from pathlib import Path
from contextlib import contextmanager
from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.manuscript_files import StagedArtifact
from bili_asr.storage.publication import PublicationRepository
from bili_asr.services.preserved_body_import import apply_preserved_body_import
root, plan_path, after_commit = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "True"
if after_commit:
    original = PublicationRepository.transaction
    @contextmanager
    def interrupted(repository):
        with original(repository):
            yield
        os._exit(79)
    PublicationRepository.transaction = interrupted
else:
    original = StagedArtifact.install
    def interrupted(staged):
        original(staged)
        os._exit(79)
    StagedArtifact.install = interrupted
with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
    apply_preserved_body_import(session.connection, plan=json.loads(plan_path.read_text(encoding="utf-8")), artifact_roots=(root,), write_root=root, actor="crash-probe")
'''
    child = subprocess.run([sys.executable, "-c", script, str(root), str(plan_file), str(after_commit)], capture_output=True, timeout=30)
    assert child.returncode == 79, child.stderr.decode()
    assert (root / ".preserved-body-import-journal.json").exists()
    assert connection.execute("SELECT count(*) FROM manuscript_import_batches").fetchone()[0] == int(after_commit)
    result = apply(connection, root, plan)
    assert result["idempotent"] == after_commit
    assert connection.execute("SELECT count(*) FROM manuscript_import_batches").fetchone()[0] == 1
    assert not (root / ".preserved-body-import-journal.json").exists()
    assert not list(root.glob(".import-stage-*"))
    assert check_preserved_body_import(connection, edition_id=result["editions"][0]["editionId"], artifact_roots=(root,))["valid"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "revision", "metadata", "review", "claim", "unknown", "extra"])
def test_origin_contract_refuses_wrong_bindings(historical, tmp_path, change):
    connection, root, ids = historical
    selectors = [{"kind": "edition", "id": row[0]} for row in connection.execute("SELECT current_edition_id FROM publication_heads")]
    plan = plan_preserved_body_import(connection, selectors=selectors, artifact_roots=(root,))
    apply(connection, root, plan)
    grant_all_current(connection, root)
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE)
    catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))
    origins = json.loads((output / "origins.json").read_text(encoding="utf-8"))
    from bili_asr.publication_origins import validate_origins
    entry = origins["entries"][0]
    if change == "missing":
        origins["entries"].clear()
    elif change == "duplicate":
        origins["entries"].append(deepcopy(entry))
    elif change == "revision":
        entry["legacyAiRevisionId"] = "0" * 64
    elif change == "metadata":
        entry["sourceMetadataSha256"] = "0" * 64
    elif change == "review":
        entry["reviewArtifactSha256"] = "0" * 64
    elif change == "claim":
        entry["bodyPreserved"] = False
    elif change == "unknown":
        entry["kind"] = "fake-ai-draft-v2"
    else:
        entry["privatePrompt"] = "must not be public"
    with pytest.raises(ExportSnapshotError, match="origin-profile"):
        validate_origins(origins, catalog["articles"], "publication-draft")


def test_empty_origin_profile_and_native_only(historical, tmp_path):
    connection, root, ids = historical
    from dataclasses import replace
    from bili_asr.services.archive_migration import initialize_archive
    from bili_asr.editorial import EditorialConfig
    from bili_asr.platform_identity import ContentRef
    from bili_asr.publication import create_edition
    from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
    from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
    native_root = tmp_path / "native"
    initialize_archive(native_root)
    with ArchiveSession(native_root, mode=ArchiveAccessMode.WRITE) as session:
        native = session.connection
        empty = tmp_path / "empty"
        export_publications(native, artifact_roots=(native_root,), output=empty, contract_profile=PROFILE)
        assert json.loads((empty / "catalog.json").read_text(encoding="utf-8"))["schemaVersion"] == 3
        assert json.loads((empty / "origins.json").read_text(encoding="utf-8"))["entries"] == []
        with native:
            part_id = SourceRepository(native).upsert_video(SourceVideoMetadata(ContentRef("youtube", "dQw4w9WgXcQ", 0), "Native source", 2000))
        insert_record(native, replace(record(("Native source prose.",)), video_part_id=part_id, language="en"))
        workflow, repository, handlers, executor = runtime(native, native_root, FakeClient())
        prepared = repository.prepare(1, None, EditorialConfig())
        job_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
        assert executor.run().failed == 0
        create_edition(native, revision_id=repository.revision_for_job(job_id), artifact_roots=(native_root,), actor="editor")
        grant_all_current(native, native_root)
        output = tmp_path / "native-drafts"
        export_publication_drafts(native, artifact_roots=(native_root,), output=output, contract_profile=PROFILE)
        assert json.loads((output / "origins.json").read_text(encoding="utf-8"))["entries"][0]["kind"] == "ai-generated-v2"


def test_native_and_imported_drafts_share_exact_profile(historical, tmp_path):
    connection, root, ids = historical
    from dataclasses import replace
    from bili_asr.editorial import EditorialConfig
    from bili_asr.platform_identity import ContentRef
    from bili_asr.publication import create_edition
    from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
    from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
    selectors = [{"kind": "edition", "id": row[0]} for row in connection.execute("SELECT current_edition_id FROM publication_heads")]
    plan = plan_preserved_body_import(connection, selectors=selectors, artifact_roots=(root,))
    imported = apply(connection, root, plan)
    preserved = next(get_edition(connection, item["editionId"]) for item in imported["editions"]
                     if get_edition(connection, item["editionId"])["video_part_id"] == 1)
    edit_edition(connection, edition_id=preserved["edition_id"], markdown_text="Fixture human edit after preservation.\n", actor="editor", note="fixture derived body")
    with connection:
        part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(ContentRef("youtube", "dQw4w9WgXcQ", 0), "Native source", 2000))
    transcript_id = connection.execute("SELECT max(transcript_id)+1 FROM transcripts").fetchone()[0]
    insert_record(connection, replace(record(("Native source prose.",), transcript_id=transcript_id), video_part_id=part_id, language="en"))
    workflow, repository, handlers, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(transcript_id, None, EditorialConfig())
    job_id, *_ = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
    assert executor.run().failed == 0
    create_edition(connection, revision_id=repository.revision_for_job(job_id), artifact_roots=(root,), actor="editor")
    grant_all_current(connection, root)
    output = tmp_path / "mixed-drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE)
    assert _validate_snapshot(output, "publication-draft-export")
    kinds = {entry["kind"] for entry in json.loads((output / "origins.json").read_text(encoding="utf-8"))["entries"]}
    assert kinds == {"ai-generated-v2", "preserved-legacy-body", "edited-after-preservation"}


def test_metadata_edit_and_tag_sync_keep_preservation_origin(historical):
    connection, root, ids = historical
    imported = apply(connection, root, plan_for(connection, root, "edition", ids["edition_current"]))["editions"][0]
    edition = get_edition(connection, imported["editionId"])
    edited = edit_edition(connection, edition_id=edition["edition_id"], markdown_text=edition["content"]["markdown"],
                         metadata={"title": "Editorial title", "summary": "Editorial summary"}, actor="editor", note="metadata")
    assert import_origin(connection, edited["edition_id"])["relation"] == "preserved"
    from bili_asr.publication_tags import sync_source_tags
    synced = sync_source_tags(connection, edition_id=edited["edition_id"], actor="editor", note="tags")
    assert import_origin(connection, synced["edition_id"])["import_id"] == imported["importId"]
    assert check_preserved_body_import(connection, edition_id=synced["edition_id"], artifact_roots=(root,))["bodyPreserved"]
    assert synced["content"]["source"]["metadata"]["tags"] == []


def test_missing_origin_never_relaxes_native_version_check(historical):
    connection, root, ids = historical
    from bili_asr.storage.publication import PublicationRepository
    plan = plan_for(connection, root, "edition", ids["edition_current"])
    baseline = plan["entries"][0]["baseline"]
    repository = PublicationRepository(connection)
    with repository.transaction():
        fake = repository.insert_edition(part_id=1, revision_id=baseline["revisionId"], content=baseline["content"],
            parent_edition_id=ids["edition_current"], actor="test", note="unproven", event_type="created", content_version=2)
    with pytest.raises(ValueError, match="requires immutable import origin"):
        get_edition(connection, fake["edition_id"])


def test_explicit_export_scope_never_silently_drops_unknown_or_stale_ids(historical, tmp_path):
    connection, root, ids = historical
    imported = apply(connection, root, plan_for(connection, root, "ai-revision", ids["draft_revision"]))["editions"][0]
    edition_id = imported["editionId"]
    grant_test_permission(connection, get_edition(connection, edition_id), (root,))
    output = tmp_path / "selected"
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE, edition_ids=[edition_id]) == 1
    assert json.loads((output / "catalog.json").read_text(encoding="utf-8"))["articles"][0]["editionId"] == edition_id
    for selected in (["f"*32], [edition_id, edition_id]):
        with pytest.raises(ExportSnapshotError, match="export-scope"):
            export_publication_drafts(connection, artifact_roots=(root,), output=tmp_path / "bad-scope", contract_profile=PROFILE, edition_ids=selected)
    edit_edition(connection, edition_id=edition_id, markdown_text="Next version.\n", actor="editor", note="next")
    with pytest.raises(ExportSnapshotError, match="no longer current"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output, contract_profile=PROFILE, edition_ids=[edition_id])
    assert export_publications(connection, artifact_roots=(root,), output=tmp_path / "explicit-empty", contract_profile=PROFILE, release_ids=[]) == 0
    assert _validate_snapshot(tmp_path / "explicit-empty", "publication-export")
