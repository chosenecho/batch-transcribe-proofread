"""Real subprocess wire tests use the same agent and bounded stream protocol as SSH."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from bili_asr.remote_storage import RemoteStorageError, SSHBinding, SSHDirectoryBackend, read_ssh_binding

pytestmark = pytest.mark.skipif(os.name != "posix", reason="supported backend is Linux/OpenSSH/Python 3")


class _Channel:
    def __init__(self, process):
        self.process = process
    def shutdown_write(self):
        self.process.stdin.close()
    def recv_exit_status(self):
        return self.process.wait(timeout=15)


class _Stream:
    def __init__(self, stream, channel):
        self.stream, self.channel = stream, channel
    def __getattr__(self, name):
        return getattr(self.stream, name)


class _LocalSSH:
    def exec_command(self, command, timeout):
        parts = shlex.split(command)
        process = subprocess.Popen([sys.executable, *parts[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        channel = _Channel(process)
        return tuple(_Stream(stream, channel) for stream in (process.stdin, process.stdout, process.stderr))


@pytest.fixture
def backend(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    binding = SSHBinding("remote-test", None, "unused", str(root))
    backend = SSHDirectoryBackend(binding)
    backend.client = _LocalSSH()
    prepared = backend.prepare()
    backend.binding = dataclasses.replace(binding, instance_id=prepared["instance_id"])
    return backend


def _payload(tmp_path):
    data = bytes(range(256)) * 4096
    source = tmp_path / "source.zip"
    source.write_bytes(data)
    return source, data, hashlib.sha256(data).hexdigest(), "packages/artifact-" + "a" * 64 + ".zip"


def test_real_wire_upload_readback_range_and_idempotence(backend, tmp_path):
    source, data, digest, key = _payload(tmp_path)
    receipt = backend.upload(source, key=key, sha256=digest, size=len(data))
    assert receipt["verification_scope"] == "entire-package-client-readback" and not receipt["source_released"]
    assert source.read_bytes() == data
    assert backend.upload(source, key=key, sha256=digest, size=len(data))["sha256"] == digest
    result = io.BytesIO()
    report = backend.download(key=key, sha256=digest, size=len(data), destination=result, offset=19, count=33)
    assert result.getvalue() == data[19:52] and not report["whole_object_verified"]
    assert not list(Path(backend.binding.root).rglob(".upload-*"))


@pytest.mark.parametrize("damage", ["offline", "identity", "same-size-corrupt", "symlink"])
def test_remote_failures_never_release_or_return_false_verification(backend, tmp_path, damage):
    source, data, digest, key = _payload(tmp_path)
    backend.upload(source, key=key, sha256=digest, size=len(data))
    root = Path(backend.binding.root)
    if damage == "offline":
        root.rename(tmp_path / "elsewhere")
    elif damage == "identity":
        marker = root / ".bili-asr-ssh-target.json"
        value = json.loads(marker.read_bytes())
        value["instance_id"] = "f" * 32
        marker.write_text(json.dumps(value))
    elif damage == "same-size-corrupt":
        (root / key).write_bytes(b"x" * len(data))
    else:
        (root / key).unlink()
        (root / key).symlink_to(source)
    with pytest.raises(RemoteStorageError):
        backend.download(key=key, sha256=digest, size=len(data), destination=io.BytesIO())
    assert source.read_bytes() == data


def test_upload_interruption_has_no_published_package_and_keeps_source(backend, tmp_path):
    source, data, digest, key = _payload(tmp_path)
    incoming, outgoing, errors = backend._start("put", key=key, sha256=digest, size=len(data))
    incoming.write(data[:500])
    incoming.flush()
    incoming.channel.shutdown_write()
    with pytest.raises(RemoteStorageError, match="upload_interrupted"):
        backend._response(outgoing)
    assert outgoing.channel.recv_exit_status() == 1
    incoming.close()
    outgoing.close()
    errors.close()
    assert not (Path(backend.binding.root) / key).exists()
    assert source.read_bytes() == data
    assert backend.upload(source, key=key, sha256=digest, size=len(data))["sha256"] == digest


def test_immutable_remote_key_does_not_overwrite_different_existing_bytes(backend, tmp_path):
    source, data, digest, key = _payload(tmp_path)
    backend.upload(source, key=key, sha256=digest, size=len(data))
    source.write_bytes(b"b" * len(data))
    with pytest.raises(RemoteStorageError, match="checksum_mismatch"):
        backend.upload(source, key=key, sha256=hashlib.sha256(source.read_bytes()).hexdigest(), size=len(data))
    assert (Path(backend.binding.root) / key).read_bytes() == data


def test_rate_limit_applies_to_upload_and_download_without_reporting_secrets(backend, tmp_path, monkeypatch):
    source, data, digest, key = _payload(tmp_path)
    seen = []
    monkeypatch.setattr(backend, "_throttle", lambda count, started: seen.append(count))
    receipt = backend.upload(source, key=key, sha256=digest, size=len(data))
    assert seen == [len(data), len(data)]
    assert "unused" not in json.dumps(receipt) and backend.binding.root not in json.dumps(receipt)


@pytest.mark.parametrize("unsafe", ["https://example/?token=secret", "../escape", "/absolute", "packages/a.zip"])
def test_remote_agent_confines_immutable_package_namespace(backend, unsafe):
    with pytest.raises(RemoteStorageError, match="invalid_key"):
        backend.download(key=unsafe, sha256="a" * 64, size=1, destination=io.BytesIO())


def test_binding_rejects_inline_credentials_and_unknown_backend(tmp_path):
    path = tmp_path / "binding.json"
    base = {"backend": "ssh-directory/v1", "target_id": "cold", "instance_id": None,
            "host": "server", "root": "/store"}
    path.write_text(json.dumps({**base, "password": "must-never-be-accepted"}))
    with pytest.raises(RemoteStorageError, match="invalid_binding"):
        read_ssh_binding(path)
    path.write_text(json.dumps({**base, "backend": "s3"}))
    with pytest.raises(RemoteStorageError, match="unsupported_backend"):
        read_ssh_binding(path)


@pytest.mark.parametrize("failure,code", [("authentication", "authentication"), ("bad_host", "host_identity"),
                                         ("offline", "connection_or_host_identity")])
def test_ssh_authentication_and_host_failures_are_classified_without_secret_echo(tmp_path, monkeypatch, failure, code):
    class SSHError(Exception):
        pass
    class Authentication(SSHError):
        pass
    class BadHost(SSHError):
        pass
    loaded = []
    class Client:
        def load_system_host_keys(self):
            loaded.append("known-hosts")
        def set_missing_host_key_policy(self, policy):
            loaded.append("reject-policy")
        def close(self):
            loaded.append("closed")
        def connect(self, *args, **kwargs):
            assert kwargs["password"] == "SECRET-SENTINEL"
            raise {"authentication": Authentication, "bad_host": BadHost, "offline": OSError}[failure]("SECRET-SENTINEL")
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(SSHClient=Client, RejectPolicy=object,
                        AuthenticationException=Authentication, BadHostKeyException=BadHost, SSHException=SSHError))
    monkeypatch.setenv("TEST_REMOTE_PASSWORD", "SECRET-SENTINEL")
    binding = SSHBinding("remote-test", "a" * 32, "server", "/root/storage", password_env="TEST_REMOTE_PASSWORD")
    with pytest.raises(RemoteStorageError) as error:
        with SSHDirectoryBackend(binding):
            raise AssertionError("failed authentication must never connect")
    assert error.value.code == code and "SECRET-SENTINEL" not in str(error.value)
    assert loaded == ["known-hosts", "reject-policy", "closed"]


def test_package_copy_restore_preserves_production_authority(backend, tmp_path):
    from bili_asr.services.remote_artifacts import push_remote_package, restore_remote_artifact
    from tests.test_artifact_restore import _authority, _setup
    (tmp_path / "fixture").mkdir()
    roots, _local_target, audio, _source, package, _replica = _setup(tmp_path / "fixture")
    before = _authority(roots)
    source = Path(package["package_path"])
    original = source.read_bytes()
    result = push_remote_package(roots, source, backend)
    assert result["object_count"] == 1 and not result["source_released"]
    restored = restore_remote_artifact(roots, audio["sha256"], audio["storage_key"], backend)
    assert restored["remote_package_verified"] and not restored["automatic_retry"]
    assert (roots.archive_root / audio["storage_key"]).exists()
    assert source.read_bytes() == original and _authority(roots) == before


def test_server_success_cannot_replace_client_checksum_evidence(backend, tmp_path, monkeypatch):
    source, data, digest, key = _payload(tmp_path)
    start = backend._start
    class Done:
        def shutdown_write(self):
            pass
        def recv_exit_status(self):
            return 0
    def forged_download(operation, **options):
        if operation != "get":
            return start(operation, **options)
        channel = Done()
        body = json.dumps({"ok": True, "size": len(data), "offset": 0}).encode() + b"\n" + b"x" * len(data)
        return tuple(_Stream(stream, channel) for stream in (io.BytesIO(), io.BytesIO(body), io.BytesIO()))
    monkeypatch.setattr(backend, "_start", forged_download)
    with pytest.raises(RemoteStorageError, match="checksum_mismatch"):
        backend.upload(source, key=key, sha256=digest, size=len(data))
    assert source.read_bytes() == data
