"""Portable recovery points fail closed on missing, corrupt or ambiguous bytes."""
import json
from pathlib import Path

import pytest

from bili_asr.canonical_json import digest
from bili_asr.portable_bundle import (
    bundle_file,
    copy_file,
    file_entry,
    json_bytes,
    new_bundle,
    new_directory,
    seal_bundle,
    verify_bundle,
    write_file,
)


@pytest.fixture
def sealed(tmp_path):
    root = tmp_path / "bundle"
    with new_bundle(root) as stage:
        write_file(stage, "data.json", b'{"evidence":"retained"}')
        seal_bundle(stage, format="test/v1", files=["data.json"])
    return root


@pytest.mark.parametrize("mode", ["identity", "format", "empty", "entry", "duplicate", "reserved", "missing", "extra"])
def test_recovery_inventory_rejects_corruption_and_ambiguity(sealed, mode):
    manifest = json.loads((sealed / "manifest.json").read_text("utf-8"))
    if mode == "format":
        manifest["format"] = "unknown"
    elif mode == "empty":
        manifest["files"] = []
    elif mode == "entry":
        manifest["files"][0]["size"] = True
    elif mode == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    elif mode == "reserved":
        manifest["files"][0]["path"] = "manifest.json"
    elif mode == "missing":
        (sealed / "data.json").unlink()
    elif mode == "extra":
        (sealed / "untracked.json").write_bytes(b"untracked")
    manifest["bundle_id"] = digest({k: v for k, v in manifest.items() if k != "bundle_id"})
    if mode == "identity":
        manifest["bundle_id"] = "not-the-hash"
    (sealed / "manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError):
        verify_bundle(sealed, format="test/v1")


def test_missing_bundle_manifest_and_source_report_validation_errors(tmp_path):
    with pytest.raises(ValueError, match="existing directory"):
        verify_bundle(tmp_path / "missing")
    with pytest.raises(ValueError, match="manifest"):
        verify_bundle(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        file_entry(tmp_path, "missing")
    with pytest.raises(ValueError, match="regular file"):
        copy_file(tmp_path, "target", tmp_path / "missing")


def test_changed_attachment_is_not_silently_sealed(tmp_path, monkeypatch):
    import bili_asr.portable_bundle as bundles

    source = tmp_path / "user.txt"
    source.write_bytes(b"original")
    original = bundles.shutil.copyfileobj

    def concurrent_edit(reader, writer, **kwargs):
        original(reader, writer, **kwargs)
        source.write_bytes(b"changed while being copied")

    monkeypatch.setattr(bundles.shutil, "copyfileobj", concurrent_edit)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="changed during copy"), new_bundle(output) as stage:
        copy_file(stage, "attachment.txt", source)
    assert not output.exists()
    assert source.read_bytes() == b"changed while being copied"


def test_staging_does_not_replace_a_concurrently_created_destination(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="created during staging"), new_directory(output) as stage:
        write_file(stage, "ours", b"ours")
        output.mkdir()
        (output / "owner").write_bytes(b"someone else")
    assert list(output.iterdir()) == [output / "owner"]
    assert (output / "owner").read_bytes() == b"someone else"


def test_links_are_refused_without_touching_the_target(tmp_path):
    target = tmp_path / "original"
    target.write_bytes(b"evidence")
    link = tmp_path / "linked"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symbolic links")
    with pytest.raises(ValueError, match="symbolic links"):
        bundle_file(tmp_path, "linked")
    with pytest.raises(ValueError, match="symbolic links"), new_directory(link):
        pytest.fail("linked destination was accepted")
    assert target.read_bytes() == b"evidence"


def test_rename_error_does_not_publish_an_incomplete_directory(tmp_path, monkeypatch):
    output = tmp_path / "out"
    original = Path.rename
    calls = []

    def denied_once(path, target):
        if path.name == "bundle":
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError("temporary rename denial")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", denied_once)
    import os
    if os.name == "nt":
        with new_directory(output) as stage:
            write_file(stage, "data", b"complete")
        assert (output / "data").read_bytes() == b"complete"
        assert len(calls) == 2
    else:
        with pytest.raises(PermissionError), new_directory(output) as stage:
            write_file(stage, "data", b"complete")
        assert not output.exists()
