"""Private input failures preserve bytes, release capacity and reject ABA writes."""
import ctypes
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from types import SimpleNamespace

import pytest

from bili_asr import file_generation
from bili_asr.artifact_fences import resource_fence
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services import asr_input_space
from bili_asr.services import workflow_audio_input as inputs
from bili_asr.storage import open_database
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.workflow_errors import JobExecutionError
from tests.support.artifact_online import archive as online_archive
from tests.test_workflow_control_plane import _seed_part


@pytest.fixture
def audio_source(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        _seed_part(connection)
        source = tmp_path / "audio/input.m4a"
        source.parent.mkdir()
        content = b"source bytes must survive every rejected input"
        source.write_bytes(content)
        dependency = {"storage_key": "audio/input.m4a", "sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content)}
        yield connection, ArtifactRoots.of(tmp_path), source, content, dependency
        assert not list(source.parent.glob(".workflow-audio-input-*"))
        assert not list((tmp_path / ".asr-input-reservations").glob("*.json"))


@pytest.mark.parametrize("change,reason", [
    ({"storage_key": None}, "dependency-storage-key-missing"),
    ({"sha256": True}, "invalid-sha256"),
    ({"object_id": "f" * 64}, "dependency-sha-conflict"),
    ({"byte_size": True}, "invalid-byte-size"),
    ({"byte_size": 0}, "invalid-byte-size"),
])
def test_invalid_dependency_identity_rejects_before_reservation_or_private_bytes(audio_source, change, reason):
    connection, roots, source, content, dependency = audio_source
    with (pytest.raises(JobExecutionError) as error,
          inputs.verified_audio_input(connection, roots, 1, {**dependency, **change}, retained=None, checkpoint=lambda: None)):
        pytest.fail("invalid identity must not reach a decoder")
    assert error.value.safe_details["reason"] == reason
    assert source.read_bytes() == content and not (roots.write_base / ".asr-input-reservations").exists()


def test_unusable_write_directory_cannot_redirect_private_input_to_legacy_source(audio_source, tmp_path):
    connection, roots, source, content, dependency = audio_source
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "audio").write_bytes(b"not a directory")
    roots = ArtifactRoots.of(roots.archive_root, artifacts)
    with (pytest.raises(JobExecutionError) as error,
          inputs.verified_audio_input(connection, roots, 1, dependency, retained=None, checkpoint=lambda: None)):
        pytest.fail("unavailable write root cannot borrow a read-only source root")
    assert error.value.error_code == "audio_input_space_unavailable"
    assert error.value.safe_details["reason"] == "private-input-directory-unavailable"
    assert source.read_bytes() == content and (artifacts / "audio").read_bytes() == b"not a directory"


@pytest.mark.parametrize("interrupted", [False, True])
def test_copy_failure_removes_partial_private_inode_and_capacity_receipt(audio_source, monkeypatch, interrupted):
    connection, roots, source, content, dependency = audio_source
    class CopyInterrupted(BaseException):
        pass
    def failed_copy(_source, destination, _checkpoint):
        destination.write(b"partially copied bytes")
        raise CopyInterrupted() if interrupted else PermissionError("offline denied destination")
    monkeypatch.setattr(inputs, "_clone_or_copy", failed_copy)
    with (pytest.raises(CopyInterrupted if interrupted else JobExecutionError) as error,
          inputs.verified_audio_input(connection, roots, 1, dependency, retained=None, checkpoint=lambda: None)):
        pytest.fail("partial bytes must not reach inference")
    if not interrupted:
        assert error.value.error_code == "audio_missing"
    assert source.read_bytes() == content
    assert not list(source.parent.glob(".workflow-audio-input-*"))
    assert not list((roots.write_base / ".asr-input-reservations").glob("*.json"))


@pytest.mark.parametrize("damage", ["private_aba", "source_missing"])
def test_post_inference_generation_failure_discards_output_and_releases_capacity(audio_source, damage):
    connection, roots, source, content, dependency = audio_source
    with inputs.verified_audio_input(connection, roots, 1, dependency, retained=None, checkpoint=lambda: None) as fixed:
        assert fixed.path.read_bytes() == content
        if damage == "private_aba":
            initial = fixed.path.stat()
            fixed.path.write_bytes(b"x" * len(content))
            fixed.path.write_bytes(content)
            os.utime(fixed.path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
            assert fixed.path.read_bytes() == content and fixed.path.stat().st_mtime_ns == initial.st_mtime_ns
        else:
            source.unlink()
        with pytest.raises(JobExecutionError) as error:
            fixed.assert_unchanged()
        assert error.value.error_code == "audio_generation_changed"
        assert error.value.safe_details["stage"] == "after-inference"
        if damage == "source_missing":
            assert error.value.safe_details["reason"] == "source-generation-unavailable"
    assert not fixed.path.exists()


def test_source_aba_during_copy_cannot_yield_an_input_even_when_digest_and_mtime_match(audio_source, monkeypatch):
    connection, roots, source, content, dependency = audio_source
    original = inputs._clone_or_copy
    initial = source.stat()
    def copy_then_restore(stream, destination, checkpoint):
        identity = original(stream, destination, checkpoint)
        source.write_bytes(b"x" * len(content))
        source.write_bytes(content)
        os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        return identity
    monkeypatch.setattr(inputs, "_clone_or_copy", copy_then_restore)
    with (pytest.raises(JobExecutionError) as error,
          inputs.verified_audio_input(connection, roots, 1, dependency, retained=None, checkpoint=lambda: None)):
        pytest.fail("a source modified during verification must not yield decoder input")
    assert error.value.error_code == "audio_generation_changed"
    assert error.value.safe_details["stage"] == "before-inference"
    assert source.read_bytes() == content and source.stat().st_mtime_ns == initial.st_mtime_ns
    assert not list(source.parent.glob(".workflow-audio-input-*"))


def test_retained_object_uses_verified_alternate_local_key_and_keeps_replica_facts(tmp_path):
    archive = online_archive.__wrapped__(tmp_path)
    with closing(open_database(archive.root)) as connection:
        catalog = ArtifactCatalog(connection)
        alternate = archive.root / "audio/alternate.m4a"
        alternate.write_bytes(archive.data)
        with connection:
            catalog.register_target("alternate-local", kind="local")
            catalog.record_verified_replica(archive.digest, "alternate-local", "audio/alternate.m4a",
                                           sha256=archive.digest, byte_size=len(archive.data))
        retained = {"object_id": archive.digest, "byte_size": len(archive.data)}
        rows = [tuple(row) for row in connection.execute("SELECT * FROM artifact_replicas")]
        with inputs.verified_audio_input(connection, archive.roots, 1,
                {"storage_key": "audio/missing-prerequisite.m4a", "object_id": archive.digest},
                retained=retained, checkpoint=lambda: None) as fixed:
            assert fixed.path.read_bytes() == archive.data and fixed.source_key == "audio/alternate.m4a"
            fixed.assert_unchanged()
        assert alternate.read_bytes() == archive.data
        assert [tuple(row) for row in connection.execute("SELECT * FROM artifact_replicas")] == rows


def test_capacity_owner_lock_refuses_input_without_copy_or_refund(audio_source, monkeypatch):
    connection, roots, source, content, dependency = audio_source
    ready, release = Event(), Event()
    def owner():
        with resource_fence(roots, "capacity", exclusive=True):
            ready.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(owner)
        try:
            assert ready.wait(10)
            clock = iter((0.0, 6.0))
            monkeypatch.setattr(asr_input_space, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
            with (pytest.raises(JobExecutionError) as error,
                  inputs.verified_audio_input(connection, roots, 1, dependency, retained=None, checkpoint=lambda: None)):
                pytest.fail("a competing real capacity owner must block copying")
            assert error.value.safe_details["reason"] == "reservation-busy"
            assert source.read_bytes() == content
        finally:
            release.set()
            pending.result(timeout=10)


@pytest.mark.parametrize("clone_supported", [False, True])
def test_copy_fallback_truncates_failed_clone_and_success_hashes_exact_destination(tmp_path, monkeypatch, clone_supported):
    source_path, destination_path = tmp_path / "source", tmp_path / "destination"
    content = b"real bounded source bytes"
    source_path.write_bytes(content)
    checkpoints = []
    with source_path.open("rb") as source, destination_path.open("w+b") as destination:
        def ioctl(target_fd, operation, source_fd):
            assert target_fd == destination.fileno() and source_fd == source.fileno() and operation == 0x40049409
            destination.write(content if clone_supported else b"failed-clone-left-longer-stale-tail" * 3)
            destination.flush()
            if not clone_supported:
                raise OSError("filesystem does not support clone")
        monkeypatch.setattr(inputs, "os", SimpleNamespace(name="posix"))
        monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
        result = inputs._clone_or_copy(source, destination, lambda: checkpoints.append(True))
    assert result == (len(content), hashlib.sha256(content).hexdigest())
    assert destination_path.read_bytes() == content and checkpoints


@pytest.mark.parametrize("api_success", [False, True])
def test_windows_change_clock_is_part_of_descriptor_identity_and_api_failure_is_explicit(tmp_path, monkeypatch, api_success):
    path = tmp_path / "clock-source"
    path.write_bytes(b"descriptor-backed test bytes")
    class ReadInformation:
        def __call__(self, handle, information_class, pointer, size):
            assert handle == 123 and information_class == 0 and size == ctypes.sizeof(pointer._obj)
            pointer._obj.ChangeTime = 987654321
            return api_success
    read = ReadInformation()
    monkeypatch.setattr(file_generation, "os", SimpleNamespace(name="nt", fstat=os.fstat))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: SimpleNamespace(GetFileInformationByHandleEx=read), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "offline denied change-time query"), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 123))
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if api_success:
            generation = file_generation.stream_generation(stream)
            assert generation == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, 987654321)
        else:
            with pytest.raises(OSError) as error:
                file_generation.stream_generation(stream)
            assert error.value.errno == 5
    assert read.argtypes[1] is ctypes.c_int and read.restype is ctypes.wintypes.BOOL
