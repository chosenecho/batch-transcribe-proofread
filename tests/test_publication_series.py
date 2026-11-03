"""Synthetic editor-confirmed relations, never real production associations."""

import copy
import hashlib
import json

import pytest

from bili_asr.cli import main
from bili_asr.export_snapshot import ExportSnapshotError, json_bytes, replace_snapshot
from bili_asr.publication import create_edition, withdraw_release
from bili_asr.editorial import EditorialConfig
from bili_asr.storage import open_database
from bili_asr.publication_export import export_publications, export_publication_drafts
from bili_asr.publication_series import (edit_series, editorial_version, project_series, read_series,
                                         validate_editorial_series, validate_public_series)
from tests.test_publication_export import publish, tree_bytes
from tests.support.content_permissions import grant_test_permission
from tests.test_ai_editorial import FakeClient, insert_record, record, runtime


@pytest.fixture
def manuscript(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    with connection:
        connection.execute("INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) VALUES (1, 'synthetic', 1, 1)")
        connection.execute("INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) VALUES ('BV1xx411c7mD', 1, 1, 'synthetic', 1, 1, 1)")
        connection.execute("INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title, duration_ms, processing_status, created_at, updated_at) VALUES (1, 'BV1xx411c7mD', 0, 1, 'synthetic p0', 1000, 'metadata_collected', 1, 1)")
    insert_record(connection, record())
    workflow, repository, _, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    jobs = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision_id = repository.revision_for_job(jobs[0])
    edition = create_edition(connection, revision_id=revision_id, artifact_roots=(root,), actor="synthetic-editor")
    grant_test_permission(connection, edition, (root,))
    try:
        yield connection, root, revision_id, edition
    finally:
        connection.close()


def source():
    return {"schemaVersion": 1, "updatedBy": "synthetic-editor-private", "series": [{
        "id": "synthetic-course", "title": "合成系列，不表示实际内容关系", "sourceUrl": "https://example.org/editor-confirmation",
        "evidence": "合成测试：编辑已明确确认顺序", "members": [
            {"ordinal": 1, "bvid": "BV1xx411c7mD", "label": "合成第一讲"},
            {"ordinal": 3, "bvid": "BV1xx411c7mE", "label": "合成第三讲"}],
        "knownMissing": [{"ordinal": 2, "label": "合成第二讲", "note": "仅合成测试明确确认缺失"}]}]}


def write(path, value=None):
    path.write_bytes(json_bytes(value or source()))
    return path


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(private=True), lambda v: v.update(schemaVersion=True),
    lambda v: v["series"][0].update(actor="leak"),
    lambda v: v["series"][0].update(id="../unsafe"),
    lambda v: v["series"][0].update(sourceUrl="javascript:alert(1)"),
    lambda v: v["series"][0].update(sourceUrl="https://editor:secret@example.org/"),
    lambda v: v["series"][0].update(sourceUrl="https://example.org:bad/"),
    lambda v: v["series"][0].update(sourceUrl="https://example.org/#"),
    lambda v: v["series"][0].update(sourceUrl="HTTPS://example.org/"),
    lambda v: v["series"][0].update(evidence=" "),
    lambda v: v["series"][0]["members"][0].update(ordinal=True),
    lambda v: v["series"][0]["members"][1].update(ordinal=9007199254740992),
    lambda v: v["series"][0]["members"][1].update(ordinal=1),
    lambda v: v["series"][0]["members"][1].update(bvid="BV1xx411c7mD"),
    lambda v: v["series"][0]["members"][0].update(bvid="BV/unsafe"),
    lambda v: v["series"][0]["members"][0].update(entries=[]),
    lambda v: v["series"][0]["knownMissing"][0].update(ordinal=1),
    lambda v: v["series"].append(copy.deepcopy(v["series"][0])),
])
def test_editorial_contract_rejects_ambiguous_or_private_fields(mutation):
    value = source()
    mutation(value)
    with pytest.raises(ExportSnapshotError):
        validate_editorial_series(value)


@pytest.mark.parametrize("body", [b'{"schemaVersion":1,"schemaVersion":1}', b'\xff', b'{"schemaVersion":NaN}',
                                  b'{"schemaVersion":1,"updatedBy":"e","series":[{"id":"a","id":"a"}]}'])
def test_file_rejects_duplicate_keys_invalid_utf8_and_nonfinite_json(tmp_path, body):
    path = tmp_path / "invalid.json"
    path.write_bytes(body)
    with pytest.raises(ExportSnapshotError):
        read_series(path)


def test_atomic_edit_compare_and_swap_and_cli_without_database(tmp_path, capsys):
    input_path = write(tmp_path / "input.json")
    output = tmp_path / "editorial.json"
    args = ["publication", "series", "edit", "--input", str(input_path), "--out", str(output),
            "--actor", "private-actor", "--expected-sha256", "new"]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert read_series(output)["updatedBy"] == "private-actor"
    before = output.read_bytes()
    assert main(args) == 1
    assert "version conflict" in capsys.readouterr().err
    assert output.read_bytes() == before
    assert main(args[:-1] + [result["sha256"]]) == 0
    capsys.readouterr()
    assert main(["publication", "series", "show", "--series-file", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["content"] == read_series(output)
    assert main(["publication", "series", "validate", "--series-file", str(output)]) == 0
    capsys.readouterr()
    assert not (tmp_path / "archive.db").exists()


def test_failed_replace_preserves_original_editorial_file(tmp_path, monkeypatch):
    import bili_asr.publication_series as module
    from bili_asr import file_ops
    original = write(tmp_path / "original.json")
    before = original.read_bytes()
    replacement = write(tmp_path / "input.json")
    syncs = []
    monkeypatch.setattr(module, "sync_directory", syncs.append)
    monkeypatch.setattr(file_ops.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic failure")))
    with pytest.raises(OSError):
        edit_series(replacement, original, actor="new editor", expected_sha256=hashlib.sha256(before).hexdigest())
    assert original.read_bytes() == before
    assert syncs == []
    assert {p.name for p in tmp_path.glob(".original.json.*")} == {".original.json.export.lock"}


def test_series_edit_syncs_replaced_parent_while_lock_is_held(tmp_path, monkeypatch):
    import bili_asr.publication_series as module
    from bili_asr.export_snapshot import _exclusive_lock
    input_path = write(tmp_path / "input.json")
    output = tmp_path / "series.json"
    calls = []

    def sync_parent(parent):
        assert parent == output.parent
        assert read_series(output)["updatedBy"] == "new editor"
        with pytest.raises(ExportSnapshotError, match="holds the output lock"):
            with _exclusive_lock(output):
                pytest.fail("series edit released its lock before directory sync")
        calls.append(parent)

    monkeypatch.setattr(module, "sync_directory", sync_parent)
    result = edit_series(input_path, output, actor="new editor", expected_sha256="new")
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert calls == [output.parent]


def test_directory_sync_failure_does_not_report_series_edit_success(tmp_path, monkeypatch, capsys):
    import bili_asr.publication_series as module
    input_path = write(tmp_path / "input.json")
    output = tmp_path / "series.json"

    def fail_sync(parent):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(module, "sync_directory", fail_sync)
    assert main(["publication", "series", "edit", "--input", str(input_path), "--out", str(output),
                 "--actor", "new editor", "--expected-sha256", "new"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "synthetic directory sync failure" in captured.err
    # The replace already happened. A retry must reload its current version.
    assert read_series(output)["updatedBy"] == "new editor"
    assert {p.name for p in tmp_path.glob(".series.json.*")} == {".series.json.export.lock"}


def test_projection_requires_all_exact_parts_in_category_and_version():
    value = source()
    articles = [{"bvid": "BV1xx411c7mD", "pageIndex": p, "slug": f"part-{p+1}", "editionId": str(p)*32,
                 "contentSha256": "a"*64, "artifactSha256": "b"*64} for p in (1, 0)]
    public = project_series(value, articles, "publication")
    assert public["series"][0]["members"][0]["entries"][0]["slug"] == "part-1"
    assert public["series"][0]["members"][1]["entries"] == []
    assert "updatedBy" not in json.dumps(public)
    assert validate_public_series(public, articles, "publication") == public
    for mutation in (
        lambda v: v["series"][0]["members"][0]["entries"].pop(),
        lambda v: v["series"][0]["members"][0]["entries"].reverse(),
        lambda v: v["series"][0]["members"][0]["entries"][0].update(editionId="e"*32),
        lambda v: v.update(manuscriptType="publication-draft"),
        lambda v: v["series"][0]["members"][0].update(secret="private"),
    ):
        altered = copy.deepcopy(public)
        mutation(altered)
        with pytest.raises(ExportSnapshotError):
            validate_public_series(altered, articles, "publication")
    normalized = source()
    normalized["series"][0]["title"] = "  " + normalized["series"][0]["title"] + " "
    assert editorial_version(value) == editorial_version(normalized)


def test_universal_bilibili_projection_does_not_bind_other_provider():
    entries = [{"platform": platform, "externalVideoId": "BV1xx411c7mD", "partIndex": 0,
                "slug": "part-1", "editionId": "a"*32, "contentSha256": "b"*64, "artifactSha256": "c"*64}
               for platform in ("youtube", "bilibili")]
    assert len(project_series(source(), entries, "publication")["series"][0]["members"][0]["entries"]) == 1


def test_ordinal_largest_js_safe_integer_is_supported():
    value = source()
    value["series"][0]["members"][1]["ordinal"] = 9007199254740991
    assert validate_editorial_series(value)["series"][0]["members"][1]["ordinal"] == 9007199254740991


def test_optional_series_export_release_withdrawal_and_failed_export_preserves_snapshot(manuscript, tmp_path):
    connection, root, _, edition = manuscript
    release = publish(connection, root, edition)
    value = source()
    value["series"][0]["members"][0]["bvid"] = edition["content"]["source"]["bvid"]
    editorial = write(tmp_path / "series-editorial.json", value)
    output = tmp_path / "public"
    export_publications(connection, artifact_roots=(root,), output=output)
    old_catalog = (output / "catalog.json").read_bytes()
    assert not (output / "series.json").exists()
    export_publications(connection, artifact_roots=(root,), output=output, series_file=editorial)
    assert (output / "catalog.json").read_bytes() == old_catalog
    public = json.loads((output / "series.json").read_bytes())
    assert len(public["series"][0]["members"][0]["entries"]) == 1
    assert b"synthetic-editor-private" not in (output / "series.json").read_bytes()
    manifest = json.loads((output / "publication-export-manifest.json").read_bytes())
    assert next(r for r in manifest["files"] if r["path"] == "series.json")["sha256"] == hashlib.sha256((output / "series.json").read_bytes()).hexdigest()
    before = tree_bytes(output)
    editorial.write_text("invalid", encoding="utf-8")
    with pytest.raises(ExportSnapshotError):
        export_publications(connection, artifact_roots=(root,), output=output, series_file=editorial)
    assert tree_bytes(output) == before
    write(editorial, value)
    withdraw_release(connection, release_id=release["release_id"], actor="test", note="synthetic withdrawal")
    export_publications(connection, artifact_roots=(root,), output=output, series_file=editorial)
    assert json.loads((output / "series.json").read_bytes())["series"][0]["members"][0]["entries"] == []


def test_draft_binding_and_tamper_refusal(manuscript, tmp_path):
    connection, root, _, edition = manuscript
    value = source()
    value["series"][0]["members"][0]["bvid"] = edition["content"]["source"]["bvid"]
    editorial = write(tmp_path / "series-editorial.json", value)
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output, series_file=editorial)
    public = json.loads((output / "series.json").read_bytes())
    assert public["manuscriptType"] == "publication-draft"
    assert public["series"][0]["members"][0]["entries"][0]["editionId"] == edition["edition_id"]
    files = {name: body for name, body in tree_bytes(output).items() if not name.endswith("-manifest.json")}
    public["series"][0]["members"][0]["entries"][0]["artifactSha256"] = "f"*64
    files["series.json"] = json_bytes(public)
    before = tree_bytes(output)
    with pytest.raises(ExportSnapshotError, match="bindings"):
        replace_snapshot(output, kind="publication-draft-export", files=files)
    assert tree_bytes(output) == before
    (output / "series.json").write_bytes(b"corruption")
    with pytest.raises(ExportSnapshotError, match="modified"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output, series_file=editorial)


def test_explicit_empty_series_legacy_export_and_source_overlap(manuscript, tmp_path):
    connection, root, _, _ = manuscript
    editorial = tmp_path / "empty.json"
    editorial.write_bytes(json_bytes({"schemaVersion": 1, "updatedBy": "synthetic-editor", "series": []}))
    output = tmp_path / "drafts"
    export_publication_drafts(connection, artifact_roots=(root,), output=output, series_file=editorial)
    assert json.loads((output / "series.json").read_bytes())["series"] == []
    before = tree_bytes(output)
    with pytest.raises(ExportSnapshotError, match="inside"):
        export_publication_drafts(connection, artifact_roots=(root,), output=output, series_file=output / "series.json")
    assert tree_bytes(output) == before
    export_publication_drafts(connection, artifact_roots=(root,), output=output)
    assert not (output / "series.json").exists()
    assert len(json.loads((output / "catalog.json").read_bytes())["articles"]) == 1


def test_concurrent_series_editor_refused_without_changing_source(tmp_path):
    from bili_asr.export_snapshot import _exclusive_lock
    editorial = write(tmp_path / "series.json")
    before = editorial.read_bytes()
    with _exclusive_lock(editorial):
        with pytest.raises(ExportSnapshotError, match="holds the output lock"):
            edit_series(editorial, editorial, actor="concurrent-editor", expected_sha256=hashlib.sha256(before).hexdigest())
    assert editorial.read_bytes() == before


def test_symlink_editorial_source_is_refused(tmp_path):
    original = write(tmp_path / "source.json")
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    with pytest.raises(ExportSnapshotError, match="link or reparse"):
        read_series(linked)


@pytest.mark.parametrize("action,category", [("export", "publication"), ("export-drafts", "publication-draft")])
def test_cli_explicit_series_source_exports_same_validated_contract(manuscript, tmp_path, capsys, action, category):
    connection, root, _, edition = manuscript
    if action == "export":
        publish(connection, root, edition)
    editorial = write(tmp_path / "editorial.json")
    output = tmp_path / "output"
    assert main(["publication", action, "--archive-root", str(root), "--out", str(output),
                 "--series-file", str(editorial), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    public = json.loads((output / "series.json").read_bytes())
    catalog = json.loads((output / "catalog.json").read_bytes())
    assert validate_public_series(public, catalog["articles"], category) == public
