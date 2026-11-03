"""Frozen bytes survive target-side packaging and selected-object recovery."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from bili_asr import artifact_packages as packages


def _source(root: Path, name: str, content: bytes) -> packages.PackageSource:
    path = root / "audio" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return packages.PackageSource(digest, path, f"audio/{name}", digest, len(content),
                                  packages.capture_source_generation(path))


def _create(tmp_path: Path, *, contents: tuple[bytes, ...] = (b"first", b"second")):
    source_root, target = tmp_path / "source", tmp_path / "target"
    target.mkdir()
    sources = [_source(source_root, f"{index}.m4a", content) for index, content in enumerate(contents)]
    result = packages.create_artifact_package(target, sources, operation_id="transfer-test",
                                              plan_sha256="a" * 64, batch_index=0)
    return result, sources, target


def _rewrite(original: Path, destination: Path, *, manifest_edit=None,
             data_edit=None, compression=zipfile.ZIP_STORED, extra=None):
    with zipfile.ZipFile(original) as incoming, zipfile.ZipFile(destination, "w", compression=compression) as outgoing:
        for member in incoming.infolist():
            content = incoming.read(member)
            if member.filename == "package.json" and manifest_edit:
                document = json.loads(content)
                manifest_edit(document)
                content = json.dumps(document).encode()
            elif data_edit and member.filename.startswith("objects/"):
                content = data_edit(content)
            outgoing.writestr(member.filename, content)
        if extra:
            outgoing.writestr(*extra)


def test_copy_check_and_restore_preserve_frozen_bytes_and_production_sources(tmp_path):
    result, sources, target = _create(tmp_path)
    archive = Path(result["package_path"])
    assert archive == target / result["package_key"]
    assert result["valid"]
    assert result["package_size_bytes"] == archive.stat().st_size
    assert result["package_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        assert all(item.compress_type == zipfile.ZIP_STORED for item in bundle.infolist())
        encoded = bundle.read("package.json")
        assert hashlib.sha256(encoded).hexdigest() == result["manifest_sha256"]
        assert all(item.extract_version == 45 for item in bundle.infolist() if item.filename.startswith("objects/"))
    assert packages.check_artifact_package(archive, expected_sha256=result["package_sha256"]) == result
    assert sources[0].source_path.read_bytes() == b"first"
    restored = tmp_path / "restored.m4a"
    answer = packages.restore_package_object(archive, sources[0].object_id, restored,
                                              expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)
    assert restored.read_bytes() == b"first"
    assert answer["installed"]
    assert not packages.restore_package_object(archive, sources[0].object_id, restored,
                                               expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)["installed"]
    assert not list(tmp_path.glob(".artifact-restore-stage-*"))


def test_restore_reads_only_selected_member_and_bounded_manifest(tmp_path, monkeypatch):
    result, sources, _target = _create(tmp_path)
    opened = []
    original = zipfile.ZipFile.open

    def observe(self, name, *args, **kwargs):
        opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", observe)
    packages.restore_package_object(Path(result["package_path"]), sources[1].object_id,
                                    tmp_path / "selected.m4a", expected_sha256=sources[1].sha256,
                                    expected_size=sources[1].size_bytes)
    assert opened == ["package.json", f"objects/{sources[1].sha256}"]


def test_existing_sealed_package_is_idempotent_even_when_source_was_released(tmp_path):
    result, sources, target = _create(tmp_path)
    original = Path(result["package_path"]).read_bytes()
    for source in sources:
        source.source_path.unlink()
    again = packages.create_artifact_package(target, sources, operation_id="transfer-test",
                                             plan_sha256="a" * 64, batch_index=0)
    assert again == result
    assert Path(again["package_path"]).read_bytes() == original


def test_batches_enforce_byte_and_object_limits_and_isolate_oversized_object(tmp_path):
    sources = [_source(tmp_path, f"{index}.m4a", content) for index, content in enumerate((b"a", b"bb", b"ccc", b"dddddd", b"ee"))]
    assert packages.package_batches(sources, max_bytes=4, max_objects=10) == (
        tuple(sources[:2]), (sources[2],), (sources[3],), (sources[4],))
    assert packages.package_batches(sources[:3], max_bytes=100, max_objects=1) == tuple((item,) for item in sources[:3])
    assert packages.package_batches([], max_bytes=1, max_objects=1) == ()
    with pytest.raises(packages.ArtifactPackageError, match="duplicate"):
        packages.package_batches([sources[0], sources[0]], max_bytes=100, max_objects=100)
    with pytest.raises(packages.ArtifactPackageError, match="limits"):
        packages.package_batches(sources, max_bytes=True, max_objects=1)


def test_prepared_target_is_required_and_must_be_outside_source_root(tmp_path):
    source = _source(tmp_path / "source", "a.m4a", b"original")
    missing = tmp_path / "missing-mount"
    with pytest.raises(packages.ArtifactPackageError):
        packages.create_artifact_package(missing, [source], operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    assert not missing.exists()
    nested = tmp_path / "source" / "target"
    nested.mkdir()
    with pytest.raises(packages.ArtifactPackageError, match="outside"):
        packages.create_artifact_package(nested, [source], operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    assert not (nested / "packages").exists()


@pytest.mark.parametrize("change", ["identity", "hash", "size", "generation"])
def test_bad_frozen_source_does_not_publish_a_package_or_modify_source(tmp_path, change):
    source = _source(tmp_path / "source", "a.m4a", b"original")
    if change == "identity":
        selected = replace(source, object_id="b" * 64)
    elif change == "hash":
        selected = replace(source, object_id="b" * 64, sha256="b" * 64)
    elif change == "size":
        selected = replace(source, size_bytes=source.size_bytes + 1)
    else:
        selected = replace(source, source_generation={**source.source_generation, "mtime_ns": source.source_generation["mtime_ns"] + 1})
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(packages.ArtifactPackageError):
        packages.create_artifact_package(target, [selected], operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    assert source.source_path.read_bytes() == b"original"
    assert not list(target.rglob("*.zip"))
    assert not list(target.rglob(".artifact-package-stage-*"))


def test_editing_an_already_read_source_chunk_is_rejected(tmp_path, monkeypatch):
    content = b"x" * (2 * 1024 * 1024 + 5)
    source = _source(tmp_path / "source", "a.m4a", content)
    target = tmp_path / "target"
    target.mkdir()
    original = packages._hash_stream
    changed = False

    def mutate_after_read(incoming, **kwargs):
        nonlocal changed
        answer = original(incoming, **kwargs)
        if kwargs.get("destination") is not None and not changed:
            changed = True
            with source.source_path.open("r+b") as writer:
                writer.write(b"y")
            os.utime(source.source_path, ns=(source.source_path.stat().st_atime_ns, source.source_generation["mtime_ns"] + 1_000_000_000))
        return answer

    monkeypatch.setattr(packages, "_hash_stream", mutate_after_read)
    with pytest.raises(packages.ArtifactPackageError, match="changed while reading"):
        packages.create_artifact_package(target, [source], operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    assert changed
    assert not list(target.rglob("*.zip"))


def test_target_reread_failure_never_publishes_half_package(tmp_path, monkeypatch):
    source = _source(tmp_path / "source", "a.m4a", b"keep")
    target = tmp_path / "target"
    target.mkdir()

    def fail_check(*args, **kwargs):
        raise packages.ArtifactPackageError("target reread failed")

    monkeypatch.setattr(packages, "check_artifact_package", fail_check)
    with pytest.raises(packages.ArtifactPackageError, match="target reread"):
        packages.create_artifact_package(target, [source], operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    assert source.source_path.read_bytes() == b"keep"
    assert not list(target.rglob("*.zip"))


@pytest.mark.parametrize("field,value", [("extra", "unknown"), ("format_version", True),
                                        ("package_id", "b" * 64), ("operation_id", "../unsafe")])
def test_unknown_or_noncanonical_manifest_is_rejected(tmp_path, field, value):
    result, _sources, _target = _create(tmp_path)
    broken = tmp_path / "broken.zip"
    _rewrite(Path(result["package_path"]), broken, manifest_edit=lambda document: document.update({field: value}))
    with pytest.raises(packages.ArtifactPackageError):
        packages.check_artifact_package(broken)


@pytest.mark.parametrize("extra_name", ["../escape", "audio/extra.m4a", "package.json", "objects/UNKNOWN"])
def test_unsafe_unlisted_and_duplicate_zip_members_are_rejected(tmp_path, extra_name):
    result, _sources, _target = _create(tmp_path)
    broken = tmp_path / "broken.zip"
    _rewrite(Path(result["package_path"]), broken, extra=(extra_name, b"extra"))
    with pytest.raises(packages.ArtifactPackageError):
        packages.read_package_manifest(broken)


def test_compressed_package_and_symlink_member_are_rejected(tmp_path):
    result, sources, _target = _create(tmp_path)
    compressed = tmp_path / "compressed.zip"
    _rewrite(Path(result["package_path"]), compressed, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(packages.ArtifactPackageError, match="compressed"):
        packages.check_artifact_package(compressed)
    linked = tmp_path / "linked.zip"
    with zipfile.ZipFile(Path(result["package_path"])) as incoming, zipfile.ZipFile(linked, "w") as outgoing:
        for member in incoming.infolist():
            if member.filename == f"objects/{sources[0].sha256}":
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
            outgoing.writestr(member, incoming.read(member))
    with pytest.raises(packages.ArtifactPackageError, match="unsafe"):
        packages.read_package_manifest(linked)


def test_same_size_corruption_and_expected_identity_mismatch_never_restore_bad_bytes(tmp_path):
    result, sources, _target = _create(tmp_path)
    broken = tmp_path / "corrupt.zip"
    _rewrite(Path(result["package_path"]), broken, data_edit=lambda content: b"!" + content[1:])
    destination = tmp_path / "restored.m4a"
    with pytest.raises(packages.ArtifactPackageError, match="SHA-256"):
        packages.restore_package_object(broken, sources[0].object_id, destination,
                                        expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)
    assert not destination.exists()
    assert not list(tmp_path.glob(".artifact-restore-stage-*"))
    with pytest.raises(packages.ArtifactPackageError, match="expected restore object"):
        packages.restore_package_object(Path(result["package_path"]), "b" * 64, destination,
                                        expected_sha256="b" * 64, expected_size=1)
    with pytest.raises(packages.ArtifactPackageError, match="package SHA-256"):
        packages.check_artifact_package(Path(result["package_path"]), expected_sha256="b" * 64)


def test_restore_never_overwrites_different_existing_bytes(tmp_path):
    result, sources, _target = _create(tmp_path)
    destination = tmp_path / "restored.m4a"
    destination.write_bytes(b"other")
    with pytest.raises(packages.ArtifactPackageError, match="different bytes"):
        packages.restore_package_object(Path(result["package_path"]), sources[0].object_id, destination,
                                        expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)
    assert destination.read_bytes() == b"other"


def test_source_and_target_symlink_paths_are_rejected(tmp_path):
    source = _source(tmp_path / "source", "a.m4a", b"keep")
    linked_root = tmp_path / "linked-source"
    try:
        linked_root.symlink_to(source.source_path.parents[1], target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(packages.ArtifactPackageError, match="symlinks|reparse"):
        packages.capture_source_generation(linked_root / "audio" / "a.m4a")
    target = tmp_path / "target"
    target.mkdir()
    linked_target = tmp_path / "linked-target"
    linked_target.symlink_to(target, target_is_directory=True)
    with pytest.raises(packages.ArtifactPackageError, match="symlinks|reparse"):
        packages.create_artifact_package(linked_target, [source], operation_id="copy", plan_sha256="a" * 64, batch_index=0)


def test_portable_case_collision_and_path_escape_are_rejected_before_copy(tmp_path):
    sources = [_source(tmp_path / "source", "one.m4a", b"one"), _source(tmp_path / "source", "two.m4a", b"two")]
    target = tmp_path / "target"
    target.mkdir()
    selected = [replace(sources[0], storage_key="audio/A.m4a"), replace(sources[1], storage_key="audio/a.m4a")]
    with pytest.raises(packages.ArtifactPackageError, match="colliding|collision"):
        packages.create_artifact_package(target, selected, operation_id="copy", plan_sha256="a" * 64, batch_index=0)
    with pytest.raises(packages.ArtifactPackageError, match="unsafe"):
        packages.create_artifact_package(target, [replace(sources[0], storage_key="audio/../out")],
                                         operation_id="copy", plan_sha256="a" * 64, batch_index=0)


def test_zip64_directory_is_supported_without_loading_entire_object(tmp_path, monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    result, sources, _target = _create(tmp_path)
    restored = tmp_path / "restored.m4a"
    packages.restore_package_object(Path(result["package_path"]), sources[0].object_id, restored,
                                    expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)
    assert restored.read_bytes() == b"first"


def test_central_directory_allocation_is_bounded_before_zipfile_opens(tmp_path, monkeypatch):
    dangerous = tmp_path / "oversized-directory.zip"
    dangerous.write_bytes(struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 2, 2, 65 * 1024 * 1024, 0, 0))

    def forbidden_open(*args, **kwargs):
        pytest.fail("unbounded ZIP central directory must be refused before zipfile allocation")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden_open)
    with pytest.raises(packages.ArtifactPackageError, match="bounded"):
        packages.read_package_manifest(dangerous)


def test_duplicate_json_keys_and_bad_existing_sealed_package_are_rejected(tmp_path):
    result, sources, target = _create(tmp_path)
    archive = Path(result["package_path"])
    broken = tmp_path / "duplicate-key.zip"
    with zipfile.ZipFile(archive) as incoming, zipfile.ZipFile(broken, "w") as outgoing:
        for member in incoming.infolist():
            content = incoming.read(member)
            if member.filename == "package.json":
                content = b'{"format":"duplicate",' + content[1:]
            outgoing.writestr(member.filename, content)
    with pytest.raises(packages.ArtifactPackageError, match="duplicate package JSON"):
        packages.read_package_manifest(broken)
    _rewrite(archive, tmp_path / "corrupt-copy.zip", data_edit=lambda content: b"!" + content[1:])
    archive.write_bytes((tmp_path / "corrupt-copy.zip").read_bytes())
    with pytest.raises(packages.ArtifactPackageError, match="SHA-256"):
        packages.create_artifact_package(target, sources, operation_id="transfer-test",
                                         plan_sha256="a" * 64, batch_index=0)
    assert sources[0].source_path.read_bytes() == b"first"


def test_concurrent_destination_creation_is_never_overwritten(tmp_path, monkeypatch):
    result, sources, _target = _create(tmp_path)
    destination = tmp_path / "restored.m4a"
    original = packages._install_no_replace

    def create_competitor(stage, target):
        target.write_bytes(b"concurrent writer")
        return original(stage, target)

    monkeypatch.setattr(packages, "_install_no_replace", create_competitor)
    with pytest.raises(packages.ArtifactPackageError):
        packages.restore_package_object(Path(result["package_path"]), sources[0].object_id, destination,
                                        expected_sha256=sources[0].sha256, expected_size=sources[0].size_bytes)
    assert destination.read_bytes() == b"concurrent writer"
    assert not list(tmp_path.glob(".artifact-restore-stage-*"))
