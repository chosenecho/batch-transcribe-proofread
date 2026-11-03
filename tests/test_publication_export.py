"""Release-only projections and recoverable managed-directory exports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

import pytest

from bili_asr import export_snapshot
from bili_asr import archive as archive_module
from bili_asr.editorial import EditorialConfig
from bili_asr.export_snapshot import ExportSnapshotError, guard_output, json_bytes, replace_snapshot
from bili_asr.publication import (
    create_edition, edit_edition, get_edition, publish_edition, review_edition, withdraw_release,
)
from bili_asr.publication_export import export_editorial, export_publications
from bili_asr.storage import open_database
from bili_asr.storage.database import SchemaContractError
from bili_asr.storage.workflow_outputs import record_bundle_publication
from tests.test_ai_editorial import FakeClient, insert_record, record, runtime
from tests.test_workflow_control_plane import _seed_part


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


@pytest.fixture
def manuscript(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    _seed_part(connection)
    insert_record(connection, record())
    paths = archive_module.write_archive(
        root,
        {"bvid": "BVtest", "cid": 1, "page_index": 0, "work_id": "BVtest:p0",
         "title": "test", "video_title": "test", "pubdate_str": "1970-01-01",
         "duration_s": 1},
        [{"start": 0, "end": 1, "text": "这是一段原始文字"}],
        source="asr",
    )
    connection.execute("BEGIN IMMEDIATE")
    record_bundle_publication(connection, part_id=1, transcript_id=1, paths=paths)
    connection.commit()
    workflow, repository, _handlers, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    jobs = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision_id = repository.revision_for_job(jobs[0])
    edition = create_edition(connection, revision_id=revision_id, artifact_roots=(root,), actor="test-editor")
    try:
        yield connection, root, revision_id, edition
    finally:
        connection.close()


def approve(connection, edition):
    from tests.support.content_permissions import grant_test_permission
    grant_test_permission(connection, edition)
    review_edition(connection, edition_id=edition["edition_id"], status="in-review",
                   content_sha256=edition["content_sha256"], expected_status="pending-review", actor="test-reviewer")
    review_edition(connection, edition_id=edition["edition_id"], status="approved",
                   content_sha256=edition["content_sha256"], expected_status="in-review", actor="test-reviewer", note="verified test content")


def publish(connection, root, edition, previous=None):
    approve(connection, edition)
    return publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=(root,), write_root=root,
                           actor="test-publisher", expected_release_id=previous)


def tree_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def catalog(output):
    return json.loads((output / "catalog.json").read_text(encoding="utf-8"))


def test_export_lifecycle_keeps_published_a_until_b_is_explicitly_released(manuscript, tmp_path):
    connection, root, revision_id, a = manuscript
    output = tmp_path / "public"
    assert export_publications(connection, artifact_roots=(root,), output=output) == 0
    assert catalog(output) == {"schemaVersion": 2, "manuscriptType": "publication", "articles": []}
    approve(connection, a)
    assert export_publications(connection, artifact_roots=(root,), output=output) == 0
    a_release = publish_edition(connection, edition_id=a["edition_id"], artifact_roots=(root,), write_root=root, actor="test-publisher")
    assert export_publications(connection, artifact_roots=(root,), output=output) == 1
    entry = catalog(output)["articles"][0]
    assert entry["releaseId"] == a_release["release_id"]
    assert entry["aiRevisionId"] == revision_id
    assert entry["artifactSha256"] == hashlib.sha256((output / entry["file"]).read_bytes()).hexdigest()
    a_files = tree_bytes(output)
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="A different test body.\n",
                     metadata={"title": "Frozen B title", "tags": ["test"]}, actor="test-editor", note="new edition")
    with connection:
        connection.execute("UPDATE videos SET title = 'Live mutable title'")
    assert export_publications(connection, artifact_roots=(root,), output=output) == 1
    assert tree_bytes(output) == a_files
    approve(connection, b)
    assert export_publications(connection, artifact_roots=(root,), output=output) == 1
    assert tree_bytes(output) == a_files
    b_release = publish_edition(connection, edition_id=b["edition_id"], artifact_roots=(root,), write_root=root,
                                actor="test-publisher", expected_release_id=a_release["release_id"])
    assert export_publications(connection, artifact_roots=(root,), output=output) == 1
    assert catalog(output)["articles"][0]["title"] == "Frozen B title"
    assert catalog(output)["articles"][0]["releaseId"] == b_release["release_id"]
    assert "Live mutable title" not in (output / entry["file"]).read_text(encoding="utf-8")
    withdraw_release(connection, release_id=b_release["release_id"], actor="test-publisher", note="withdraw test")
    assert export_publications(connection, artifact_roots=(root,), output=output) == 0
    assert set(tree_bytes(output)) == {"catalog.json", "publication-export-manifest.json"}


def test_public_snapshot_excludes_internal_metadata_and_database_is_read_only(manuscript, tmp_path):
    connection, root, _revision_id, edition = manuscript
    publish(connection, root, edition)
    connection.close()
    connection = sqlite3.connect((root / "archive.db").as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        before = (root / "archive.db").read_bytes()
        output = tmp_path / "public"
        assert export_publications(connection, artifact_roots=(root,), output=output) == 1
        assert (root / "archive.db").read_bytes() == before
        entry = catalog(output)["articles"][0]
        assert not {"reviewStatus", "reviewId", "actor", "qualityStatus", "config", "model"} & entry.keys()
        snapshot = b"\n".join(tree_bytes(output).values())
        assert b"test-reviewer" not in snapshot
        assert (output / entry["reviewFile"]).read_bytes() == (root / f"documents/part-1/{entry['aiRevisionId']}/ai-draft-v1/review.md").read_bytes()
        assert not (output / "review.md").exists()
    finally:
        connection.close()


def test_corrupt_release_and_broken_head_fail_without_changing_public_output(manuscript, tmp_path):
    connection, root, _revision_id, edition = manuscript
    release = publish(connection, root, edition)
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    article = root / release["relative_path"]
    original = article.read_bytes()
    article.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before
    article.write_bytes(original)
    connection.execute("PRAGMA foreign_keys = OFF")
    with connection:
        connection.execute("UPDATE publication_heads SET current_release_id = ?", ("f" * 64,))
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(ValueError, match="matching current publication head"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


@pytest.mark.parametrize("delete_head", [False, True])
def test_published_release_with_missing_head_is_not_silently_omitted(manuscript, tmp_path, delete_head):
    connection, root, _revision_id, edition = manuscript
    publish(connection, root, edition)
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    with connection:
        if delete_head:
            connection.execute("DELETE FROM publication_heads")
        else:
            connection.execute("UPDATE publication_heads SET current_release_id = NULL")
    with pytest.raises(ExportSnapshotError, match="matching current publication head"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


def test_review_export_exact_baseline_parent_diff_and_review_target(manuscript, tmp_path):
    connection, root, revision_id, parent = manuscript
    edition = edit_edition(connection, edition_id=parent["edition_id"], markdown_text="Human amended body.\n",
                           metadata={"title": "Changed title"}, actor="test-editor", note="clarified test claim")
    review_edition(connection, edition_id=edition["edition_id"], status="in-review", content_sha256=edition["content_sha256"],
                   expected_status="pending-review", actor="test-reviewer", note="check source")
    output = tmp_path / "review"
    result = export_editorial(connection, revision_id=revision_id, edition_id=edition["edition_id"], artifact_roots=(root,), output=output)
    assert result["content_sha256"] == edition["content_sha256"]
    data = json.loads((output / "review.json").read_text(encoding="utf-8"))
    assert data["editionId"] == edition["edition_id"] and data["reviewStatus"] == "in-review"
    assert data["reviews"][0]["content_sha256"] == edition["content_sha256"]
    assert data["parentEditionId"] == parent["edition_id"]
    lineage = data["ai"]["sourceLineage"]
    assert lineage["revision_id"] == revision_id and lineage["semantic_status"] == "unverified"
    assert lineage["review_required"] and lineage["human_review_status"] == "not_reviewed"
    assert lineage["paragraphs"][0]["declared_source_range"]["semantic_support"] == "unverified"
    assert "request_json" not in str(data) and "response_json" not in str(data)
    assert b"Human amended body" in (output / "differences/ai.patch").read_bytes()
    assert b"Changed title" in (output / "differences/parent.patch").read_bytes()
    assert (output / "edition.md").read_text(encoding="utf-8").startswith("# Changed title\n")
    assert json.loads((output / "edition.json").read_text(encoding="utf-8")) == get_edition(connection, edition["edition_id"])["content"]
    ai_path = root / "documents" / "part-1" / revision_id / "ai-draft-v1"
    assert (output / "review.md").read_bytes() == (ai_path / "review.md").read_bytes()
    assert (output / "ai-draft.md").read_bytes() == (ai_path / "ai-draft.md").read_bytes()
    with pytest.raises(ValueError, match="does not belong"):
        export_editorial(connection, revision_id="f" * 64, edition_id=edition["edition_id"], artifact_roots=(root,), output=output)
    assert result["snapshot_id"] == json.loads((output / "editorial-export-manifest.json").read_text(encoding="utf-8"))["snapshotId"]
    with pytest.raises(ValueError, match="managed publication-export"):
        export_publications(connection, artifact_roots=(root,), output=output)


def test_unchanged_initial_edition_has_no_artificial_ai_or_parent_difference(manuscript, tmp_path):
    connection, root, revision_id, edition = manuscript
    output = tmp_path / "review"
    export_editorial(connection, revision_id=revision_id, edition_id=edition["edition_id"],
                     artifact_roots=(root,), output=output)
    assert (output / "differences/ai.patch").read_bytes() == b""
    assert (output / "differences/parent.patch").read_bytes() == b""


@pytest.mark.parametrize("relative", ["manual.md", "articles/manual.md", "db-import-manifest.json"])
def test_public_output_refuses_manual_files_or_legacy_contracts(manuscript, tmp_path, relative):
    connection, root, _revision_id, _edition = manuscript
    output = tmp_path / "public"
    path = output / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"manual content")
    before = tree_bytes(output)
    with pytest.raises(ValueError, match="managed publication-export"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


def test_export_refuses_modified_managed_files_and_extra_directory(manuscript, tmp_path):
    connection, root, _revision_id, edition = manuscript
    publish(connection, root, edition)
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    article = output / catalog(output)["articles"][0]["file"]
    original = article.read_bytes()
    article.write_bytes(b"hand edited")
    with pytest.raises(ValueError, match="missing or modified"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert article.read_bytes() == b"hand edited"
    article.write_bytes(original)
    (output / "manual-empty-directory").mkdir()
    with pytest.raises(ValueError, match="unmanaged"):
        export_publications(connection, artifact_roots=(root,), output=output)


@pytest.mark.parametrize("which", ["inside-root", "source-root", "source-ancestor"])
def test_export_output_must_not_overlap_archive(manuscript, tmp_path, which):
    connection, root, _revision_id, _edition = manuscript
    target = {"inside-root": root / "public", "source-root": root, "source-ancestor": tmp_path}[which]
    with pytest.raises(ExportSnapshotError, match="overlaps"):
        export_publications(connection, artifact_roots=(root,), output=target)


def test_output_symlink_and_managed_child_symlink_are_refused(manuscript, tmp_path):
    connection, root, _revision_id, edition = manuscript
    publish(connection, root, edition)
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("this platform cannot create directory symlinks")
    with pytest.raises(ExportSnapshotError, match="link or reparse"):
        guard_output(connection, alias / "public", (root,))
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    articles = output / "articles"
    parked = tmp_path / "parked-articles"
    os.replace(articles, parked)
    articles.symlink_to(parked, target_is_directory=True)
    with pytest.raises(ExportSnapshotError, match="link or reparse"):
        export_publications(connection, artifact_roots=(root,), output=output)
    assert parked.is_dir()


def snapshot_files(body=b"first\n"):
    review = b"Unreviewed reference with original text.\n"
    article = {"manuscriptType": "publication", "slug": "part-1", "title": "Example", "summary": "", "tags": [],
               "attribution": "Test compilation.", "editorNote": "", "releaseId": "a" * 64, "editionId": "b" * 32,
               "aiRevisionId": "c" * 64, "videoPartId": 1, "bvid": "BVtest", "pageIndex": 0,
               "sourceUrl": "https://www.bilibili.com/video/BVtest/?p=1", "contentSha256": "d" * 64,
               "artifactSha256": hashlib.sha256(body).hexdigest(), "templateVersion": "publish-v1", "publishedAt": 1,
               "file": "articles/part-1/publish.md", "reviewFile": "articles/part-1/review.md",
               "reviewArtifactSha256": hashlib.sha256(review).hexdigest()}
    return {"catalog.json": json_bytes({"schemaVersion": 2, "manuscriptType": "publication", "articles": [article]}),
            "articles/part-1/publish.md": body, "articles/part-1/review.md": review}


def test_snapshot_second_rename_failure_restores_previous_directory(tmp_path, monkeypatch):
    output = tmp_path / "public"
    replace_snapshot(output, kind="publication-export", files=snapshot_files())
    before = tree_bytes(output)
    real_replace = os.replace
    def fail_install(source, target):
        if str(source).endswith(".stage") and Path(target) == output:
            raise OSError("simulated rename failure")
        return real_replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "replace", fail_install)
        with pytest.raises(OSError, match="simulated"):
            replace_snapshot(output, kind="publication-export", files=snapshot_files(b"second\n"))
    assert tree_bytes(output) == before
    replace_snapshot(output, kind="publication-export", files=snapshot_files(b"second\n"))
    assert (output / "articles/part-1/publish.md").read_bytes() == b"second\n"


@pytest.mark.parametrize("crash_after_install", [False, True])
def test_interrupted_directory_switch_recovers_on_retry(tmp_path, monkeypatch, crash_after_install):
    output = tmp_path / "public"
    replace_snapshot(output, kind="publication-export", files=snapshot_files())
    real_replace = os.replace
    def interrupt(source, target):
        if str(source).endswith(".stage") and Path(target) == output:
            if crash_after_install:
                real_replace(source, target)
            raise KeyboardInterrupt("simulated process interruption")
        return real_replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            replace_snapshot(output, kind="publication-export", files=snapshot_files(b"second\n"))
    assert export_snapshot._journal_path(output).is_file()
    replace_snapshot(output, kind="publication-export", files=snapshot_files(b"third\n"))
    assert (output / "articles/part-1/publish.md").read_bytes() == b"third\n"
    assert not export_snapshot._journal_path(output).exists()
    assert not list(tmp_path.glob("*.backup")) and not list(tmp_path.glob("*.stage"))


def test_snapshot_concurrent_writer_is_refused(tmp_path):
    output = tmp_path / "public"
    with export_snapshot._exclusive_lock(output):
        with pytest.raises(ExportSnapshotError, match="another export"):
            replace_snapshot(output, kind="publication-export", files=snapshot_files())
    assert not output.exists()


def test_snapshot_staging_write_failure_preserves_old_output(tmp_path, monkeypatch):
    output = tmp_path / "public"
    replace_snapshot(output, kind="publication-export", files=snapshot_files())
    before = tree_bytes(output)
    real_write = export_snapshot._write_file
    def fail_write(path, data):
        if path.name == "publish.md":
            raise OSError("simulated disk failure")
        return real_write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot, "_write_file", fail_write)
        with pytest.raises(OSError, match="disk failure"):
            replace_snapshot(output, kind="publication-export", files=snapshot_files(b"new\n"))
    assert tree_bytes(output) == before
    assert not export_snapshot._journal_path(output).exists()


def test_partial_journal_write_is_removed_and_retry_succeeds(tmp_path, monkeypatch):
    output = tmp_path / "public"
    replace_snapshot(output, kind="publication-export", files=snapshot_files())
    before = tree_bytes(output)
    real_fsync = os.fsync
    real_open = Path.open
    journal_descriptor = None

    def track_journal(path, *args, **kwargs):
        nonlocal journal_descriptor
        stream = real_open(path, *args, **kwargs)
        if path.name == ".recovery-journal.tmp":
            journal_descriptor = stream.fileno()
        return stream

    def fail_journal_sync(descriptor):
        if descriptor == journal_descriptor:
            raise OSError("simulated journal sync failure")
        return real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", track_journal)
        patch.setattr(export_snapshot.os, "fsync", fail_journal_sync)
        with pytest.raises(OSError, match="journal sync failure"):
            replace_snapshot(output, kind="publication-export", files=snapshot_files(b"second\n"))
    assert tree_bytes(output) == before
    assert not export_snapshot._journal_path(output).exists()
    assert not list(tmp_path.glob("*.stage"))
    replace_snapshot(output, kind="publication-export", files=snapshot_files(b"third\n"))
    assert (output / "articles/part-1/publish.md").read_bytes() == b"third\n"


def test_interrupt_immediately_after_atomic_journal_install_recovers(tmp_path, monkeypatch):
    output = tmp_path / "public"
    replace_snapshot(output, kind="publication-export", files=snapshot_files())
    before = tree_bytes(output)
    real_replace = os.replace

    def install_then_interrupt(source, target):
        real_replace(source, target)
        if Path(target) == export_snapshot._journal_path(output):
            raise KeyboardInterrupt("simulated interruption after journal install")

    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "replace", install_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            replace_snapshot(output, kind="publication-export", files=snapshot_files(b"second\n"))
    assert tree_bytes(output) == before
    assert json.loads(export_snapshot._journal_path(output).read_bytes())["schemaVersion"] == 1
    replace_snapshot(output, kind="publication-export", files=snapshot_files(b"third\n"))
    assert (output / "articles/part-1/publish.md").read_bytes() == b"third\n"
    assert not export_snapshot._journal_path(output).exists()


def test_unmanaged_lock_and_recovery_journal_are_preserved(tmp_path):
    output = tmp_path / "public"
    lock = tmp_path / ".public.export.lock"
    lock.write_bytes(b"a user's own file")
    with pytest.raises(ValueError, match="lock file is unmanaged"):
        replace_snapshot(output, kind="publication-export", files=snapshot_files())
    assert lock.read_bytes() == b"a user's own file"
    lock.unlink()
    journal = export_snapshot._journal_path(output)
    journal.write_bytes(b'{"token":"../../outside"}')
    with pytest.raises(ValueError, match="recovery journal"):
        replace_snapshot(output, kind="publication-export", files=snapshot_files())
    assert journal.read_bytes() == b'{"token":"../../outside"}' and not output.exists()


def test_catalog_schema_id_patterns_match_actual_exports(manuscript, tmp_path):
    connection, root, revision_id, edition = manuscript
    release = publish(connection, root, edition)
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    schemas = Path(__file__).parents[1] / "docs" / "contracts"
    schema = json.loads((schemas / "publication-catalog.schema.json").read_text(encoding="utf-8"))
    entry = catalog(output)["articles"][0]
    definitions = schema["$defs"]["article"]["properties"]
    for key in ("editionId", "releaseId", "aiRevisionId", "contentSha256", "artifactSha256", "reviewArtifactSha256"):
        rule = definitions[key]
        if "$ref" in rule:
            rule = schema["$defs"][rule["$ref"].rsplit("/", 1)[-1]]
        assert re.fullmatch(rule["pattern"], entry[key]), key
    assert entry["editionId"] == edition["edition_id"] and entry["releaseId"] == release["release_id"]
    review_output = tmp_path / "review"
    export_editorial(connection, revision_id=revision_id, edition_id=edition["edition_id"], artifact_roots=(root,), output=review_output)
    review_schema = json.loads((schemas / "editorial-review.schema.json").read_text(encoding="utf-8"))
    review = json.loads((review_output / "review.json").read_text(encoding="utf-8"))
    assert re.fullmatch(review_schema["$defs"]["editionId"]["pattern"], review["editionId"])
    assert set(entry) == set(schema["$defs"]["article"]["required"])


@pytest.mark.parametrize("defect", ["legacy", "private", "extra-field", "boolean-id", "source-url", "catalog-hash",
                                   "releaseId", "editionId", "aiRevisionId", "contentSha256"])
def test_strict_catalog_contract_rejects_invalid_generated_snapshot(tmp_path, defect):
    files = snapshot_files()
    document = json.loads(files["catalog.json"])
    if defect == "legacy":
        document = document["articles"]
    elif defect == "private":
        document["manuscriptType"] = "editorial-review"
    elif defect == "extra-field":
        document["articles"][0]["reviewMetadata"] = "review.json"
    elif defect == "boolean-id":
        document["articles"][0]["videoPartId"] = True
    elif defect == "source-url":
        document["articles"][0]["sourceUrl"] = "https://www.bilibili.com/video/BVother/?p=1"
    elif defect == "catalog-hash":
        document["articles"][0]["artifactSha256"] = "f" * 64
    elif defect in {"releaseId", "editionId", "aiRevisionId", "contentSha256"}:
        document["articles"][0][defect] += "\n"
    files["catalog.json"] = json_bytes(document)
    output = tmp_path / "public"
    with pytest.raises(ValueError):
        replace_snapshot(output, kind="publication-export", files=files)
    assert not output.exists()


def test_old_database_contract_is_rejected_before_output_creation(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE reading_publications (revision_id TEXT)")
    output = tmp_path / "public"
    with pytest.raises(SchemaContractError, match="manuscript-schema-contract"):
        export_publications(connection, artifact_roots=(), output=output)
    assert not output.exists()
