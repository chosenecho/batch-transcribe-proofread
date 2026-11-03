"""A handoff is checked once and needs no downstream publication lifecycle."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from bili_asr.cli import main
from bili_asr.delivery import (
    export_delivery,
    read_records,
    validate_record,
    verify_delivery,
)
from bili_asr.storage import open_database
from tests.test_ai_editorial import (
    EditorialConfig,
    FakeClient,
    insert_record,
    record,
    runtime,
)
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def produced(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    _seed_part(connection)
    insert_record(connection, record())
    workflow, repository, _, executor = runtime(connection, root, FakeClient())
    prepared = repository.prepare(1, None, EditorialConfig())
    jobs = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().succeeded == 2
    revision = repository.revision_for_job(jobs[0])
    try:
        yield connection, root, revision
    finally:
        connection.close()


def test_delivery_needs_no_edition_and_preserves_extensions_and_originals(produced, tmp_path):
    connection, root, revision = produced
    before = connection.total_changes
    extra = tmp_path / "user.json"
    extra.write_bytes(b'{"future-field":42}')
    output = tmp_path / "delivery"
    receipt = export_delivery(connection, output=output, artifact_roots=(root,), revision_ids=[revision],
        extensions={"knowledge": {"collection": "philosophy"}, "training_fields": ["later"]},
        attachments={"user-info.json": extra})
    assert connection.total_changes == before
    assert connection.execute("SELECT count(*) FROM publication_editions").fetchone()[0] == 0
    assert verify_delivery(output)["bundle_id"] == receipt["bundle_id"]
    rows = read_records(output / "records.jsonl")
    assert rows[0]["extensions"]["knowledge"] == {"collection": "philosophy"}
    assert rows[0]["quality"]["human_review_status"] == "not_reviewed"
    assert rows[0]["quality"]["semantic_status"] == "unverified"
    assert (output / rows[0]["provenance"]["review_file"]).is_file()
    assert (output / "attachments/user-info.json").read_bytes() == extra.read_bytes()
    assert json.loads((output / rows[0]["provenance"]["input_file"]).read_text("utf-8"))["snapshot"]["base"]["segments"]


def test_delivery_is_offline_checked_and_refuses_overwrite_or_corruption(produced, tmp_path, capsys):
    connection, root, revision = produced
    output = tmp_path / "delivery"
    export_delivery(connection, output=output, artifact_roots=(root,), revision_ids=[revision])
    assert main(["delivery", "check", "--directory", str(output)]) == 0
    original = (output / "records.jsonl").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        export_delivery(connection, output=output, artifact_roots=(root,), revision_ids=[revision])
    assert (output / "records.jsonl").read_bytes() == original
    (output / "records.jsonl").write_bytes(original + b" ")
    assert main(["delivery", "check", "--directory", str(output)]) == 1
    assert "identity mismatch" in capsys.readouterr().err


def test_unknown_fields_allowed_but_invalid_timeline_is_not(produced, tmp_path):
    connection, root, revision = produced
    output = tmp_path / "delivery"
    export_delivery(connection, output=output, artifact_roots=(root,), revision_ids=[revision])
    row = read_records(output / "records.jsonl")[0]
    row["future_training_annotation"] = {"argument": "pending", "new": 7}
    validate_record(row)
    bad = deepcopy(row)
    bad["segments"][1]["start_ms"] = -1
    with pytest.raises(ValueError, match="do not sort or clamp"):
        validate_record(bad)


def test_asr_export_preserves_measured_admission_and_rejects_missing_measurements(tmp_path):
    root = tmp_path / "archive"
    connection = open_database(root)
    _seed_part(connection)
    insert_record(connection, record(), measured=False)
    try:
        with pytest.raises(RuntimeError):
            export_delivery(connection, output=tmp_path / "bad", artifact_roots=(root,), transcript_ids=[1])
        assert not (tmp_path / "bad").exists()
    finally:
        connection.close()


def test_source_mutation_does_not_create_a_receipt(produced, tmp_path):
    connection, root, revision = produced
    with connection:
        connection.execute("UPDATE transcript_segments SET text='changed' WHERE transcript_id=1 AND ordinal=0")
    with pytest.raises(ValueError, match="differs from its transcript"):
        export_delivery(connection, output=tmp_path / "bad", artifact_roots=(root,), revision_ids=[revision])
    assert not (tmp_path / "bad").exists()


def test_delivery_discovery_is_machine_readable_and_not_an_approval(produced, capsys):
    _, root, revision = produced
    assert main(["delivery", "list", "--archive-root", str(root), "--part-id", "1", "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["revision_id"] == revision
    assert result["quality_checked"] is False and result["next_offset"] is None
    assert main(["delivery", "list", "--archive-root", str(root), "--kind", "transcript"]) == 0
    assert json.loads(capsys.readouterr().out)["records"][0]["transcript_id"] == 1


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_fresh_universal_archive_delivers_without_publication_state(tmp_path, platform):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.platform_identity import ContentRef
    from bili_asr.services.archive_migration import initialize_archive
    from bili_asr.storage.sources import SourceRepository, SourceVideoMetadata
    root = tmp_path / "archive"
    initialize_archive(root)
    with ArchiveSession(root, mode=ArchiveAccessMode.WRITE) as session:
        connection = session.connection
        if platform == "bilibili":
            _seed_part(connection)
            part_id = 1
        else:
            with connection:
                part_id = SourceRepository(connection).upsert_video(SourceVideoMetadata(
                    ContentRef("youtube", "dQw4w9WgXcQ", 0), "English source", 2000,
                    "UCcreator", "Creator", 1234567890, "en", 2))
        transcript = replace(record(("This is a complete source sentence.",)), video_part_id=part_id, language="en")
        insert_record(connection, transcript)
        workflow, repository, _, executor = runtime(connection, root, FakeClient())
        prepared = repository.prepare(1, None, EditorialConfig())
        jobs = workflow.request_editorial(video_part_id=part_id, input_id=prepared["input_id"])
        assert executor.run().succeeded == 2
        revision = repository.revision_for_job(jobs[0])
        before = connection.total_changes
        export_delivery(connection, output=tmp_path / "delivery", artifact_roots=(root,), revision_ids=[revision])
        assert connection.total_changes == before
        assert connection.execute("SELECT count(*) FROM publication_editions").fetchone()[0] == 0
    assert read_records(tmp_path / "delivery/records.jsonl")[0]["source"]["platform"] == platform


@pytest.mark.parametrize("name", ["../escape", "CON.txt", "a/../b", "trailing.", "NUL", "a:b", "a\\b"])
def test_attachment_names_remain_portable_across_hosts(produced, tmp_path, name):
    connection, root, revision = produced
    attachment = tmp_path / "user.json"
    attachment.write_bytes(b"{}")
    with pytest.raises(ValueError, match="portable"):
        export_delivery(connection, output=tmp_path / "bad", artifact_roots=(root,),
                        revision_ids=[revision], attachments={name: attachment})
    assert not (tmp_path / "bad").exists()


def _minimal_record():
    return {"record_id": "source:1", "kind": "transcript", "text": "A usable sentence.",
            "source": {"platform": "local", "source_id": "1", "group_id": "local:1"},
            "quality": {"status": "passed", "gate": "production-minimum/v1"},
            "provenance": {"transcript_file": "bronze/source.json"}}


@pytest.mark.parametrize(("key", "value"), [
    ("text", ""), ("source", {}), ("quality", {}), ("provenance", {}),
    ("segments", []), ("segments", [{"start_ms": True, "end_ms": 2}]),
])
def test_minimum_contract_rejects_unusable_core_fields_without_pruning_extensions(key, value):
    row = _minimal_record()
    row["custom"] = {"future_use": "unrestricted"}
    validate_record(row)
    row[key] = value
    with pytest.raises(ValueError):
        validate_record(row)
    assert row["custom"] == {"future_use": "unrestricted"}


def test_record_stream_rejects_nonobjects_empty_lines_and_duplicate_ids(tmp_path):
    path = tmp_path / "records.jsonl"
    for content in ("[]\n", "\n", "", json.dumps(_minimal_record()) + "\n" + json.dumps(_minimal_record()) + "\n"):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            read_records(path)


@pytest.mark.parametrize(("key", "value"), [("records_file", "other.jsonl"), ("record_count", 2)])
def test_rehashed_receipt_still_requires_the_declared_record_contract(tmp_path, key, value):
    from bili_asr.delivery import FORMAT
    from bili_asr.portable_bundle import json_bytes, seal_bundle, write_file

    root = tmp_path / "bundle"
    root.mkdir()
    write_file(root, "records.jsonl", json_bytes(_minimal_record()))
    metadata = {"records_file": "records.jsonl", "record_count": 1, key: value}
    seal_bundle(root, format=FORMAT, files=["records.jsonl"], **metadata)
    with pytest.raises(ValueError):
        verify_delivery(root)


def test_cli_exports_retained_asr_evidence_and_user_attachment(produced, tmp_path, capsys):
    _, root, _ = produced
    extra = tmp_path / "extra.json"
    extra.write_text('{"future":{"field":true}}', encoding="utf-8")
    attachment = tmp_path / "user.txt"
    attachment.write_bytes(b"caller-owned material")
    output = tmp_path / "transcript-delivery"
    assert main(["delivery", "export", "--archive-root", str(root), "--transcript-id", "1",
                 "--extensions", str(extra), "--attachment", "user.txt=" + str(attachment),
                 "--out", str(output)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["record_count"] == 1
    assert verify_delivery(output)["bundle_id"] == receipt["bundle_id"]
    row = read_records(output / "records.jsonl")[0]
    assert row["kind"] == "transcript" and row["quality"]["admission"]
    assert row["extensions"] == {"future": {"field": True}}
    assert (output / "attachments/user.txt").read_bytes() == attachment.read_bytes()


@pytest.mark.parametrize("binding", ["missing-equals", "=file", "name="])
def test_cli_rejects_invalid_attachment_bindings_before_export(tmp_path, capsys, binding):
    assert main(["delivery", "export", "--attachment", binding, "--out", str(tmp_path / "out")]) == 1
    assert "unique NAME=FILE" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_listing_rejects_invalid_pagination_before_opening_a_database(capsys):
    assert main(["delivery", "list", "--limit", "0"]) == 1
    assert "limit 1..1000" in capsys.readouterr().err


@pytest.mark.parametrize("revision_id", ["../escape", "unknown"])
def test_revision_read_refuses_invalid_and_unknown_identity(produced, revision_id):
    from bili_asr.storage.editorial_revision import read_revision

    connection, _, _ = produced
    with pytest.raises(ValueError, match="revision ID"):
        read_revision(connection, revision_id)


@pytest.mark.parametrize("mode", ["malformed", "identity"])
def test_revision_read_rejects_corrupt_frozen_input(produced, mode):
    from bili_asr.storage.editorial_revision import read_revision

    connection, _, revision = produced
    row = connection.execute("SELECT prepared_json FROM editorial_inputs").fetchone()
    value = json.loads(row[0])
    if mode == "identity":
        value["snapshot"]["metadata"]["title"] = "Changed after freezing"
    else:
        value = {}
    with connection:
        connection.execute("UPDATE editorial_inputs SET prepared_json=?", (json.dumps(value),))
    with pytest.raises(ValueError, match="publication-integrity"):
        read_revision(connection, revision)
