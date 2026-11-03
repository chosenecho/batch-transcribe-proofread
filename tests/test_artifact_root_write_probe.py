"""Configured product roots must support the operations their writers perform."""

from __future__ import annotations

import bili_asr.cli.main as _module_cli_main


import importlib
import os
import stat
from pathlib import Path

import pytest

from bili_asr import artifact_root, cli
from bili_asr.artifact_root import ArtifactRootError, roots_for


def _roots(tmp_root) -> tuple[Path, Path]:
    archive = Path(tmp_root) / "archive"
    artifact = Path(tmp_root) / "artifacts"
    archive.mkdir()
    artifact.mkdir()
    return archive, artifact


@pytest.mark.parametrize("failure", ["create", "write", "short-write", "file-sync", "directory-sync"])
def test_probe_refuses_unwritable_or_unsyncable_roots_and_cleans_up(
    tmp_root, monkeypatch, failure,
) -> None:
    if failure == "directory-sync" and os.name != "posix":
        pytest.skip("directory descriptor sync is a POSIX writer operation")
    archive, artifact = _roots(tmp_root)
    real_open, real_write, real_fsync = os.open, os.write, os.fsync

    def probe_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and failure == "create":
            raise PermissionError("mount accepts reads only")
        return real_open(path, flags, *args, **kwargs)

    def probe_write(descriptor, content):
        if failure == "write":
            raise OSError("mount write failed")
        if failure == "short-write":
            return 0
        return real_write(descriptor, content)

    def probe_fsync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure == "file-sync" and not is_directory) or (
            failure == "directory-sync" and is_directory
        ):
            raise OSError("mount does not support sync")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "open", probe_open)
    monkeypatch.setattr(os, "write", probe_write)
    monkeypatch.setattr(os, "fsync", probe_fsync)

    with pytest.raises(ArtifactRootError, match="cannot be written and synced") as error:
        roots_for(archive, flag_value=str(artifact), environ={})

    assert str(artifact) in str(error.value)
    assert list(artifact.iterdir()) == []
    assert list(archive.iterdir()) == []


def test_probe_writes_and_syncs_a_real_file_before_removing_it(tmp_root, monkeypatch) -> None:
    archive, artifact = _roots(tmp_root)
    events = []
    real_write, real_fsync, real_unlink = os.write, os.fsync, os.unlink

    def traced_write(descriptor, content):
        assert len(list(artifact.iterdir())) == 1
        events.append("write")
        return real_write(descriptor, content)

    def traced_fsync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        events.append("directory-sync" if is_directory else "file-sync")
        if not is_directory:
            assert next(artifact.iterdir()).read_bytes() == b"bili-asr artifact-root probe\n"
        return real_fsync(descriptor)

    def traced_unlink(path, *args, **kwargs):
        events.append("remove")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "write", traced_write)
    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "unlink", traced_unlink)
    assert roots_for(archive, flag_value=str(artifact), environ={}).write_base == artifact

    expected = ["write", "file-sync"]
    if os.name == "posix":
        expected.append("directory-sync")
    expected.append("remove")
    if os.name == "posix":
        expected.append("directory-sync")
    assert events == expected
    assert list(artifact.iterdir()) == []


def test_read_only_root_validation_does_not_create_a_probe(tmp_root, monkeypatch) -> None:
    archive, artifact = _roots(tmp_root)
    real_open = os.open

    def deny_creation(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            pytest.fail("reading products must not create a probe")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_creation)
    roots = roots_for(archive, flag_value=str(artifact), environ={}, require_writable=False)
    assert roots.write_base == artifact
    assert list(artifact.iterdir()) == []


def test_identity_root_remains_a_no_op(tmp_root, monkeypatch) -> None:
    root = Path(tmp_root) / "absent"

    def refuse_any_probe(_path):
        pytest.fail("the identity root must preserve the existing no-op boundary")

    monkeypatch.setattr(artifact_root, "_probe_writable", refuse_any_probe)
    assert not roots_for(root, flag_value=str(root), environ={}).configured
    assert not root.exists()




def test_cli_search_only_reads_the_artifact_root(tmp_root, monkeypatch) -> None:
    archive, artifact = _roots(tmp_root)
    monkeypatch.setattr(artifact_root, "_probe_writable", lambda _path: pytest.fail("search writes no product"))
    monkeypatch.setattr(_module_cli_main, "_dispatch_command", lambda args: 0)
    assert _module_cli_main.main([
        "search", "query", "--archive-root", str(archive), "--artifact-root", str(artifact),
    ]) == 0


@pytest.mark.skip(reason="legacy asr command was replaced by SQLite workflow handlers")
def test_cli_rejects_an_unsyncable_root_before_taking_the_writer_lock(
    tmp_root, monkeypatch, capsys,
) -> None:
    archive, artifact = _roots(tmp_root)
    real_fsync = os.fsync

    def fail_probe_sync(descriptor):
        if os.name != "posix" or stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("mount does not support sync")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_probe_sync)
    monkeypatch.setattr(_module_cli_main, "_dispatch_command", lambda args: pytest.fail("writer must not start"))
    assert _module_cli_main.main([
        "asr", "--archive-root", str(archive), "--artifact-root", str(artifact),
    ]) == 1

    assert capsys.readouterr().err == f"asr: artifact root cannot be written and synced ({artifact})\n"
    assert list(artifact.iterdir()) == []
    assert list(archive.iterdir()) == []


@pytest.mark.skip(reason="derive-audio-inventory command was retired with the manifest queue")
def test_audio_inventory_reuses_the_invocations_resolved_roots(tmp_root, monkeypatch, capsys) -> None:
    archive, artifact = _roots(tmp_root)
    cli_main = importlib.import_module('bili_asr.cli.main.main')
    cli_queue = importlib.import_module("bili_asr.cli.queue")
    calls = []
    original = cli_main.roots_for

    def count_resolution(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli_main, "roots_for", count_resolution)
    # The handler used to independently resolve the roots a second time.
    monkeypatch.setattr(artifact_root, "roots_for", lambda *args, **kwargs: pytest.fail("duplicate resolution"))
    monkeypatch.setattr(cli_queue, "_open_subtitle_connection", lambda *args, **kwargs: None)
    assert _module_cli_main.main([
        "derive-audio-inventory", "--archive-root", str(archive), "--artifact-root", str(artifact),
    ]) == 1
    assert len(calls) == 1
    assert calls[0]["require_writable"] is False
    assert list(artifact.iterdir()) == []
