"""Portable archive snapshots preserve results and reject incomplete input."""

from __future__ import annotations

import hashlib
import json
import lzma
import os
from pathlib import Path
import sqlite3
import stat
import struct
import time
import zipfile

import pytest

from bili_asr.services.archive_snapshot import (
    SnapshotError,
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from bili_asr.services import archive_snapshot
from bili_asr.archive import BUNDLE_MARKER_NAME
from bili_asr.artifacts import BUNDLE_SCHEMA, REQUIRED_ARTIFACT_KEYS
from bili_asr.storage import open_database


def _seed_archive(root: Path, artifact_root: Path | None = None) -> Path:
    connection = open_database(root)
    try:
        with connection:
            connection.execute(
                "INSERT INTO bilibili_users VALUES (1, 'snapshot test', 1, 1)"
            )
            connection.execute(
                "INSERT INTO videos VALUES ('BVtest', 1, 1, 'video', 1, 1, 1)"
            )
            connection.execute(
                "INSERT INTO video_parts VALUES (1, 'BVtest', 0, 1, 'part', 1000, 'metadata_collected', 1, 1)"
            )
            connection.execute(
                "INSERT INTO ingestion_cursors VALUES (1, 7, 100, 'ready', NULL, 1)"
            )
            connection.execute(
                "INSERT INTO workflow_jobs (job_id,kind,video_part_id,dedupe_key,payload_json,status,available_at,created_at,updated_at) "
                "VALUES ('queued', 'audio', 1, 'queued-audio', '{}', 'queued', 1, 1, 1)"
            )
        base = artifact_root or root
        audio = base / "audio" / "BVtest.m4a"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"saved audio data")
        with connection:
            connection.execute(
                "INSERT INTO audio_objects VALUES (1, ?, ?, 'm4a', 1000, 'audio/BVtest.m4a', 1)",
                (hashlib.sha256(audio.read_bytes()).hexdigest(), audio.stat().st_size),
            )
            connection.execute("INSERT INTO part_audio_objects VALUES (1, 1, 1, 'bilibili')")
        for relative in ("transcripts/work/bundle.txt", "documents/ai-draft.md", "subtitles/raw.json"):
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
    finally:
        connection.close()
    return audio


def _rewrite_snapshot(source: Path, output: Path, mutate_manifest=None, mutate_files=None) -> None:
    with zipfile.ZipFile(source) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    manifest = json.loads(files["snapshot.json"])
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    files["snapshot.json"] = json.dumps(manifest).encode()
    if mutate_files is not None:
        mutate_files(files)
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _seed_publication(root: Path) -> Path:
    directory = root / "transcripts" / "published"
    directory.mkdir(parents=True)
    files = {}
    for key, name in {
        "srt_path": "bundle.srt", "vtt_path": "bundle.vtt", "txt_path": "bundle.txt",
        "md_path": "bundle.md", "raw_path": "bundle.raw.json",
    }.items():
        path = directory / name
        path.write_bytes(name.encode())
        files[key] = path.relative_to(root).as_posix()
    marker = directory / BUNDLE_MARKER_NAME
    marker.write_text(json.dumps({
        "schema": BUNDLE_SCHEMA,
        "artifacts": {
            key: {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
            for key, path in files.items()
        },
    }), encoding="ascii")
    connection = open_database(root)
    try:
        with connection:
            connection.execute(
                "INSERT INTO transcripts(video_part_id, source_kind, language, version, content_sha256, created_at) "
                "VALUES (1, 'subtitle-ai', 'zh-CN', 1, ?, 1)", ("a" * 64,)
            )
            transcript_id = connection.execute("SELECT transcript_id FROM transcripts").fetchone()[0]
            connection.execute(
                "INSERT INTO workflow_publications(video_part_id,transcript_id,published_at,artifact_json) VALUES (1, ?, 1, ?)",
                (transcript_id, json.dumps(files)),
            )
    finally:
        connection.close()
    return marker


def test_snapshot_roundtrip_preserves_progress_and_all_artifact_families(tmp_path) -> None:
    root = tmp_path / "source"
    _seed_archive(root)
    source_db = (root / "archive.db").read_bytes()
    snapshot = tmp_path / "backup.zip"
    saved = save_snapshot(root, snapshot)
    checked = check_snapshot(snapshot)
    target = tmp_path / "different-device" / "archive"
    restored = restore_snapshot(snapshot, target)

    assert saved["snapshot_id"] == checked["snapshot_id"] == restored["snapshot_id"]
    assert checked["valid"] is True
    assert saved["file_count"] == 5
    assert (root / "archive.db").read_bytes() == source_db
    for relative in ("audio/BVtest.m4a", "transcripts/work/bundle.txt", "documents/ai-draft.md", "subtitles/raw.json"):
        assert (target / relative).read_bytes() == (root / relative).read_bytes()
    with sqlite3.connect(target / "archive.db") as connection:
        assert connection.execute("SELECT next_page FROM ingestion_cursors").fetchone()[0] == 7
        assert connection.execute("SELECT status FROM workflow_jobs").fetchone()[0] == "queued"
    with zipfile.ZipFile(snapshot) as archive:
        assert archive.getinfo("audio/BVtest.m4a").compress_type == zipfile.ZIP_STORED
        assert not any(str(root) in name for name in archive.namelist())


def test_restore_accepts_empty_target_and_rejects_nonempty_target(tmp_path) -> None:
    root, target, snapshot = tmp_path / "source", tmp_path / "target", tmp_path / "backup.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    target.mkdir()
    restore_snapshot(snapshot, target)
    sentinel = target / "keep.txt"
    sentinel.write_text("preserve")
    with pytest.raises(SnapshotError, match="nonexistent or empty"):
        restore_snapshot(snapshot, target)
    assert sentinel.read_text() == "preserve"


def test_configured_artifact_root_has_precedence_with_archive_fallback(tmp_path) -> None:
    root, products, snapshot = tmp_path / "source", tmp_path / "products", tmp_path / "backup.zip"
    _seed_archive(root, products)
    legacy = root / "audio" / "legacy.m4a"
    legacy.parent.mkdir()
    legacy.write_bytes(b"legacy fallback")
    shadow = root / "audio" / "BVtest.m4a"
    shadow.write_bytes(b"stale legacy copy")
    save_snapshot(root, snapshot, artifact_root=products)
    target = tmp_path / "target"
    restore_snapshot(snapshot, target)
    assert (target / "audio" / "BVtest.m4a").read_bytes() == b"saved audio data"
    assert (target / "audio" / "legacy.m4a").read_bytes() == b"legacy fallback"


def test_snapshot_refuses_missing_and_corrupt_referenced_audio(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    audio = _seed_archive(root)
    audio.write_bytes(b"wrong audio")
    with pytest.raises(SnapshotError, match="hash mismatch"):
        save_snapshot(root, snapshot)
    assert not snapshot.exists()
    audio.unlink()
    with pytest.raises(SnapshotError, match="missing artifact"):
        save_snapshot(root, snapshot)
    assert not snapshot.exists()
    assert not list(tmp_path.glob(".*.snapshot-stage-*"))


@pytest.mark.parametrize("relative", ["audio/.audio-stage-abc.download", "audio/.workflow-audio-abc/audio/BVtest.m4a", "transcripts/.archive-bundle-stage-abc/bundle.txt", "documents/.reading.tmp"])
def test_snapshot_refuses_leftover_temporary_artifacts(tmp_path, relative) -> None:
    root = tmp_path / "source"
    _seed_archive(root)
    temporary = root / relative
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text("unfinished")
    with pytest.raises(SnapshotError, match="unfinished temporary artifact"):
        save_snapshot(root, tmp_path / "backup.zip")


def test_snapshot_never_overwrites_output_or_saves_inside_source_roots(tmp_path) -> None:
    root, products = tmp_path / "source", tmp_path / "products"
    _seed_archive(root, products)
    output = tmp_path / "backup.zip"
    output.write_bytes(b"keep output")
    with pytest.raises(SnapshotError, match="already exists"):
        save_snapshot(root, output, artifact_root=products)
    assert output.read_bytes() == b"keep output"
    for output in (root / "backup.zip", products / "backup.zip"):
        with pytest.raises(SnapshotError, match="outside archive"):
            save_snapshot(root, output, artifact_root=products)
        assert not output.exists()


def test_running_worker_blocks_snapshot_but_expired_worker_recovers_only_on_restore(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    with sqlite3.connect(root / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='running', lease_owner='old-device', lease_expires_at=?", (int(time.time()) + 3600,))
        connection.execute("INSERT INTO workflow_attempts VALUES ('attempt', 'queued', 'old-device', 1, NULL, 'running', NULL, NULL)")
    with pytest.raises(SnapshotError, match="still running"):
        save_snapshot(root, snapshot)
    with sqlite3.connect(root / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET lease_expires_at=1")
    source_bytes = (root / "archive.db").read_bytes()
    saved = save_snapshot(root, snapshot)
    check_snapshot(snapshot)
    assert (root / "archive.db").read_bytes() == source_bytes
    target = tmp_path / "target"
    restored = restore_snapshot(snapshot, target)
    assert restored["recovered"]["workflow_jobs"] == 1
    assert restored["recovered"]["workflow_attempts"] == 1
    with sqlite3.connect(target / "archive.db") as connection:
        job = connection.execute("SELECT status,lease_owner,lease_expires_at FROM workflow_jobs").fetchone()
        assert job == ("queued", None, None)
        outcome, error_code, result = connection.execute("SELECT outcome,error_code,result_json FROM workflow_attempts").fetchone()
        assert outcome == "failed" and error_code == "snapshot_restored"
        assert json.loads(result)["snapshot_recovery"]["snapshot_id"] == saved["snapshot_id"]
    assert (root / "archive.db").read_bytes() == source_bytes


@pytest.mark.parametrize("bad_path", [
    "../escape", "/absolute", "C:/absolute", "audio\\escape", "audio/../escape",
    "audio//escape", "audio/NUL.wav", "audio/COM1.m4a", "audio/trailing.",
    "audio/trailing ", "audio/file:stream", "audio/bad\x00name", "other/file.txt",
])
def test_check_rejects_unsafe_portable_paths(tmp_path, bad_path) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "unsafe.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    _rewrite_snapshot(snapshot, corrupt, lambda manifest: manifest["files"].append({"path": bad_path, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}))
    with pytest.raises(SnapshotError, match="snapshot .*path|snapshot path"):
        check_snapshot(corrupt)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("paths", [
    ["audio/A.m4a", "audio/a.m4a"],
    ["audio/Sub/a.m4a", "audio/sub/b.m4a"],
    ["audio/file", "audio/file/child"],
    ["audio/same", "audio/same"],
])
def test_check_rejects_colliding_paths_and_file_parents(tmp_path, paths) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "unsafe.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    def add(manifest):
        manifest["files"].extend({"path": path, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()} for path in paths)
    _rewrite_snapshot(snapshot, corrupt, add)
    with pytest.raises(SnapshotError, match="collision|colliding|duplicate|parent directory"):
        check_snapshot(corrupt)


def test_check_and_restore_reject_modified_bytes_without_touching_target(tmp_path) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "corrupt.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    def corrupt_audio(files):
        files["audio/BVtest.m4a"] = b"wrong audio data"
    _rewrite_snapshot(snapshot, corrupt, mutate_files=corrupt_audio)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(SnapshotError, match="mismatch"):
        check_snapshot(corrupt)
    with pytest.raises(SnapshotError, match="mismatch"):
        restore_snapshot(corrupt, target)
    assert target.is_dir() and list(target.iterdir()) == []
    assert not list(tmp_path.glob(".target.restore-stage-*"))


def test_check_rejects_audio_hash_conflicting_with_database_even_if_manifest_matches(tmp_path) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "corrupt.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    replacement = b"different audio with honest ZIP hash"
    def mutate_manifest(manifest):
        item = next(item for item in manifest["files"] if item["path"] == "audio/BVtest.m4a")
        item.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
    _rewrite_snapshot(snapshot, corrupt, mutate_manifest, lambda files: files.update({"audio/BVtest.m4a": replacement}))
    with pytest.raises(SnapshotError, match="database artifact hash mismatch"):
        check_snapshot(corrupt)


def test_check_rejects_unlisted_duplicate_and_symlink_zip_members(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    unlisted = tmp_path / "unlisted.zip"
    _rewrite_snapshot(snapshot, unlisted, mutate_files=lambda files: files.update({"../escape": b"unsafe"}))
    with pytest.raises(SnapshotError, match="unlisted ZIP member"):
        check_snapshot(unlisted)
    duplicate = tmp_path / "duplicate.zip"
    _rewrite_snapshot(snapshot, duplicate)
    with zipfile.ZipFile(duplicate, "a") as archive, pytest.warns(UserWarning, match="Duplicate name"):
        archive.writestr("audio/BVtest.m4a", b"duplicate")
    with pytest.raises(SnapshotError, match="duplicate"):
        check_snapshot(duplicate)
    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(snapshot) as source, zipfile.ZipFile(symlink, "w") as destination:
        for info in source.infolist():
            if info.filename == "audio/BVtest.m4a":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            destination.writestr(info, source.read(info.filename))
    with pytest.raises(SnapshotError, match="unsafe"):
        check_snapshot(symlink)


@pytest.mark.parametrize("mutate", [
    lambda manifest: manifest.update(format_version=2),
    lambda manifest: manifest.update(format_version=True),
    lambda manifest: manifest.update(snapshot_id="../../escape"),
    lambda manifest: manifest.update(created_at="2026-10-08T12:00:00"),
    lambda manifest: manifest.update(database_contract="different-contract"),
    lambda manifest: manifest["files"][0].update(size=True),
    lambda manifest: manifest["files"][0].update(sha256="invalid"),
])
def test_check_rejects_invalid_manifest_and_incompatible_contract(tmp_path, mutate) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "corrupt.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    _rewrite_snapshot(snapshot, corrupt, mutate)
    with pytest.raises(SnapshotError):
        check_snapshot(corrupt)


def test_snapshot_supports_sqlite_wal_without_copying_wal_sidecars(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    connection = sqlite3.connect(root / "archive.db")
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("UPDATE ingestion_cursors SET next_page=12")
        connection.commit()
        assert (root / "archive.db-wal").exists()
        save_snapshot(root, snapshot)
        target = tmp_path / "target"
        restore_snapshot(snapshot, target)
        with sqlite3.connect(target / "archive.db") as restored:
            assert restored.execute("SELECT next_page FROM ingestion_cursors").fetchone()[0] == 12
        with zipfile.ZipFile(snapshot) as archive:
            assert "archive.db-wal" not in archive.namelist()
            assert "archive.db-shm" not in archive.namelist()
    finally:
        connection.close()


def test_snapshot_refuses_source_artifact_symlinks(tmp_path) -> None:
    root = tmp_path / "source"
    _seed_archive(root)
    external = tmp_path / "external.txt"
    external.write_text("private file")
    link = root / "documents" / "link.txt"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("platform cannot create symlinks")
    with pytest.raises(SnapshotError, match="symlinks"):
        save_snapshot(root, tmp_path / "backup.zip")


def test_snapshot_refuses_non_database_and_keeps_missing_source_missing(tmp_path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(SnapshotError):
        save_snapshot(root, tmp_path / "backup.zip")
    assert not root.exists()
    root.mkdir()
    (root / "archive.db").write_text("not SQLite")
    with pytest.raises(SnapshotError):
        save_snapshot(root, tmp_path / "backup.zip")


def test_check_streams_artifacts_without_materializing_them(tmp_path, monkeypatch) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    stage = tmp_path / "check-stage"
    stage.mkdir()

    class CheckDirectory:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return str(stage)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(archive_snapshot.tempfile, "TemporaryDirectory", CheckDirectory)
    assert check_snapshot(snapshot)["valid"]
    assert [entry.name for entry in stage.iterdir()] == ["archive.db"]


def test_snapshot_no_clobber_publication_race(tmp_path, monkeypatch) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    primitive = "rename" if os.name == "nt" else "link"
    original_publish = getattr(archive_snapshot.os, primitive)

    def competing_output(source, destination):
        Path(destination).write_bytes(b"other save won")
        original_publish(source, destination)

    monkeypatch.setattr(archive_snapshot.os, primitive, competing_output)
    with pytest.raises(SnapshotError):
        save_snapshot(root, snapshot)
    assert snapshot.read_bytes() == b"other save won"
    assert not list(tmp_path.glob(".*.snapshot-stage-*"))


def test_missing_reference_in_zip_fails_even_when_manifest_is_consistent(tmp_path) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "incomplete.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    def remove_manifest_reference(manifest):
        manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "audio/BVtest.m4a"]
    _rewrite_snapshot(snapshot, corrupt, remove_manifest_reference, lambda files: files.pop("audio/BVtest.m4a"))
    with pytest.raises(SnapshotError, match="missing artifact"):
        check_snapshot(corrupt)


def test_publication_missing_or_stale_bundle_marker_prevents_save(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    marker = _seed_publication(root)
    original = marker.read_bytes()
    marker.unlink()
    with pytest.raises(SnapshotError, match="missing artifact"):
        save_snapshot(root, snapshot)
    marker.write_bytes(original)
    (marker.parent / "bundle.txt").write_bytes(b"changed published text")
    with pytest.raises(SnapshotError, match="bundle marker hash"):
        save_snapshot(root, snapshot)
    assert not snapshot.exists()


@pytest.mark.parametrize("basename", ["bundle.txt", "bundle.vtt"])
def test_bundle_marker_is_checked_against_inventory_after_zip_hashes_match(tmp_path, basename) -> None:
    root, snapshot, forged = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "forged.zip"
    _seed_archive(root)
    marker = _seed_publication(root)
    save_snapshot(root, snapshot)
    path = (marker.parent / basename).relative_to(root).as_posix()
    replacement = b"replaced transcript"
    def mutate_manifest(manifest):
        entry = next(entry for entry in manifest["files"] if entry["path"] == path)
        entry.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
    _rewrite_snapshot(snapshot, forged, mutate_manifest, lambda files: files.update({path: replacement}))
    with pytest.raises(SnapshotError, match="bundle marker hash"):
        check_snapshot(forged)


def test_publication_missing_webvtt_prevents_save(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    marker = _seed_publication(root)
    (marker.parent / "bundle.vtt").unlink()
    with pytest.raises(SnapshotError, match="missing artifact.*bundle.vtt"):
        save_snapshot(root, snapshot)
    assert not snapshot.exists()


def test_snapshot_rejects_obsolete_bundle_marker_schema(tmp_path) -> None:
    root, snapshot = tmp_path / "source", tmp_path / "backup.zip"
    _seed_archive(root)
    marker = _seed_publication(root)
    document = json.loads(marker.read_bytes())
    document["schema"] = "archive-bundle-v1"
    marker.write_text(json.dumps(document), encoding="ascii")
    with pytest.raises(SnapshotError, match="invalid transcript bundle marker"):
        save_snapshot(root, snapshot)
    assert not snapshot.exists()


def test_complete_publication_restores_with_readable_bundle(tmp_path) -> None:
    root, snapshot, target = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "target"
    _seed_archive(root)
    _seed_publication(root)
    save_snapshot(root, snapshot)
    assert check_snapshot(snapshot)["valid"]
    restore_snapshot(snapshot, target)
    from bili_asr.archive import archive_bundle_complete
    with sqlite3.connect(target / "archive.db") as connection:
        paths = json.loads(connection.execute("SELECT artifact_json FROM workflow_publications").fetchone()[0])
    assert archive_bundle_complete(target, paths)
    assert set(paths) == set(REQUIRED_ARTIFACT_KEYS)
    assert (target / paths["vtt_path"]).read_bytes() == b"bundle.vtt"


def test_snapshot_refuses_unreadable_artifact_directory_instead_of_omitting_it(tmp_path, monkeypatch) -> None:
    root = tmp_path / "source"
    _seed_archive(root)
    original_walk = archive_snapshot.os.walk

    def unavailable(directory, *args, **kwargs):
        if Path(directory).name == "audio":
            kwargs["onerror"](PermissionError(13, "denied", str(directory)))
        return original_walk(directory, *args, **kwargs)

    monkeypatch.setattr(archive_snapshot.os, "walk", unavailable)
    with pytest.raises(SnapshotError, match="cannot be fully read"):
        save_snapshot(root, tmp_path / "backup.zip")


def test_corrupt_lzma_archive_reports_snapshot_error(tmp_path) -> None:
    root, snapshot, corrupt = tmp_path / "source", tmp_path / "backup.zip", tmp_path / "corrupt-lzma.zip"
    _seed_archive(root)
    save_snapshot(root, snapshot)
    with zipfile.ZipFile(snapshot) as source, zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_LZMA) as destination:
        for name in source.namelist():
            destination.writestr(name, source.read(name))
    with zipfile.ZipFile(corrupt) as archive:
        info = archive.getinfo("audio/BVtest.m4a")
    with corrupt.open("r+b") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        name_size, extra_size = struct.unpack_from("<HH", header, 26)
        payload = info.header_offset + 30 + name_size + extra_size
        stream.seek(payload + 4)  # First LZMA filter property after the ZIP-LZMA header.
        stream.write(b"\xff")
    with zipfile.ZipFile(corrupt) as archive:
        with pytest.raises(lzma.LZMAError):
            archive.read("audio/BVtest.m4a")
    with pytest.raises(SnapshotError):
        check_snapshot(corrupt)


def test_bundle_marker_extra_item_fields_cannot_break_restored_bundle(tmp_path) -> None:
    root = tmp_path / "source"
    _seed_archive(root)
    marker = _seed_publication(root)
    document = json.loads(marker.read_bytes())
    document["artifacts"]["txt_path"]["unexpected"] = True
    marker.write_text(json.dumps(document), encoding="ascii")
    with pytest.raises(SnapshotError, match="bundle marker"):
        save_snapshot(root, tmp_path / "backup.zip")
