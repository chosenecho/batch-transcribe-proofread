"""Version 2 reader catalogs pair unchanged AI review references with each text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bili_asr import cli
from bili_asr.editorial import EditorialConfig
from bili_asr.export_snapshot import ExportSnapshotError, _snapshot_id, json_bytes, replace_snapshot
from bili_asr.publication import (
    create_edition, edit_edition, get_ai_artifacts, get_edition, publish_edition,
    withdraw_release,
)
from bili_asr.publication_export import export_publication_drafts, export_publications
from tests.test_ai_editorial import FakeClient, runtime
from tests.test_publication import approve, seeded_publication


pytestmark = pytest.mark.usefixtures('enable_content_publication_for_tests')


def catalog(output):
    return json.loads((output / "catalog.json").read_bytes())


def files(output):
    return {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}


@pytest.fixture(params=["publication", "publication-draft"])
def reader(request, tmp_path):
    root = tmp_path / "archive"
    connection, revision, roots = seeded_publication(root)
    from bili_asr.storage.publication_packages import install_packages
    install_packages(connection)
    initial = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="editor")
    edition = edit_edition(connection, edition_id=initial["edition_id"], markdown_text="Manual reader edit.\n",
                           actor="editor", note="Private edit reason")
    exporter = export_publications if request.param == "publication" else export_publication_drafts
    from tests.support.content_permissions import grant_test_permission
    grant_test_permission(connection, edition)
    if request.param == "publication":
        approve(connection, edition)
        publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=roots,
                        write_root=root, actor="publisher")
    output = tmp_path / "reader"
    try:
        yield connection, root, revision, edition, exporter, output, request.param
    finally:
        connection.close()


def test_reader_exports_original_review_bytes_and_preserves_all_database_state(reader):
    connection, root, revision, edition, exporter, output, manuscript_type = reader
    expected_review = get_ai_artifacts(connection, revision, (root,))["review.md"]
    before = (root / "archive.db").read_bytes()
    state = get_edition(connection, edition["edition_id"])
    assert exporter(connection, artifact_roots=(root,), output=output) == 1
    assert (root / "archive.db").read_bytes() == before
    assert get_edition(connection, edition["edition_id"]) == state
    document = catalog(output)
    assert document["schemaVersion"] == 2 and document["manuscriptType"] == manuscript_type
    entry = document["articles"][0]
    review = (output / entry["reviewFile"]).read_bytes()
    assert review == expected_review
    assert b"Manual reader edit." not in review
    assert b"Manual reader edit." in (output / entry["file"]).read_bytes()
    assert entry["reviewArtifactSha256"] == hashlib.sha256(review).hexdigest()
    kind = f"{manuscript_type}-export"
    manifest = json.loads((output / f"{kind}-manifest.json").read_bytes())
    assert manifest["schemaVersion"] == 1
    paths = {record["path"] for record in manifest["files"]}
    expected = {"catalog.json", "content-rights.json", entry["file"], entry["reviewFile"]}
    if manuscript_type == "publication":
        expected.add("publication-products.json")
        assert any(path.startswith("transcripts/part-1/") for path in paths)
    assert expected <= paths
    assert not any(path.endswith(("review.json", "edition.json", ".patch")) for path in files(output))


@pytest.mark.parametrize("defect", ["review-missing", "review-bytes", "review-hash", "ai-counterpart"])
def test_missing_or_corrupt_review_pair_retains_previous_reader_snapshot(reader, defect):
    connection, root, revision, _, exporter, output, _ = reader
    exporter(connection, artifact_roots=(root,), output=output)
    before = files(output)
    pair_root = root / f"documents/part-1/{revision}/ai-draft-v1"
    if defect == "review-missing":
        (pair_root / "review.md").unlink()
    elif defect == "review-bytes":
        (pair_root / "review.md").write_bytes(b"wrong review bytes")
    elif defect == "review-hash":
        with connection:
            connection.execute("UPDATE document_artifacts SET content_sha256 = ? WHERE artifact_name = 'review.md'",
                               ("f" * 64,))
    else:
        (pair_root / "ai-draft.md").write_bytes(b"wrong counterpart bytes")
    with pytest.raises(ValueError):
        exporter(connection, artifact_roots=(root,), output=output)
    assert files(output) == before


@pytest.mark.parametrize("defect", ["version1", "missing-field", "missing-file", "wrong-path", "wrong-hash", "extra-private-file"])
def test_version2_requires_the_canonical_review_pair_and_refuses_version1(reader, tmp_path, defect):
    connection, root, _, _, exporter, output, manuscript_type = reader
    exporter(connection, artifact_roots=(root,), output=output)
    generated = files(output)
    kind = f"{manuscript_type}-export"
    generated.pop(f"{kind}-manifest.json")
    document = json.loads(generated["catalog.json"])
    entry = document["articles"][0]
    if defect == "version1":
        document["schemaVersion"] = 1
    elif defect == "missing-field":
        del entry["reviewArtifactSha256"]
    elif defect == "missing-file":
        generated.pop(entry["reviewFile"])
    elif defect == "wrong-path":
        entry["reviewFile"] = "articles/part-999/review.md" if manuscript_type == "publication" else f"drafts/edition-{'f' * 32}/review.md"
    elif defect == "wrong-hash":
        entry["reviewArtifactSha256"] = "f" * 64
    else:
        generated["review.json"] = b"{}"
    generated["catalog.json"] = json_bytes(document)
    invalid = tmp_path / "invalid-reader"
    with pytest.raises(ExportSnapshotError):
        replace_snapshot(invalid, kind=kind, files=generated)
    assert not invalid.exists()


@pytest.mark.parametrize("empty", [False, True])
def test_managed_version1_output_is_refused_without_migration_or_mutation(reader, empty):
    connection, root, _, _, exporter, output, manuscript_type = reader
    exporter(connection, artifact_roots=(root,), output=output)
    old = files(output)
    kind = f"{manuscript_type}-export"
    manifest_name = f"{kind}-manifest.json"
    document = json.loads(old["catalog.json"])
    document["schemaVersion"] = 1
    for entry in document["articles"]:
        (output / entry.pop("reviewFile")).unlink()
        entry.pop("reviewArtifactSha256")
    if empty:
        for entry in document["articles"]:
            path = output / entry["file"]
            path.unlink()
            path.parent.rmdir()
            path.parent.parent.rmdir()
        document["articles"] = []
    (output / "catalog.json").write_bytes(json_bytes(document))
    old = files(output)
    records = [{"path": path, "sha256": hashlib.sha256(content).hexdigest()}
               for path, content in sorted(old.items()) if path != manifest_name]
    (output / manifest_name).write_bytes(json_bytes({"schemaVersion": 1, "manuscriptType": kind,
                                                   "snapshotId": _snapshot_id(records), "files": records}))
    before = files(output)
    with pytest.raises(ExportSnapshotError, match="unsupported public catalog"):
        exporter(connection, artifact_roots=(root,), output=output)
    assert files(output) == before


def test_published_a_and_draft_b_use_reviews_from_their_exact_different_revisions(tmp_path):
    root = tmp_path / "archive"
    connection, revision_a, roots = seeded_publication(root)
    try:
        a = create_edition(connection, revision_id=revision_a, artifact_roots=roots, actor="editor")
        approve(connection, a)
        publish_edition(connection, edition_id=a["edition_id"], artifact_roots=roots, write_root=root, actor="publisher")
        workflow, repository, _, executor = runtime(connection, root, FakeClient())
        prepared = repository.prepare(1, None, EditorialConfig(top_p=1.0))
        proof_id, *_ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
        assert executor.run().succeeded == 2
        revision_b = repository.revision_for_job(proof_id)
        assert revision_a != revision_b
        b = create_edition(connection, revision_id=revision_b, artifact_roots=roots, actor="editor",
                           expected_edition_id=a["edition_id"])
        from tests.support.content_permissions import grant_test_permission
        grant_test_permission(connection, b)
        public, drafts = tmp_path / "public", tmp_path / "drafts"
        assert export_publications(connection, artifact_roots=roots, output=public) == 1
        assert export_publication_drafts(connection, artifact_roots=roots, output=drafts) == 1
        a_entry, b_entry = catalog(public)["articles"][0], catalog(drafts)["articles"][0]
        assert a_entry["editionId"] == a["edition_id"] and b_entry["editionId"] == b["edition_id"]
        a_review, b_review = (public / a_entry["reviewFile"]).read_bytes(), (drafts / b_entry["reviewFile"]).read_bytes()
        assert a_review == get_ai_artifacts(connection, revision_a, roots)["review.md"]
        assert b_review == get_ai_artifacts(connection, revision_b, roots)["review.md"]
        assert a_review != b_review
        assert a_entry["reviewArtifactSha256"] != b_entry["reviewArtifactSha256"]
        approve(connection, b)
        release_b = publish_edition(connection, edition_id=b["edition_id"], artifact_roots=roots, write_root=root,
                                     actor="publisher", expected_release_id=a_entry["releaseId"])
        assert export_publications(connection, artifact_roots=roots, output=public) == 1
        assert export_publication_drafts(connection, artifact_roots=roots, output=drafts) == 0
        assert (public / a_entry["reviewFile"]).read_bytes() == b_review
        assert files(drafts).keys() == {"catalog.json", "publication-draft-export-manifest.json"}
        withdraw_release(connection, release_id=release_b["release_id"], actor="publisher", note="withdraw test")
        assert export_publications(connection, artifact_roots=roots, output=public) == 0
        assert files(public).keys() == {"catalog.json", "publication-export-manifest.json"}
    finally:
        connection.close()


def test_cli_reader_export_returns_a_paired_version2_snapshot_without_approving(reader, capsys):
    connection, root, _, edition, _, output, manuscript_type = reader
    action = "export" if manuscript_type == "publication" else "export-drafts"
    before = get_edition(connection, edition["edition_id"])
    assert cli.main(["publication", action, "--archive-root", str(root), "--out", str(output), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert catalog(output)["schemaVersion"] == 2
    entry = catalog(output)["articles"][0]
    assert (output / entry["reviewFile"]).is_file()
    assert get_edition(connection, edition["edition_id"]) == before
