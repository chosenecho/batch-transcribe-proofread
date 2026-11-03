"""Read-only reference validation never materializes embedded media."""
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr import cli
from bili_asr.artifacts import BUNDLE_MARKER_NAME
from bili_asr.services import reference_backup as service
from tests.test_artifact_restore import _setup


def configured(tmp_path):
    roots, *_ = _setup(tmp_path)
    media = roots.archive_root / "audio/unoffloaded.m4a"
    media.write_bytes(b"unoffloaded media" * 300000)
    return roots, media


def test_save_check_inspect_plan_stream_every_file_with_database_only_scratch(tmp_path, monkeypatch, capsys):
    roots, media = configured(tmp_path)
    captures = []
    original = service._validate_into
    def observed(path, stage, **kwargs):
        manifest = original(path, stage, **kwargs)
        files = {item.relative_to(stage).as_posix(): item.stat().st_size for item in stage.rglob("*") if item.is_file()}
        captures.append((kwargs.get("extract_artifacts", False), files))
        return manifest
    monkeypatch.setattr(service, "_validate_into", observed)
    output = tmp_path / "reference.zip"
    saved = service.save_reference_backup(roots, output)
    checked = service.check_reference_backup(output)
    plan = service.plan_reference_restore(output, tmp_path / "future")
    assert cli.main(["reference", "inspect", "--file", str(output)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert len(captures) == 4
    assert all(not full and set(files) == {"archive.db"} for full, files in captures)
    for report in (saved, checked, plan, inspected):
        assert report["valid"] and not report["read_only_media_extracted"]
        assert report["read_only_temporary_bytes"] == captures[0][1]["archive.db"]
    assert plan["required_free_bytes"] > media.stat().st_size
    assert not (tmp_path / "future").exists()


def test_small_scratch_can_validate_media_larger_than_available_space(tmp_path, monkeypatch):
    roots, media = configured(tmp_path)
    output = tmp_path / "reference.zip"
    service.save_reference_backup(roots, output)
    with service.verified_reference(output) as (database, _manifest):
        budget = database.stat().st_size + 32768
    assert media.stat().st_size > budget
    # Enforce actual scratch writes, including save/check's private stage. The
    # database fits; any media extraction would exceed this declared quota.
    original = Path.open
    def limited(path, mode="r", *args, **kwargs):
        if ("bili-reference-check-" in str(path) and any(flag in mode for flag in "wax+")
                and path.name != "archive.db"):
            raise OSError("scratch quota exhausted by media materialization")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", limited)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _path: SimpleNamespace(free=budget))
    report = service.check_reference_backup(output)
    assert report["valid"] and report["read_only_temporary_bytes"] < budget
    plan = service.plan_reference_restore(output, tmp_path / "target")
    assert plan["valid"] and not plan["target_ready"]
    with pytest.raises(service.ReferenceBackupError, match="insufficient restore space"):
        service.restore_reference_backup(output, tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("damage", ["media-hash", "media-size", "unlisted", "unsafe-path", "duplicate", "bundle-marker"])
def test_streaming_validation_still_rejects_damaged_media_paths_and_markers(tmp_path, damage):
    roots, _media = configured(tmp_path)
    backup = tmp_path / "reference.zip"
    service.save_reference_backup(roots, backup)
    with zipfile.ZipFile(backup) as bundle:
        members = {item.filename: bundle.read(item) for item in bundle.infolist()}
    manifest = json.loads(members[service.MANIFEST])
    entry = next(item for item in manifest["files"] if item["path"] == "audio/unoffloaded.m4a")
    if damage == "media-hash":
        members[entry["path"]] = b"X" + members[entry["path"]][1:]
    elif damage == "media-size":
        entry["size"] += 1
    elif damage == "unlisted":
        members["audio/unlisted.m4a"] = b"undeclared"
    elif damage == "unsafe-path":
        members["../escape"] = members.pop(entry["path"])
        entry["path"] = "../escape"
    elif damage == "bundle-marker":
        marker = next(item for item in manifest["files"] if item["path"].endswith(BUNDLE_MARKER_NAME))
        document = json.loads(members[marker["path"]])
        document["artifacts"][next(iter(document["artifacts"]))]["sha256"] = "f" * 64
        members[marker["path"]] = json.dumps(document).encode("ascii")
        import hashlib
        marker.update(size=len(members[marker["path"]]), sha256=hashlib.sha256(members[marker["path"]]).hexdigest())
    members[service.MANIFEST] = json.dumps(manifest).encode("utf-8")
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(damaged, "w") as bundle:
        for key, raw in members.items():
            bundle.writestr(key, raw)
        if damage == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr(entry["path"], members[entry["path"]])
    for action in (lambda: service.check_reference_backup(damaged),
                   lambda: service.plan_reference_restore(damaged, tmp_path / "never-created"),
                   lambda: service.restore_reference_backup(damaged, tmp_path / "never-created")):
        with pytest.raises((ValueError, zipfile.BadZipFile)):
            action()
    assert not (tmp_path / "never-created").exists()


def test_restore_extracts_all_embedded_bytes_and_preserves_old_backup_contract(tmp_path):
    roots, media = configured(tmp_path)
    backup = tmp_path / "legacy-reference.zip"
    service.save_reference_backup(roots, backup)
    destination = tmp_path / "restored"
    result = service.restore_reference_backup(backup, destination)
    assert result["installed"] and result["format"] == "archive-reference-backup/v1"
    assert (destination / media.relative_to(roots.archive_root)).read_bytes() == media.read_bytes()
    with zipfile.ZipFile(backup) as bundle:
        manifest = json.loads(bundle.read(service.MANIFEST))
        for entry in manifest["files"]:
            assert (destination / entry["path"]).read_bytes() == bundle.read(entry["path"])
