"""Complete snapshots stream retained external objects without changing source."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.services import archive_snapshot
from bili_asr.services.archive_snapshot import (
    SnapshotError,
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from tests.test_artifact_restore import _authority, _setup


@pytest.mark.parametrize("configured", [False, True])
def test_offloaded_audio_snapshot_is_self_contained_and_preserves_source(tmp_path, configured):
    roots, target, audio, _source, _package, _replica = _setup(tmp_path, configured=configured)
    authority = _authority(roots)
    database = (roots.archive_root / "archive.db").read_bytes()
    output = tmp_path / "full.zip"
    result = save_snapshot(roots.archive_root, output,
                           artifact_root=roots.artifact_root if configured else None,
                           storage_targets={"cold": target})
    assert result["file_count"] > 1
    assert check_snapshot(output)["valid"]
    assert not (roots.archive_root / audio["storage_key"]).exists()
    assert not (roots.write_base / audio["storage_key"]).exists()
    assert (roots.archive_root / "archive.db").read_bytes() == database
    assert _authority(roots) == authority
    moved = tmp_path / "offline"
    target.rename(moved)
    restored = tmp_path / "restored"
    restore_snapshot(output, restored)
    with zipfile.ZipFile(output) as package:
        assert (restored / audio["storage_key"]).read_bytes() == package.read(audio["storage_key"])
        assert not any("cold" in name or "package" in name for name in package.namelist())


def test_missing_binding_keeps_complete_snapshot_strict(tmp_path):
    roots, _target, audio, *_ = _setup(tmp_path)
    output = tmp_path / "full.zip"
    with pytest.raises(SnapshotError, match="missing artifact"):
        save_snapshot(roots.archive_root, output)
    assert not output.exists()
    assert not (roots.archive_root / audio["storage_key"]).exists()


@pytest.mark.parametrize("failure", ["offline", "identity", "member"])
def test_bad_external_bytes_or_target_never_publish_complete_snapshot(tmp_path, failure):
    roots, target, audio, _source, package, _replica = _setup(tmp_path)
    if failure == "offline":
        target.rename(tmp_path / "offline")
    elif failure == "identity":
        (target / ".bili-asr-storage-target.json").write_text('{"version":1,"target_id":"replacement"}')
    else:
        package_path = Path(package["package_path"])
        with zipfile.ZipFile(package_path) as archive:
            members = [(member.filename, archive.read(member)) for member in archive.infolist()]
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in members:
                archive.writestr(name, b"x" * len(data) if name.startswith("objects/") else data)
    output = tmp_path / "full.zip"
    with pytest.raises(SnapshotError):
        save_snapshot(roots.archive_root, output, storage_targets={"cold": target})
    assert not output.exists()
    assert not (roots.archive_root / audio["storage_key"]).exists()


def test_complete_snapshot_checks_target_capacity_before_streaming(tmp_path, monkeypatch):
    roots, target, _audio, *_ = _setup(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    output = tmp_path / "full.zip"
    with pytest.raises(SnapshotError, match="space"):
        archive_snapshot.save_snapshot(roots.archive_root, output, storage_targets={"cold": target})
    assert not output.exists()
