"""A decoder owns an independent byte fence without storage catalog access."""
import hashlib
import sqlite3

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.artifact_child import child_artifact_access
from bili_asr.artifact_fences import object_fence
from bili_asr.artifact_root import ArtifactRoots


def test_child_byte_fence_blocks_exclusive_release_and_never_opens_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("decoder fence must not connect to the archive database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    path = tmp_path / "audio" / "source.m4a"
    path.parent.mkdir()
    path.write_bytes(b"immutable retained audio")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    roots = ArtifactRoots.of(tmp_path)
    context = {"archive_root": str(tmp_path), "artifact_root": None, "object_id": digest}
    with child_artifact_access(context, str(path)):
        with pytest.raises(ArchiveBusyError), object_fence(roots, digest, exclusive=True):
            pytest.fail("exclusive release entered while the decoder owned its fence")
    with object_fence(roots, digest, exclusive=True):
        pass
    path.write_bytes(b"different generation")
    with pytest.raises(ValueError, match="fenced byte identity"), child_artifact_access(context, path):
        pytest.fail("decoder accepted bytes that do not match the fenced identity")


def test_child_refuses_input_outside_its_bound_audio_roots(tmp_path):
    context = {"archive_root": str(tmp_path), "artifact_root": None, "object_id": "a" * 64}
    with pytest.raises(ValueError, match="outside"), child_artifact_access(context, tmp_path / "other.m4a"):
        pass
