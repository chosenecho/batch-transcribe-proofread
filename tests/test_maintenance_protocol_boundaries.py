"""Protocol parsers reject malformed historical evidence before package writes."""
import copy
import errno
import json
import sqlite3
import zipfile

import pytest

from bili_asr.services import maintenance_package_core as core
from bili_asr.services import snapshot_protocol as protocol
from tests.fixtures.frozen_migration_archive import frozen_archive


@pytest.mark.parametrize("value,error", [
    ([], "result envelope"), ({"artifacts": []}, "artifact result"),
    ({"raw_path": "transcripts/x/raw.json"}, "incomplete workflow bundle"),
    ({"storage_key": "audio/x.wav", "sha256": "unknown"}, "artifact hash"),
])
def test_workflow_attempt_envelope_does_not_invent_malformed_dependencies(tmp_path, value, error):
    source = tmp_path / "source"
    frozen_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("DELETE FROM transcript_asr_evidence")
        connection.execute("UPDATE workflow_attempts SET result_json=NULL")
        key = connection.execute("SELECT attempt_id FROM workflow_attempts LIMIT 1").fetchone()[0]
        connection.execute("UPDATE workflow_attempts SET result_json=? WHERE attempt_id=?", (json.dumps(value), key))
    with pytest.raises(ValueError, match=error):
        core._attempt_artifacts(source / "archive.db")


def test_attempt_dictionary_outputs_include_explicit_bundle_marker_and_detect_conflicts(tmp_path):
    from bili_asr.artifacts import BUNDLE_MARKER_NAME, REQUIRED_ARTIFACT_KEYS
    source = tmp_path / "source"
    frozen_archive(source)
    outputs = {key: f"transcripts/unit/{key}.json" for key in REQUIRED_ARTIFACT_KEYS}
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("DELETE FROM transcript_asr_evidence")
        connection.execute("UPDATE workflow_attempts SET result_json=NULL")
        keys = [row[0] for row in connection.execute("SELECT attempt_id FROM workflow_attempts LIMIT 2")]
        connection.execute("UPDATE workflow_attempts SET result_json=? WHERE attempt_id=?", (json.dumps({**outputs, "artifacts": {"trace": "documents/trace.json"}}), keys[0]))
    dependencies = core._attempt_artifacts(source / "archive.db")
    assert "documents/trace.json" in dependencies and any(key.endswith(BUNDLE_MARKER_NAME) for key in dependencies)
    with sqlite3.connect(source / "archive.db") as connection:
        for identity, digest in zip(keys, ("0" * 64, "1" * 64), strict=True):
            connection.execute("UPDATE workflow_attempts SET result_json=? WHERE attempt_id=?", (json.dumps({"storage_key": "audio/same.wav", "sha256": digest}), identity))
    with pytest.raises(ValueError, match="conflicting"):
        core._attempt_artifacts(source / "archive.db")


@pytest.mark.parametrize("value,error", [
    ({"schema_version": 2}, "ASR evidence version"),
    ({"schema_version": 1, "audio": "untyped"}, "audio evidence"),
])
def test_asr_evidence_unknown_version_or_audio_layout_is_not_exported(tmp_path, value, error):
    source = tmp_path / "source"
    frozen_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(value),))
    with pytest.raises(ValueError, match=error):
        core._attempt_artifacts(source / "archive.db")


def test_selected_missing_dependency_and_bounded_document_reader(tmp_path, monkeypatch):
    source = tmp_path / "source"
    frozen_archive(source)
    with pytest.raises(ValueError, match="dependency artifact is missing"):
        core._selected_artifacts(source / "archive.db", {}, [1])
    document = tmp_path / "large.json"
    document.write_bytes(b'{"large":"content"}')
    monkeypatch.setattr(core, "_LIMIT", 2)
    with pytest.raises(ValueError, match="oversized"):
        core._json(document)


@pytest.mark.parametrize("change,error", [
    (lambda m: m.update(created_at="not-time"), "creation timestamp"),
    (lambda m: m.update(created_at="2026-10-13T00:00:00+08:00"), "creation timestamp"),
    (lambda m: m["tables"].append(copy.deepcopy(m["tables"][0])), "duplicate maintenance table"),
    (lambda m: m["files"].append(copy.deepcopy(m["files"][0])), "duplicate"),
    (lambda m: m["files"][0].update(path="../archive.db"), "portable|path|artifact"),
])
def test_rehashed_manifest_cannot_bypass_semantic_validation(tmp_path, change, error):
    source = tmp_path / "source"
    frozen_archive(source)
    archive = tmp_path / "source.zip"
    core.export_package(source, archive)
    value = core.inspect_package(archive)["manifest"]
    change(value)
    value["content_id"] = core.digest({key: item for key, item in value.items() if key not in {"content_id", "created_at", "producer_version"}})
    with pytest.raises(ValueError, match=error):
        core._validate_manifest(value)


def test_container_special_members_declared_size_and_reader_changes_fail(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    archive = tmp_path / "source.zip"
    core.export_package(source, archive)
    with zipfile.ZipFile(archive) as incoming:
        manifest = json.loads(incoming.read(core._NAME))
        files = {item.filename: incoming.read(item) for item in incoming.infolist() if item.filename != core._NAME}
    for index, special in enumerate((False, True)):
        value = copy.deepcopy(manifest)
        if not special:
            value["files"][0]["size"] += 1
        value["content_id"] = core.digest({k: v for k, v in value.items() if k not in {"content_id", "created_at", "producer_version"}})
        damaged = tmp_path / f"damaged-{index}.zip"
        with zipfile.ZipFile(damaged, "w") as bundle:
            for key, data in files.items():
                if special and key == "archive.db":
                    info = zipfile.ZipInfo(key)
                    info.external_attr = 0o120777 << 16
                    bundle.writestr(info, data)
                else:
                    bundle.writestr(key, data)
            bundle.writestr(core._NAME, core.canonical(value))
        with pytest.raises(ValueError, match="size mismatch|links"):
            core.inspect_package(damaged)


def test_sync_directory_handles_unsupported_fs_but_preserves_real_io_errors(tmp_path, monkeypatch):
    original = protocol.os.fsync
    def unsupported(_):
        raise OSError(errno.EINVAL, "filesystem does not support directory fsync")
    monkeypatch.setattr(protocol.os, "fsync", unsupported)
    protocol._sync_directory(tmp_path)
    def unavailable(_):
        raise OSError(errno.EIO, "real I/O failure")
    monkeypatch.setattr(protocol.os, "fsync", unavailable)
    with pytest.raises(OSError, match="real I/O"):
        protocol._sync_directory(tmp_path)
    monkeypatch.setattr(protocol.os, "fsync", original)
    (tmp_path / "existing").write_text("preserved")
    with pytest.raises(protocol.SnapshotError):
        protocol._empty_destination(tmp_path)


@pytest.mark.parametrize("change,error", [
    (lambda m: m.update(snapshot_id="not-uuid"), "UUID"),
    (lambda m: m.update(producer_version=""), "producer_version"),
    (lambda m: m.update(files=[]), "nonempty"),
    (lambda m: m["files"][0].pop("size"), "file entry"),
    (lambda m: m["files"][0].update(path="documents/history.json"), "missing archive.db"),
    (lambda m: m.update(unknown="field"), "invalid fields"),
])
def test_legacy_snapshot_protocol_refuses_rehashed_untyped_fields(tmp_path, change, error):
    from bili_asr.services.archive_snapshot import save_snapshot
    source = tmp_path / "source"
    frozen_archive(source)
    original = tmp_path / "snapshot.zip"
    save_snapshot(source, original)
    with zipfile.ZipFile(original) as bundle:
        manifest = json.loads(bundle.read(protocol._MANIFEST_NAME))
        files = {entry.filename: bundle.read(entry) for entry in bundle.infolist() if entry.filename != protocol._MANIFEST_NAME}
    change(manifest)
    changed = tmp_path / "damaged.zip"
    with zipfile.ZipFile(changed, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
        bundle.writestr(protocol._MANIFEST_NAME, json.dumps(manifest))
    with zipfile.ZipFile(changed) as bundle, pytest.raises(protocol.SnapshotError, match=error):
        protocol._read_manifest(bundle)


@pytest.mark.parametrize("raw,error", [(b'{"format":"x","format":"y"}', "duplicate"),
                                     (b'{"format":', "not valid JSON")])
def test_legacy_manifest_ambiguous_or_truncated_json_refuses(tmp_path, raw, error):
    archive = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(protocol._MANIFEST_NAME, raw)
    with zipfile.ZipFile(archive) as bundle, pytest.raises(protocol.SnapshotError, match=error):
        protocol._read_manifest(bundle)


def test_legacy_marker_invalid_ascii_and_size_refuse(tmp_path, monkeypatch):
    from bili_asr.artifacts import BUNDLE_MARKER_NAME
    key = "transcripts/original/" + BUNDLE_MARKER_NAME
    archive = tmp_path / "marker.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(key, b"\xffnot-ascii")
    with zipfile.ZipFile(archive) as bundle, pytest.raises(protocol.SnapshotError, match="invalid transcript"):
        protocol._check_bundle_markers(bundle, {key: {}})
    monkeypatch.setattr(protocol, "_MARKER_LIMIT", 1)
    with zipfile.ZipFile(archive) as bundle, pytest.raises(protocol.SnapshotError, match="oversized"):
        protocol._check_bundle_markers(bundle, {key: {}})


def test_export_location_cannot_overwrite_source_or_existing_user_file(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    existing = tmp_path / "important.zip"
    existing.write_bytes(b"original important bytes")
    for output, message in ((source / "inside.zip", "outside"), (existing, "already exists"),
                            (tmp_path / "missing-parent" / "new.zip", "parent must exist")):
        with pytest.raises(ValueError, match=message):
            core.export_package(source, output)
    assert existing.read_bytes() == b"original important bytes"


def test_workflow_reference_hash_cannot_be_downgraded_to_presence_only(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with pytest.raises(ValueError, match="workflow result references"):
        core._check_attempt_artifacts(source / "archive.db", {})
