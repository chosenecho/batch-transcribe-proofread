"""Independent release review: last-copy safety at the final deletion boundary."""
from __future__ import annotations

import os

import pytest

from bili_asr.services import artifact_transfer as service
from bili_asr.services import artifact_release
from tests.test_artifact_transfer import archive, plan


def _retained_local_bytes(archive):
    return [path.read_bytes() for path in (archive.root / "audio").iterdir() if path.is_file()]


def test_hold_added_during_final_package_verification_preserves_local_bytes(archive, monkeypatch):
    frozen = plan(archive)
    holds = {}
    original = service._package_evidence

    def evidence_then_hold(*args, **kwargs):
        result = original(*args, **kwargs)
        holds[f"sha256:{archive.digest}"] = ("new operational retry hold",)
        return result

    monkeypatch.setattr(service, "_package_evidence", evidence_then_hold)
    try:
        service.transfer_artifacts(archive.roots, frozen, target_root=archive.target,
                                   mode="offload", external_holds=lambda: holds)
    except (ValueError, OSError):
        pass
    assert archive.data in _retained_local_bytes(archive)


@pytest.mark.parametrize("change", ["hold", "target_missing"])
def test_guard_is_refreshed_after_final_source_hash(archive, monkeypatch, change):
    frozen = plan(archive)
    holds = {}
    original = artifact_release._verify
    calls = 0

    def source_verified_then_change(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            if change == "hold":
                holds[f"sha256:{archive.digest}"] = ("hold while final source hash ran",)
            else:
                package, = (archive.target / "packages").glob("*.zip")
                package.unlink()
        return result

    monkeypatch.setattr(artifact_release, "_verify", source_verified_then_change)
    try:
        service.transfer_artifacts(archive.roots, frozen, target_root=archive.target,
                                   mode="offload", external_holds=lambda: holds)
    except (ValueError, OSError):
        pass
    assert calls >= 2, "exercise the expensive final source verification"
    assert archive.data in _retained_local_bytes(archive)


@pytest.mark.parametrize("damage", ["missing", "changed_same_size"])
def test_target_damage_after_package_verification_preserves_last_local_copy(archive, monkeypatch, damage):
    frozen = plan(archive)
    original = service._package_evidence

    def evidence_then_damage(*args, **kwargs):
        result = original(*args, **kwargs)
        package, = (archive.target / "packages").glob("*.zip")
        if damage == "missing":
            package.unlink()
        else:
            before = package.stat()
            content = package.read_bytes()
            assert content.count(archive.data) == 1
            package.write_bytes(content.replace(archive.data, b"x" * len(archive.data)))
            os.utime(package, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result

    monkeypatch.setattr(service, "_package_evidence", evidence_then_damage)
    try:
        service.transfer_artifacts(archive.roots, frozen, target_root=archive.target,
                                   mode="offload", external_holds={})
    except (ValueError, OSError):
        pass
    assert archive.data in _retained_local_bytes(archive)
