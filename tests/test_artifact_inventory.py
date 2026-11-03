"""Shared bounded hashing and portable inventories preserve selected bytes."""
from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from bili_asr.artifact_inventory import (
    ArtifactInventoryError, check_artifact_collisions, collect_artifacts,
    portable_artifact_parts, stream_hash,
)
from bili_asr.artifacts import BUNDLE_BASENAMES, owns_bundle_paths


def test_stream_hash_is_bounded_and_copies_exactly_the_hashed_bytes():
    content = bytes(range(256)) * 12000

    class BoundedSource(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    destination = BytesIO()
    assert stream_hash(BoundedSource(content), destination) == (len(content), hashlib.sha256(content).hexdigest())
    assert destination.getvalue() == content
    assert stream_hash(BytesIO(b"")) == (0, hashlib.sha256(b"").hexdigest())


def test_inventory_preserves_ordered_root_precedence_and_full_recursive_inventory(tmp_path):
    configured, archive = tmp_path / "products", tmp_path / "archive"
    for root in (configured, archive):
        (root / "transcripts" / "bundle").mkdir(parents=True)
        (root / "transcripts" / "bundle" / "bundle.txt").write_bytes(root.name.encode())
    (archive / "documents").mkdir()
    (archive / "documents" / "draft.md").write_bytes(b"draft")
    found = collect_artifacts((configured, archive))
    assert found == {
        "transcripts/bundle/bundle.txt": configured / "transcripts" / "bundle" / "bundle.txt",
        "documents/draft.md": archive / "documents" / "draft.md",
    }
    assert found["transcripts/bundle/bundle.txt"].read_bytes() == b"products"


@pytest.mark.parametrize("path", [
    "audio/../out", "audio//piece", "audio\\piece", "audio/CON.bin",
    "audio/LPT².bin", "audio/name.", "audio/name ", "audio/has:colon", "audio/has\x7fcontrol",
    "private/data", "/audio/piece", "audio/" + "é" * 128,
])
def test_portable_inventory_rejects_unsafe_or_unrepresentable_paths(path):
    with pytest.raises(ArtifactInventoryError):
        portable_artifact_parts(path)


@pytest.mark.parametrize("paths", [
    ["audio/Sound", "audio/sound"],
    ["audio/é", "audio/e\u0301"],
    ["audio/part", "audio/part/piece"],
    ["documents/Part/a", "documents/part/b"],
])
def test_portable_inventory_refuses_file_directory_unicode_and_case_collisions(paths):
    with pytest.raises(ArtifactInventoryError):
        check_artifact_collisions(paths)


def test_inventory_refuses_pending_artifacts_instead_of_silently_omitting_them(tmp_path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "record.m4a.partial").write_bytes(b"pending")
    with pytest.raises(ArtifactInventoryError, match="unfinished"):
        collect_artifacts((tmp_path,))


def test_inventory_refuses_links_in_recursed_directories(tmp_path):
    (tmp_path / "audio").mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (tmp_path / "audio" / "linked").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ArtifactInventoryError, match="symlinks"):
        collect_artifacts((tmp_path,))


def test_bundle_ownership_requires_one_complete_canonical_directory():
    paths = {key: f"transcripts/part-1/{name}" for key, name in BUNDLE_BASENAMES.items()}
    assert owns_bundle_paths(paths)
    assert not owns_bundle_paths({key: path for key, path in paths.items() if key != "vtt_path"})
    assert not owns_bundle_paths({**paths, "md_path": "transcripts/part-2/bundle.md"})
    assert not owns_bundle_paths({**paths, "srt_path": "transcripts//part-1/bundle.srt"})
    assert not owns_bundle_paths({**paths, "srt_path": "transcripts/../bundle.srt"})
    assert not owns_bundle_paths({**paths, "srt_path": "transcripts\\part-1\\bundle.srt"})
