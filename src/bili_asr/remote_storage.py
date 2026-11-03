"""Explicit SSH directory backend; connection secrets never enter receipts.

Support is limited to Linux OpenSSH servers with Python 3, regular files,
hardlink publication, and file/directory fsync. No cloud API is implied.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, Protocol


BACKEND_CONTRACT = "ssh-directory/v1"
CAPABILITIES = {"upload": True, "immutable_publish": True, "range_read": True,
                "verification": "client-and-server-sha256-readback", "durability": "file-and-directory-fsync",
                "resume_upload": False, "resume_policy": "rebuild-unfinished-batch", "automatic_source_release": False}


class RemoteStorageError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"remote_storage:{code}")


class StorageBackend(Protocol):
    """Capabilities describe actual transport behavior, not hoped-for fallbacks."""
    capabilities: dict

    def probe(self) -> dict: ...
    def upload(self, path: Path, *, key: str, sha256: str, size: int) -> dict: ...
    def download(self, *, key: str, sha256: str, size: int, destination: BinaryIO,
                 offset: int = 0, count: int | None = None) -> dict: ...


@dataclass(frozen=True)
class SSHBinding:
    target_id: str
    instance_id: str | None
    host: str = field(repr=False)
    root: str = field(repr=False)
    username: str = field(default="root", repr=False)
    port: int = 22
    password_env: str | None = field(default=None, repr=False)
    key_filename: str | None = field(default=None, repr=False)
    known_hosts: str | None = field(default=None, repr=False)
    python_executable: str = "python3"
    timeout: float = 30
    max_bytes_per_second: int | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.target_id) or self.target_id in {"local", "local-artifacts"}:
            raise RemoteStorageError("invalid_target")
        if self.instance_id is not None and not re.fullmatch("[0-9a-f]{32}", self.instance_id):
            raise RemoteStorageError("invalid_instance")
        if not self.host or any(char in self.host for char in "\r\n/@") or not self.username or any(char in self.username for char in "\r\n"):
            raise RemoteStorageError("invalid_binding")
        if not self.root.startswith("/") or self.root == "/" or any(part in {"", ".", ".."} for part in self.root.split("/")[1:]):
            raise RemoteStorageError("invalid_root")
        if type(self.port) is not int or not 1 <= self.port <= 65535 or not 0 < self.timeout <= 300:
            raise RemoteStorageError("invalid_binding")
        if self.password_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.password_env):
            raise RemoteStorageError("invalid_secret_reference")
        if self.max_bytes_per_second is not None and (type(self.max_bytes_per_second) is not int or self.max_bytes_per_second <= 0):
            raise RemoteStorageError("invalid_rate")
        if self.python_executable != "python3" and (not self.python_executable.startswith("/") or any(char in self.python_executable for char in "\r\n\0")):
            raise RemoteStorageError("invalid_runtime_binding")


def read_ssh_binding(path: Path) -> SSHBinding:
    from bili_asr.artifact_inventory import require_regular_file
    from bili_asr.storage_targets import unique_json_object
    if require_regular_file(path).st_size > 16384:
        raise RemoteStorageError("binding_too_large")
    document = json.loads(path.read_bytes(), object_pairs_hook=unique_json_object)
    from bili_asr.contracts.json_schema import validate_json
    try:
        validate_json(BACKEND_CONTRACT, document)
    except ValueError:
        if isinstance(document, dict) and document.get("backend") != BACKEND_CONTRACT:
            raise RemoteStorageError("unsupported_backend") from None
        raise RemoteStorageError("invalid_binding") from None
    if not isinstance(document, dict) or document.pop("backend", None) != BACKEND_CONTRACT:
        raise RemoteStorageError("unsupported_backend")
    try:
        return SSHBinding(**document)
    except TypeError:
        raise RemoteStorageError("invalid_binding") from None


class SSHDirectoryBackend:
    capabilities = CAPABILITIES

    def __init__(self, binding: SSHBinding):
        self.binding = binding
        self.client = None

    def __enter__(self):
        try:
            import paramiko
        except ImportError:
            raise RemoteStorageError("install_remote_extra") from None
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.binding.known_hosts:
            client.load_host_keys(self.binding.known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        password = None
        if self.binding.password_env:
            password = os.environ.get(self.binding.password_env)
            if not password:
                raise RemoteStorageError("credential_missing")
        try:
            client.connect(self.binding.host, port=self.binding.port, username=self.binding.username,
                           password=password, key_filename=self.binding.key_filename,
                           timeout=self.binding.timeout, auth_timeout=self.binding.timeout,
                           banner_timeout=self.binding.timeout, allow_agent=True, look_for_keys=True)
        except paramiko.AuthenticationException:
            client.close()
            raise RemoteStorageError("authentication") from None
        except paramiko.BadHostKeyException:
            client.close()
            raise RemoteStorageError("host_identity") from None
        except (OSError, paramiko.SSHException):
            client.close()
            raise RemoteStorageError("connection_or_host_identity") from None
        self.client = client
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()
            self.client = None

    def _start(self, operation: str, **options):
        if self.client is None:
            raise RemoteStorageError("not_connected")
        if operation != "prepare" and self.binding.instance_id is None:
            raise RemoteStorageError("instance_binding_required")
        agent = files("bili_asr").joinpath("ssh_storage_agent.py").read_text("utf-8")
        try:
            incoming, outgoing, errors = self.client.exec_command(shlex.quote(self.binding.python_executable) + " -c " + shlex.quote(agent), timeout=self.binding.timeout)
            request = {"operation": operation, "root": self.binding.root, "target_id": self.binding.target_id,
                       "instance_id": self.binding.instance_id, **options}
            incoming.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            incoming.flush()
            return incoming, outgoing, errors
        except (OSError, EOFError):
            raise RemoteStorageError("connection_lost") from None

    @staticmethod
    def _response(outgoing):
        try:
            raw = outgoing.readline(16385)
            if not raw and outgoing.channel.recv_exit_status() == 127:
                raise RemoteStorageError("python_runtime_unavailable")
            if len(raw) > 16384:
                raise RemoteStorageError("invalid_response")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True:
                code = result.get("code", "invalid_response") if isinstance(result, dict) else "invalid_response"
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,40}", code):
                    code = "invalid_response"
                raise RemoteStorageError(code)
            return result
        except (ValueError, OSError) as error:
            if isinstance(error, RemoteStorageError):
                raise
            raise RemoteStorageError("invalid_response") from None

    @staticmethod
    def _finish(outgoing):
        if outgoing.channel.recv_exit_status() != 0:
            raise RemoteStorageError("remote_operation_failed")

    def _throttle(self, count, started):
        if self.binding.max_bytes_per_second:
            delay = count / self.binding.max_bytes_per_second - (time.monotonic() - started)
            while delay > 0:
                time.sleep(min(delay, 0.1))
                delay = count / self.binding.max_bytes_per_second - (time.monotonic() - started)

    def prepare(self) -> dict:
        incoming, outgoing, errors = self._start("prepare")
        try:
            incoming.channel.shutdown_write()
            result = self._response(outgoing)
            self._finish(outgoing)
            return {"backend": BACKEND_CONTRACT, "target_id": self.binding.target_id,
                    "instance_id": result["instance_id"], "capabilities": dict(self.capabilities)}
        finally:
            incoming.close()
            outgoing.close()
            errors.close()
    def probe(self) -> dict:
        incoming, outgoing, errors = self._start("probe")
        try:
            incoming.channel.shutdown_write()
            result = self._response(outgoing)
            self._finish(outgoing)
            return {"target_id": self.binding.target_id, "instance_id": result["instance_id"], "available": True}
        finally:
            incoming.close()
            outgoing.close()
            errors.close()

    def upload(self, path: Path, *, key: str, sha256: str, size: int) -> dict:
        from bili_asr.artifact_packages import _open_regular
        incoming, outgoing, errors = self._start("put", key=key, sha256=sha256, size=size)
        try:
            count, digest, started = 0, hashlib.sha256(), time.monotonic()
            with _open_regular(path) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    count += len(block)
                    if count > size:
                        raise RemoteStorageError("source_changed")
                    digest.update(block)
                    incoming.write(block)
                    self._throttle(count, started)
            if count != size or digest.hexdigest() != sha256:
                raise RemoteStorageError("source_changed")
            incoming.flush()
            incoming.channel.shutdown_write()
            self._response(outgoing)
            self._finish(outgoing)
        except (OSError, EOFError, socket.timeout):
            raise RemoteStorageError("upload_interrupted") from None
        finally:
            incoming.close()
            outgoing.close()
            errors.close()
        # Server acknowledgement is not byte evidence: read it back to this client.
        class Discard:
            def write(self, block):
                return len(block)
        self.download(key=key, sha256=sha256, size=size, destination=Discard())
        return {"target_id": self.binding.target_id, "instance_id": self.binding.instance_id,
                "key": key, "sha256": sha256, "size": size, "verified_at": int(time.time()),
                "verification_scope": "entire-package-client-readback", "source_released": False}

    def download(self, *, key: str, sha256: str, size: int, destination: BinaryIO,
                 offset: int = 0, count: int | None = None) -> dict:
        count = size if count is None else count
        incoming, outgoing, errors = self._start("get", key=key, sha256=sha256, size=size, offset=offset, count=count)
        try:
            incoming.channel.shutdown_write()
            response = self._response(outgoing)
            if response.get("size") != count or response.get("offset") != offset:
                raise RemoteStorageError("invalid_response")
            remaining, digest, started = count, hashlib.sha256(), time.monotonic()
            while remaining:
                block = outgoing.read(min(1024 * 1024, remaining))
                if not block:
                    raise RemoteStorageError("download_interrupted")
                destination.write(block)
                digest.update(block)
                remaining -= len(block)
                self._throttle(count - remaining, started)
            self._finish(outgoing)
            if offset == 0 and count == size and digest.hexdigest() != sha256:
                raise RemoteStorageError("checksum_mismatch")
            return {"size": count, "sha256": digest.hexdigest(), "whole_object_verified": offset == 0 and count == size}
        except (OSError, EOFError, socket.timeout):
            raise RemoteStorageError("download_interrupted") from None
        finally:
            incoming.close()
            outgoing.close()
            errors.close()


class UnavailableBackend:
    """Keep a classified connection failure in dependency reports without secrets."""
    def __init__(self, binding: SSHBinding, error: RemoteStorageError):
        self.binding, self.error = binding, error

    def download(self, **_options):
        raise self.error
