"""Filesystem release preserves frozen bytes across faults and pathname races."""
from __future__ import annotations

import hashlib
import os

import pytest

from bili_asr.artifact_packages import capture_source_generation
from bili_asr.services import artifact_release as release


@pytest.fixture
def audio(tmp_path):
    directory = tmp_path / "audio"
    directory.mkdir()
    path = directory / "input.m4a"
    content = b"original retained sound"
    path.write_bytes(content)
    return tmp_path, path, content, hashlib.sha256(content).hexdigest(), capture_source_generation(path)


def _release(audio, *, quarantine_key="audio/.artifact-release-test", allow_delete=True, isolated=None):
    root, _path, _content, digest, generation = audio
    return release.release_copy(root, "audio/input.m4a", quarantine_key, digest, generation,
                                allow_delete=allow_delete, isolated=isolated)


@pytest.mark.parametrize("unsafe_key", ["audio/..\\foreign.m4a", "audio/.artifact-release-..\\foreign.m4a"])
def test_quarantine_windows_path_escape_is_rejected_before_any_mutation(audio, unsafe_key):
    root, source, content, _digest, _generation = audio
    foreign = root / "foreign.m4a"
    foreign.write_bytes(b"foreign bytes")
    with pytest.raises(ValueError, match="unsafe"):
        _release(audio, quarantine_key=unsafe_key)
    assert source.read_bytes() == content
    assert foreign.read_bytes() == b"foreign bytes"


def test_existing_different_quarantine_is_preserved_and_never_overwritten(audio):
    root, source, content, _digest, _generation = audio
    quarantine = root / "audio/.artifact-release-test"
    quarantine.write_bytes(b"unrelated investigation evidence")
    with pytest.raises(ValueError, match="generation|bytes"):
        _release(audio)
    assert source.read_bytes() == content
    assert quarantine.read_bytes() == b"unrelated investigation evidence"


def test_target_error_in_last_callback_keeps_bytes_and_guarded_replay_restores_input(audio):
    root, source, content, _digest, _generation = audio
    quarantine = root / "audio/.artifact-release-test"

    def target_offline():
        raise OSError("prepared target was disconnected")

    with pytest.raises(OSError, match="target was disconnected"):
        _release(audio, isolated=target_offline)
    assert quarantine.read_bytes() == content
    assert not source.exists() or source.read_bytes() == content
    outcome = _release(audio, allow_delete=False)
    assert outcome["state"] == "cancelled" and outcome["released_bytes"] == 0
    assert source.read_bytes() == content
    assert not quarantine.exists()


def test_callback_editing_same_inode_and_restoring_mtime_is_refused(audio):
    root, source, content, _digest, generation = audio
    quarantine = root / "audio/.artifact-release-test"
    changed = b"x" * len(content)

    def mutate_then_hide_mtime():
        before = quarantine.stat()
        with quarantine.open("r+b") as writer:
            writer.write(changed)
            writer.flush()
            os.fsync(writer.fileno())
        os.utime(quarantine, ns=(before.st_atime_ns, generation["mtime_ns"]))
        assert quarantine.stat().st_ino == before.st_ino
        assert quarantine.stat().st_mtime_ns == generation["mtime_ns"]

    with pytest.raises(ValueError, match="changed|generation"):
        _release(audio, isolated=mutate_then_hide_mtime)
    assert quarantine.read_bytes() == changed
    assert not source.exists() or source.read_bytes() == changed


def test_two_hardlinked_registered_paths_count_physical_content_release_once(audio):
    root, source, content, digest, _generation = audio
    alias = root / "audio/alias.m4a"
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("hardlinks unavailable")
    first_generation = capture_source_generation(source)
    second_generation = capture_source_generation(alias)
    first = release.release_copy(root, "audio/input.m4a", "audio/.artifact-release-first",
                                 digest, first_generation, allow_delete=True)
    second = release.release_copy(root, "audio/alias.m4a", "audio/.artifact-release-second",
                                  digest, second_generation, allow_delete=True)
    assert first["state"] == second["state"] == "released"
    assert first["released_bytes"] == 0
    assert second["released_bytes"] == len(content)
    assert not list((root / "audio").iterdir())


def test_concurrent_quarantine_creation_refuses_to_clobber_either_copy(audio, monkeypatch):
    root, source, content, _digest, _generation = audio
    quarantine = root / "audio/.artifact-release-test"
    primitive = "rename" if os.name == "nt" else "link"
    original = getattr(release.os, primitive)

    def create_competitor(first, second, *args, **kwargs):
        quarantine.write_bytes(b"concurrent writer")
        return original(first, second, *args, **kwargs)

    monkeypatch.setattr(release.os, primitive, create_competitor)
    with pytest.raises(FileExistsError):
        _release(audio)
    assert source.read_bytes() == content
    assert quarantine.read_bytes() == b"concurrent writer"


def test_callback_replacing_source_generation_keeps_replacement_and_quarantine(audio):
    if os.name == "nt":
        pytest.skip("Windows isolation already moves the original source pathname")
    root, source, content, _digest, _generation = audio
    quarantine = root / "audio/.artifact-release-test"
    replacement = b"new source generation"

    def replace_original_name():
        source.unlink()
        source.write_bytes(replacement)

    with pytest.raises(ValueError, match="changed|generation"):
        _release(audio, isolated=replace_original_name)
    assert source.read_bytes() == replacement
    assert quarantine.read_bytes() == content
