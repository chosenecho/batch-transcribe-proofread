"""Wire agent for the explicitly supported Linux/OpenSSH/Python 3 backend.

Executed over an authenticated SSH channel, never installed on the server.
Only the prepared root and immutable package namespace are accessible.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid

MARKER = ".bili-asr-ssh-target.json"
CHUNK = 1024 * 1024


def _json(value):
    sys.stdout.buffer.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


def _open_root(path):
    if not isinstance(path, str) or not path.startswith("/") or path == "/" or any(p in {"", ".", ".."} for p in path.split("/")[1:]):
        raise ValueError("invalid_root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_marker(root):
    fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("identity_mismatch")
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("identity_mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"version", "target_id", "instance_id"} or type(value["version"]) is not int or value["version"] != 1 or not re.fullmatch("[0-9a-f]{32}", value["instance_id"]):
        raise ValueError("identity_mismatch")
    return value


def _identity(root, request, initial):
    with_fd = _open_root(request["root"])
    try:
        current = os.fstat(with_fd)
    finally:
        os.close(with_fd)
    if (current.st_dev, current.st_ino) != initial:
        raise ValueError("identity_mismatch")
    marker = _read_marker(root)
    if marker["target_id"] != request["target_id"] or marker["instance_id"] != request["instance_id"]:
        raise ValueError("identity_mismatch")


def _opened(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("unsafe_object")
    return os.fdopen(fd, "rb")


def _generation(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _verified(source, digest, size):
    before = os.fstat(source.fileno())
    actual = hashlib.sha256()
    count = 0
    for block in iter(lambda: source.read(CHUNK), b""):
        count += len(block)
        actual.update(block)
    if count != size or actual.hexdigest() != digest or _generation(before) != _generation(os.fstat(source.fileno())):
        raise ValueError("checksum_mismatch")
    return before


def _main(request):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request.get("target_id", "")):
        raise ValueError("invalid_request")
    operation = request.get("operation")
    root = _open_root(request["root"])
    directory = None
    try:
        info = os.fstat(root)
        initial = (info.st_dev, info.st_ino)
        if operation == "prepare":
            marker = {"version": 1, "target_id": request["target_id"], "instance_id": uuid.uuid4().hex}
            try:
                fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
            except FileExistsError:
                marker = _read_marker(root)
            else:
                with os.fdopen(fd, "wb") as output:
                    output.write(json.dumps(marker, sort_keys=True).encode())
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(root)
            if marker["target_id"] != request["target_id"]:
                raise ValueError("identity_mismatch")
            request["instance_id"] = marker["instance_id"]
            _identity(root, request, initial)
            _json({"ok": True, **marker})
            return
        _identity(root, request, initial)
        if operation == "probe":
            _json({"ok": True, "instance_id": request["instance_id"]})
            return
        key = request.get("key", "")
        if not re.fullmatch(r"packages/artifact-[0-9a-f]{64}\.zip", key):
            raise ValueError("invalid_key")
        digest, size = request.get("sha256"), request.get("size")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or type(size) is not int or size < 0:
            raise ValueError("invalid_request")
        if operation == "put":
            try:
                os.mkdir("packages", 0o700, dir_fd=root)
                os.fsync(root)
            except FileExistsError:
                pass
        directory = os.open("packages", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        name = key.split("/")[1]
        if operation == "put":
            staging = ".upload-" + uuid.uuid4().hex
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                actual, remaining = hashlib.sha256(), size
                with os.fdopen(fd, "wb") as output:
                    while remaining:
                        block = sys.stdin.buffer.read(min(CHUNK, remaining))
                        if not block:
                            raise ValueError("upload_interrupted")
                        output.write(block)
                        actual.update(block)
                        remaining -= len(block)
                    output.flush()
                    os.fsync(output.fileno())
                if actual.hexdigest() != digest:
                    raise ValueError("checksum_mismatch")
                with _opened(directory, staging) as source:
                    _verified(source, digest, size)
                _identity(root, request, initial)
                try:
                    os.link(staging, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    with _opened(directory, name) as source:
                        _verified(source, digest, size)
                os.fsync(directory)
            finally:
                os.unlink(staging, dir_fd=directory)
            _identity(root, request, initial)
            _json({"ok": True, "size": size, "sha256": digest, "durability": "file-and-directory-fsync"})
        elif operation == "get":
            offset, count = request.get("offset", 0), request.get("count", size)
            if type(offset) is not int or type(count) is not int or offset < 0 or count < 0 or offset + count > size:
                raise ValueError("invalid_range")
            with _opened(directory, name) as source:
                before = _verified(source, digest, size)
                source.seek(offset)
                _identity(root, request, initial)
                _json({"ok": True, "size": count, "offset": offset})
                remaining = count
                while remaining:
                    block = source.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError("download_interrupted")
                    sys.stdout.buffer.write(block)
                    remaining -= len(block)
                sys.stdout.buffer.flush()
                if _generation(before) != _generation(os.fstat(source.fileno())) or _generation(before) != _generation(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                    raise ValueError("object_changed")
            _identity(root, request, initial)
        else:
            raise ValueError("unsupported")
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.readline(16385)
        if len(raw) > 16384:
            raise ValueError("invalid_request")
        _main(json.loads(raw))
    except BaseException as error:
        code = (str(error) if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]{1,40}", str(error))
                else "missing" if isinstance(error, FileNotFoundError)
                else "permission_denied" if isinstance(error, PermissionError) else "storage_error")
        _json({"ok": False, "code": code})
        raise SystemExit(1)
