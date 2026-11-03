"""Explicit reader drafts, release-history exclusions and managed snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import pytest

from bili_asr import cli, export_snapshot
from bili_asr.cli.parser import build_parser
from bili_asr.cli.publication import archive_connection
from bili_asr.export_snapshot import ExportSnapshotError, json_bytes, replace_snapshot
from bili_asr.publication import (
    create_edition, edit_edition, publish_edition,
    render_publication, review_edition, withdraw_release,
)
from bili_asr.publication_export import export_editorial, export_publication_drafts, export_publications
from tests.test_publication import approve, seeded_publication
from tests.support.content_permissions import grant_test_permission


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


@pytest.fixture
def draft_archive(tmp_path):
    root = tmp_path / "archive"
    connection, revision, roots = seeded_publication(root)
    edition = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="private-test-editor")
    grant_test_permission(connection, edition)
    try:
        yield connection, root, revision, edition
    finally:
        connection.close()


def catalog(output):
    return json.loads((output / "catalog.json").read_text(encoding="utf-8"))


def tree_bytes(output):
    return {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def change_review(connection, edition, status):
    if status == "pending-review":
        return
    review_edition(connection, edition_id=edition["edition_id"], status="in-review",
                   content_sha256=edition["content_sha256"], expected_status="pending-review", actor="private-reviewer")
    if status != "in-review":
        review_edition(connection, edition_id=edition["edition_id"], status=status,
                       content_sha256=edition["content_sha256"], expected_status="in-review", actor="private-reviewer",
                       note="private review note", issue_url="https://github.com/example/private/issues/1")


@pytest.mark.parametrize("status", ["pending-review", "in-review", "changes-requested", "approved", "rejected"])
def test_reader_draft_contract_contains_complete_content_and_review_status(draft_archive, tmp_path, status):
    connection, root, revision, original = draft_archive
    edition = edit_edition(connection, edition_id=original["edition_id"], markdown_text="读者可见正文。\n",
                           metadata={"title": "草稿标题", "summary": "读者摘要", "tags": ["草稿"],
                                     "attribution": "整理声明", "editorNote": "编辑说明"},
                           actor="private-test-editor", note="private editorial note")
    grant_test_permission(connection, edition)
    change_review(connection, edition, status)
    output = tmp_path / "drafts"
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 1
    document = catalog(output)
    assert document["schemaVersion"] == 2 and document["manuscriptType"] == "publication-draft"
    entry = document["articles"][0]
    assert entry["reviewStatus"] == status
    assert entry["editionId"] == edition["edition_id"] and entry["aiRevisionId"] == revision
    assert entry["slug"] == f"edition-{edition['edition_id']}"
    assert entry["file"] == f"drafts/{entry['slug']}/preview.md"
    rendered = (output / entry["file"]).read_bytes()
    assert rendered == render_publication(edition["content"])
    assert entry["artifactSha256"] == hashlib.sha256(rendered).hexdigest()
    assert entry["contentSha256"] == edition["content_sha256"]
    review = (output / entry["reviewFile"]).read_bytes()
    assert entry["reviewFile"] == f"drafts/{entry['slug']}/review.md"
    assert review == (root / f"documents/part-1/{revision}/ai-draft-v1/review.md").read_bytes()
    assert entry["reviewArtifactSha256"] == hashlib.sha256(review).hexdigest()
    schema = json.loads((Path(__file__).parents[1] / "docs/contracts/publication-draft-catalog.schema.json").read_text(encoding="utf-8"))
    assert set(entry) == set(schema["$defs"]["article"]["required"])
    for key, rule in schema["$defs"]["article"]["properties"].items():
        if "$ref" in rule:
            rule = schema["$defs"][rule["$ref"].rsplit("/", 1)[1]]
        if "pattern" in rule:
            assert re.fullmatch(rule["pattern"], entry[key]), key
    assert set(tree_bytes(output)) == {"catalog.json", "content-rights.json", entry["file"], entry["reviewFile"], "publication-draft-export-manifest.json"}
    manifest = json.loads((output / "publication-draft-export-manifest.json").read_text(encoding="utf-8"))
    manifest_schema = json.loads((Path(__file__).parents[1] / "docs/contracts/publication-draft-export-manifest.schema.json").read_text(encoding="utf-8"))
    assert set(manifest) == set(manifest_schema["required"])
    assert manifest["manuscriptType"] == "publication-draft-export"
    assert re.fullmatch(manifest_schema["properties"]["snapshotId"]["pattern"], manifest["snapshotId"])
    for item in manifest["files"]:
        assert re.fullmatch(manifest_schema["properties"]["files"]["items"]["properties"]["path"]["pattern"], item["path"])
        assert item["sha256"] == hashlib.sha256((output / item["path"]).read_bytes()).hexdigest()
    assert not any(word in json.dumps(document, ensure_ascii=False) + rendered.decode() for word in
                   ("private-reviewer", "private review note", "private editorial note", "promptSha256", "modelConfig"))
    assert export_publications(connection, artifact_roots=(root,), output=tmp_path / "public") == 0


def test_only_current_never_released_editions_are_exported_and_old_files_are_removed(draft_archive, tmp_path):
    connection, root, _, a = draft_archive
    output = tmp_path / "drafts"
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 1
    old_file = catalog(output)["articles"][0]["file"]
    old_review_file = catalog(output)["articles"][0]["reviewFile"]
    approve(connection, a)
    release_a = publish_edition(connection, edition_id=a["edition_id"], artifact_roots=(root,), write_root=root, actor="publisher")
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 0
    assert not (output / old_file).exists()
    assert not (output / old_review_file).exists()
    b = edit_edition(connection, edition_id=a["edition_id"], markdown_text="下一稿", actor="editor", note="next draft")
    grant_test_permission(connection, b)
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 1
    assert catalog(output)["articles"][0]["editionId"] == b["edition_id"]
    public = tmp_path / "public"
    assert export_publications(connection, artifact_roots=(root,), output=public) == 1
    public_entry = catalog(public)["articles"][0]
    assert public_entry["editionId"] == a["edition_id"] != b["edition_id"]
    assert public_entry["releaseId"] == release_a["release_id"]
    approve(connection, b)
    release_b = publish_edition(connection, edition_id=b["edition_id"], artifact_roots=(root,), write_root=root,
                                actor="publisher", expected_release_id=release_a["release_id"])
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 0
    assert export_publications(connection, artifact_roots=(root,), output=public) == 1
    assert catalog(public)["articles"][0]["editionId"] == b["edition_id"]
    withdraw_release(connection, release_id=release_b["release_id"], actor="publisher", note="withdraw")
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 0
    # Even a manually repointed old draft head must not expose a superseded or
    # withdrawn edition through the explicit draft channel.
    with connection:
        connection.execute("UPDATE publication_heads SET current_edition_id = ?", (a["edition_id"],))
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 0
    assert set(tree_bytes(output)) == {"catalog.json", "publication-draft-export-manifest.json"}


@pytest.mark.parametrize("defect", ["head-missing", "head-invalid", "review-missing", "content-hash", "ai-bytes", "ai-role"])
def test_corrupt_inputs_fail_without_replacing_previous_draft_snapshot(draft_archive, tmp_path, defect):
    connection, root, _, edition = draft_archive
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    connection.execute("PRAGMA foreign_keys = OFF")
    with connection:
        if defect == "head-missing":
            connection.execute("DELETE FROM publication_heads")
        elif defect == "head-invalid":
            connection.execute("UPDATE publication_heads SET current_edition_id = ?", ("f" * 32,))
        elif defect == "review-missing":
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'immutable_publication_reviews_delete'").fetchone()[0]
            connection.execute("DROP TRIGGER immutable_publication_reviews_delete")
            connection.execute("DELETE FROM publication_edition_reviews")
            connection.execute(trigger)
        elif defect == "content-hash":
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'immutable_publication_editions_update'").fetchone()[0]
            connection.execute("DROP TRIGGER immutable_publication_editions_update")
            connection.execute("UPDATE publication_editions SET content_sha256 = ?", ("f" * 64,))
            connection.execute(trigger)
        elif defect == "ai-role":
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE document_artifacts SET manuscript_role = 'other' WHERE artifact_name = 'ai-draft.md'")
    connection.execute("PRAGMA foreign_keys = ON")
    if defect == "ai-bytes":
        (root / f"documents/part-1/{edition['revision_id']}/ai-draft-v1/ai-draft.md").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


def test_explicit_artifact_root_is_used_for_both_baseline_documents(draft_archive, tmp_path):
    connection, root, _, edition = draft_archive
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (root / "documents").rename(artifacts / "documents")
    output = tmp_path / "drafts"
    assert export_publication_drafts(connection, artifact_roots=(artifacts,), output=output) == 1
    assert catalog(output)["articles"][0]["editionId"] == edition["edition_id"]


def test_cleared_release_head_is_rejected_before_reader_draft_snapshot(draft_archive, tmp_path):
    connection, root, _, edition = draft_archive
    approve(connection, edition)
    publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=(root,), write_root=root, actor="publisher")
    edit_edition(connection, edition_id=edition["edition_id"], markdown_text="下一稿", actor="editor", note="new draft")
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    with connection:
        connection.execute("UPDATE publication_heads SET current_release_id = NULL")
    with pytest.raises(ExportSnapshotError, match="matching current publication head"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


def test_linked_draft_output_is_rejected_without_touching_the_target(draft_archive, tmp_path):
    connection, root, _, _ = draft_archive
    target = tmp_path / "target"
    target.mkdir()
    (target / "personal.md").write_text("Keep this file.", encoding="utf-8")
    output = tmp_path / "linked"
    try:
        output.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    before = tree_bytes(target)
    with pytest.raises(ExportSnapshotError, match="link or reparse"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(target) == before


@pytest.mark.parametrize("existing", ["publication", "editorial", "manual"])
def test_draft_output_never_reuses_other_snapshot_kinds_or_user_files(draft_archive, tmp_path, existing):
    connection, root, revision, edition = draft_archive
    output = tmp_path / "output"
    if existing == "publication":
        export_publications(connection, artifact_roots=(root,), output=output)
    elif existing == "editorial":
        export_editorial(connection, revision_id=revision, edition_id=edition["edition_id"], artifact_roots=(root,), output=output)
    else:
        output.mkdir()
        (output / "personal.md").write_text("Keep this file.", encoding="utf-8")
    before = tree_bytes(output)
    with pytest.raises(ExportSnapshotError, match="managed publication-draft-export"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


@pytest.mark.parametrize("other", ["publication", "editorial"])
def test_other_export_kinds_cannot_replace_reader_draft_output(draft_archive, tmp_path, other):
    connection, root, revision, edition = draft_archive
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    with pytest.raises(ExportSnapshotError):
        if other == "publication":
            export_publications(connection, artifact_roots=(root,), output=output)
        else:
            export_editorial(connection, revision_id=revision, edition_id=edition["edition_id"], artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


def test_draft_snapshot_install_failure_rolls_back_and_retry_removes_stale_files(draft_archive, tmp_path, monkeypatch):
    connection, root, _, edition = draft_archive
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    updated = edit_edition(connection, edition_id=edition["edition_id"], markdown_text="更新正文", actor="editor", note="update")
    grant_test_permission(connection, updated)
    original_replace = export_snapshot.os.replace

    def fail_install(source, destination):
        if str(source).endswith(".stage") and Path(destination) == output:
            raise OSError("injected draft install failure")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(export_snapshot.os, "replace", fail_install)
        with pytest.raises(OSError, match="injected"):
            export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before
    assert export_publication_drafts(connection, artifact_roots=(root,), output=output) == 1
    assert catalog(output)["articles"][0]["editionId"] == updated["edition_id"]
    assert not (output / f"drafts/edition-{edition['edition_id']}/preview.md").exists()
    assert not (output / f"drafts/edition-{edition['edition_id']}/review.md").exists()


def test_draft_export_rejects_source_overlap_and_concurrent_output_lock(draft_archive, tmp_path):
    connection, root, _, _ = draft_archive
    with pytest.raises(ExportSnapshotError, match="overlaps"):
        export_publication_drafts(connection, artifact_roots=(root,), output=root / "drafts")
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    before = tree_bytes(output)
    with export_snapshot._exclusive_lock(output):
        with pytest.raises(ExportSnapshotError, match="holds the output lock"):
            export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert tree_bytes(output) == before


@pytest.mark.parametrize("defect", ["private-field", "release-field", "wrong-type", "wrong-status", "wrong-path", "wrong-hash"])
def test_strict_draft_snapshot_contract_rejects_unwanted_or_invalid_fields(draft_archive, tmp_path, defect):
    connection, root, _, _ = draft_archive
    source = tmp_path / "source"
    export_publication_drafts(connection, artifact_roots=(root,), output=source)
    files = tree_bytes(source)
    files.pop("publication-draft-export-manifest.json")
    document = json.loads(files["catalog.json"])
    article = document["articles"][0]
    if defect == "private-field":
        article["reviewMetadata"] = "review.json"
    elif defect == "release-field":
        article["releaseId"] = "f" * 64
    elif defect == "wrong-type":
        document["manuscriptType"] = "publication"
    elif defect == "wrong-status":
        article["reviewStatus"] = "published"
    elif defect == "wrong-path":
        article["file"] = "../private.md"
    else:
        article["artifactSha256"] = "f" * 64
    files["catalog.json"] = json_bytes(document)
    output = tmp_path / "bad-output"
    with pytest.raises(ExportSnapshotError):
        replace_snapshot(output, kind="publication-draft-export", files=files)
    assert not output.exists()


def test_export_drafts_cli_uses_readonly_connection_and_explicit_artifact_root(draft_archive, tmp_path, capsys, monkeypatch):
    connection, root, _, edition = draft_archive
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (root / "documents").rename(artifacts / "documents")
    before = (root / "archive.db").read_bytes()
    original = archive_connection
    observed = []

    def capture_connection(archive_root, *, readonly=True):
        active = original(archive_root, readonly=readonly)
        observed.append((readonly, active.execute("PRAGMA query_only").fetchone()[0]))
        return active

    import bili_asr.cli.publication as commands
    monkeypatch.setattr(commands, "archive_connection", capture_connection)
    output = tmp_path / "reader-drafts"
    argv = ["publication", "export-drafts", "--archive-root", str(root), "--artifact-root", str(artifacts),
            "--out", str(output), "--format", "json"]
    parsed = build_parser().parse_args(argv)
    assert parsed.publication_action == "export-drafts"
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"manuscriptType": "publication-draft", "count": 1, "output": str(output)}
    assert observed == [(True, 1)]
    assert (root / "archive.db").read_bytes() == before
    assert catalog(output)["articles"][0]["editionId"] == edition["edition_id"]


def test_export_drafts_cli_missing_archive_is_not_created(tmp_path, capsys):
    missing = tmp_path / "missing"
    output = tmp_path / "drafts"
    assert cli.main(["publication", "export-drafts", "--archive-root", str(missing), "--out", str(output)]) == 1
    assert "no archive database" in capsys.readouterr().err
    assert not missing.exists() and not output.exists()
