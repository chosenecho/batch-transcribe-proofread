"""Compression changes container bytes, never archived identity or payload."""
import hashlib
import zipfile

import pytest

from bili_asr.services.archive_snapshot import save_snapshot, check_snapshot, restore_snapshot
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_optional_deflate_roundtrips_exact_historical_files(tmp_path):
    source, restored = tmp_path / "source", tmp_path / "restored"
    expected = frozen_archive(source)
    stored, deflated = tmp_path / "stored.zip", tmp_path / "deflated.zip"
    first = save_snapshot(source, stored)
    second = save_snapshot(source, deflated, compression="deflate")
    assert first["file_count"] == second["file_count"]
    assert first["total_bytes"] == second["total_bytes"]
    assert deflated.stat().st_size < stored.stat().st_size
    with zipfile.ZipFile(deflated) as archive:
        assert all(member.compress_type == zipfile.ZIP_DEFLATED for member in archive.infolist())
    assert check_snapshot(deflated)["valid"]
    restore_snapshot(deflated, restored)
    for relative, sha256 in expected["files"].items():
        assert hashlib.sha256((restored / relative).read_bytes()).hexdigest() == sha256
    with pytest.raises(ValueError, match="compression"):
        save_snapshot(source, tmp_path / "unsupported.zip", compression="lossy")
