"""Lossless compressed payloads use their own identity and bounded readers."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from bili_asr import artifact_packages as packages
from bili_asr.contracts.json_schema import validate_json
from tests.test_artifact_packages import _source


def build(tmp_path, content=b"retained exact audio" * 200000):
    target = tmp_path / "target"
    target.mkdir()
    source = _source(tmp_path / "source", "input.m4a", content)
    result = packages.create_artifact_package(target, (source,), operation_id="compressed-test",
        plan_sha256="a" * 64, batch_index=0, format_version=2, compression_level=6)
    return result, source


def test_v2_streams_lossless_compression_verifies_restores_and_keeps_v1_identity(tmp_path):
    result, source = build(tmp_path)
    manifest = packages.package_manifest((source,), "compressed-test", "a" * 64, 0,
                                        format_version=2)
    validate_json("artifact-package-v2", manifest)
    old = packages.package_manifest((source,), "compressed-test", "a" * 64, 0)
    assert old["format_version"] == 1 and "compression" not in old
    assert old["package_id"] != manifest["package_id"]
    statistics = result["compression_statistics"]
    assert statistics["original_payload_bytes"] == source.size_bytes
    assert statistics["compressed_payload_bytes"] < source.size_bytes
    assert statistics["container_to_original_ratio"] < 0.01
    path = source.source_path.parents[2] / "target" / result["package_key"]
    assert packages.check_artifact_package(path, expected_sha256=result["package_sha256"]) == result
    with zipfile.ZipFile(path) as bundle:
        assert bundle.getinfo("package.json").compress_type == zipfile.ZIP_STORED
        assert bundle.getinfo(f"objects/{source.sha256}").compress_type == zipfile.ZIP_DEFLATED
    restored = tmp_path / "restored.m4a"
    packages.restore_package_object(path, source.object_id, restored,
        expected_sha256=source.sha256, expected_size=source.size_bytes)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == source.sha256
    assert restored.read_bytes() == source.source_path.read_bytes()


def test_compressed_recovery_completes_short_writes_with_bounded_memory(tmp_path):
    result, source = build(tmp_path)
    class ShortWriter(io.BytesIO):
        def write(self, block):
            assert len(block) <= 1024**2
            return super().write(block[:4096])
    outgoing = ShortWriter()
    packages.copy_package_object(result["package_path"], source.sha256, outgoing,
        expected_size=source.size_bytes, package_id=result["package_id"], manifest_sha256=result["manifest_sha256"])
    assert hashlib.sha256(outgoing.getvalue()).hexdigest() == source.sha256


@pytest.mark.parametrize("change", ["old_version", "method", "manifest_compressed", "wrong_payload_method"])
def test_payload_reader_rejects_declared_format_mismatch(tmp_path, change):
    result, _source_record = build(tmp_path, b"retained sound")
    output = tmp_path / "broken.zip"
    with zipfile.ZipFile(result["package_path"]) as incoming, zipfile.ZipFile(output, "w") as outgoing:
        for info in incoming.infolist():
            content = incoming.read(info)
            method = info.compress_type
            if info.filename == "package.json":
                manifest = json.loads(content)
                if change == "old_version":
                    manifest["format_version"] = 1
                    manifest.pop("compression")
                if change == "method":
                    manifest["compression"]["method"] = "unsupported"
                content = json.dumps(manifest).encode()
                if change == "manifest_compressed":
                    method = zipfile.ZIP_DEFLATED
            elif change == "wrong_payload_method":
                method = zipfile.ZIP_STORED
            outgoing.writestr(info.filename, content, compress_type=method)
    with pytest.raises(packages.ArtifactPackageError):
        packages.check_artifact_package(output)


def test_corrupt_compressed_payload_fails_without_publishing_restore(tmp_path):
    result, source = build(tmp_path, b"exact bytes" * 10000)
    output = tmp_path / "broken.zip"
    with zipfile.ZipFile(result["package_path"]) as incoming, zipfile.ZipFile(output, "w") as outgoing:
        for info in incoming.infolist():
            content = incoming.read(info)
            if info.filename.startswith("objects/"):
                content = b"X" + content[1:]
            outgoing.writestr(info.filename, content, compress_type=info.compress_type)
    restored = tmp_path / "restored.m4a"
    with pytest.raises(packages.ArtifactPackageError, match="SHA-256"):
        packages.restore_package_object(output, source.object_id, restored,
            expected_sha256=source.sha256, expected_size=source.size_bytes)
    assert not restored.exists()
    assert not list(tmp_path.glob(".artifact-restore-stage-*"))


def test_already_compressed_bytes_report_actual_container_growth(tmp_path):
    import random
    result, source = build(tmp_path, random.Random(406).randbytes(20000))
    assert result["compression_statistics"]["container_to_original_ratio"] > 1
    assert source.source_path.stat().st_size == source.size_bytes
