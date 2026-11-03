"""Private S3-compatible package transport with client SHA-256 verification.

Multipart ETags are opaque part receipts. Only a complete client readback may
authorize a catalog replica. Upload never deletes source or remote content.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from bili_asr.artifact_inventory import require_regular_file, stream_hash
from bili_asr.artifact_packages import _open_regular
from bili_asr.canonical_json import canonical
from bili_asr.remote_storage import RemoteStorageError
from bili_asr.storage.artifact_catalog import storage_key_parts
from bili_asr.storage_targets import unique_json_object

BACKEND_CONTRACT = "s3-compatible/v1"
CAPABILITIES = {"upload": True, "immutable_publish": False, "range_read": True,
                "verification": "entire-package-client-sha256-readback", "resume_upload": True,
                "resume_policy": "durable-part-receipts-and-final-readback",
                "automatic_source_release": False, "anonymous_access": False}


@dataclass(frozen=True)
class S3Binding:
    target_id: str
    endpoint: str = field(repr=False)
    bucket: str = field(repr=False)
    access_key_env: str = field(repr=False)
    secret_key_env: str = field(repr=False)
    prefix: str = field(default="bili-asr", repr=False)
    region: str = "auto"
    session_token_env: str | None = field(default=None, repr=False)
    addressing_style: str = "path"
    part_size: int = 8 * 1024**2
    timeout: float = 30
    download_range_bytes: int = 0
    private_bucket: bool = True
    allow_local_http: bool = False

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        local_http = (self.allow_local_http is True and parsed.scheme == "http"
                      and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise RemoteStorageError("invalid_endpoint")
        if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.target_id) or self.target_id in {"local", "local-artifacts"}:
            raise RemoteStorageError("invalid_target")
        if not re.fullmatch("[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket) or ".." in self.bucket:
            raise RemoteStorageError("invalid_bucket")
        for value in (self.access_key_env, self.secret_key_env, self.session_token_env):
            if value is not None and not re.fullmatch("[A-Za-z_][A-Za-z0-9_]{0,127}", value):
                raise RemoteStorageError("invalid_secret_reference")
        if self.prefix:
            storage_key_parts(self.prefix)
        if (self.private_bucket is not True or self.addressing_style not in {"path", "virtual"}
                or not re.fullmatch("[A-Za-z0-9-]{1,63}", self.region)
                or type(self.part_size) is not int or not 5 * 1024**2 <= self.part_size <= 64 * 1024**2
                or type(self.timeout) not in {int, float} or not 0 < self.timeout <= 300):
            raise RemoteStorageError("invalid_binding")
        if (type(self.download_range_bytes) is not int or self.download_range_bytes != 0
                and not 1024**2 <= self.download_range_bytes <= 64 * 1024**2):
            raise RemoteStorageError("invalid_binding")

    @property
    def instance_id(self):
        # Machine-independent endpoint identity, never credentials or signed URLs.
        return hashlib.sha256(canonical([BACKEND_CONTRACT, self.target_id, self.endpoint.rstrip("/").lower(),
                                         self.bucket, self.prefix]).encode()).hexdigest()[:32]


def read_s3_binding(path: Path) -> S3Binding:
    if require_regular_file(path).st_size > 16384:
        raise RemoteStorageError("binding_too_large")
    document = json.loads(path.read_bytes(), object_pairs_hook=unique_json_object)
    from bili_asr.contracts.json_schema import validate_json
    try:
        validate_json(BACKEND_CONTRACT, document)
        return S3Binding(**{key: value for key, value in document.items() if key != "backend"})
    except (TypeError, ValueError):
        raise RemoteStorageError("invalid_binding") from None


class _Discard:
    def write(self, block):
        return len(block)


class _HashingWriter:
    def __init__(self, destination):
        self.destination, self.digest, self.size = destination, hashlib.sha256(), 0

    def write(self, block):
        written = self.destination.write(block)
        if type(written) is not int or not 0 < written <= len(block):
            raise RemoteStorageError("destination_write_failed")
        self.digest.update(block[:written])
        self.size += written
        return written


class S3Backend:
    capabilities = CAPABILITIES
    target_kind = "s3"

    def __init__(self, binding: S3Binding):
        self.binding = binding
        self.client = None

    def __enter__(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise RemoteStorageError("install_remote_extra") from None
        credentials = [os.environ.get(name) if name else None for name in
                       (self.binding.access_key_env, self.binding.secret_key_env, self.binding.session_token_env)]
        if not credentials[0] or not credentials[1] or (self.binding.session_token_env and not credentials[2]):
            raise RemoteStorageError("credential_missing")
        self.client = boto3.client(
            "s3", endpoint_url=self.binding.endpoint, region_name=self.binding.region,
            aws_access_key_id=credentials[0], aws_secret_access_key=credentials[1], aws_session_token=credentials[2],
            config=Config(signature_version="s3v4", connect_timeout=self.binding.timeout, read_timeout=self.binding.timeout,
                          retries={"mode": "standard", "total_max_attempts": 3},
                          request_checksum_calculation="when_required", response_checksum_validation="when_required",
                          s3={"addressing_style": self.binding.addressing_style}))
        return self

    def __exit__(self, *_):
        if self.client is not None:
            self.client.close()
            self.client = None

    def _call(self, operation, **arguments):
        if self.client is None:
            raise RemoteStorageError("not_connected")
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return getattr(self.client, operation)(Bucket=self.binding.bucket, **arguments)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            classification = {"AccessDenied": "permission_denied", "InvalidAccessKeyId": "authentication",
                              "SignatureDoesNotMatch": "authentication", "NoSuchKey": "object_missing",
                              "NoSuchBucket": "bucket_missing", "404": "object_missing", "403": "permission_denied",
                              "NoSuchUpload": "upload_missing", "RequestTimeout": "timeout"}.get(code, "s3_operation_failed")
            raise RemoteStorageError(classification) from None
        except BotoCoreError:
            raise RemoteStorageError("connection_or_timeout") from None

    def _key(self, key):
        try:
            storage_key_parts(key)
        except ValueError:
            raise RemoteStorageError("invalid_key") from None
        return self.binding.prefix + "/" + key if self.binding.prefix else key

    def probe(self):
        self._call("head_bucket")
        return {"target_id": self.binding.target_id, "instance_id": self.binding.instance_id,
                "available": True, "private_bucket_attested": True,
                "capabilities": dict(self.capabilities)}

    def prepare(self):
        # Existing private buckets only. Never create a bucket or change policy.
        return self.probe()

    def download(self, *, key, sha256, size, destination, offset=0, count=None):
        from botocore.exceptions import BotoCoreError
        count = size - offset if count is None else count
        if (not re.fullmatch("[0-9a-f]{64}", sha256) or any(type(value) is not int for value in (size, offset, count))
                or size < 0 or offset < 0 or count < 0 or offset + count > size):
            raise RemoteStorageError("invalid_download")
        whole = offset == 0 and count == size
        window = self.binding.download_range_bytes
        if whole and window and size > window:
            checked = _HashingWriter(destination)
            for start in range(0, size, window):
                self.download(key=key, sha256=sha256, size=size, destination=checked,
                              offset=start, count=min(window, size-start))
            if checked.size != size or checked.digest.hexdigest() != sha256:
                raise RemoteStorageError("checksum_mismatch")
            return {"size": size, "sha256": sha256, "whole_object_verified": True}
        arguments = {"Key": self._key(key)}
        if not whole and count:
            arguments["Range"] = f"bytes={offset}-{offset + count - 1}"
        if not count and not whole:
            return {"size": 0, "sha256": hashlib.sha256(b"").hexdigest(), "whole_object_verified": False}
        response = self._call("get_object", **arguments)
        body = response["Body"]
        digest, read = hashlib.sha256(), 0
        try:
            if response.get("ContentLength") != count:
                raise RemoteStorageError("size_mismatch")
            if not whole and response.get("ContentRange") != f"bytes {offset}-{offset + count - 1}/{size}":
                raise RemoteStorageError("invalid_range_response")
            while block := body.read(min(1024**2, count - read + 1)):
                read += len(block)
                if read > count:
                    raise RemoteStorageError("size_mismatch")
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = destination.write(view)
                    if type(written) is not int or written <= 0 or written > len(view):
                        raise RemoteStorageError("destination_write_failed")
                    view = view[written:]
            if read != count:
                raise RemoteStorageError("download_interrupted")
            if whole and digest.hexdigest() != sha256:
                raise RemoteStorageError("checksum_mismatch")
        except (OSError, ValueError, BotoCoreError) as error:
            if isinstance(error, RemoteStorageError):
                raise
            raise RemoteStorageError("download_interrupted") from None
        finally:
            body.close()
        return {"size": read, "sha256": digest.hexdigest(), "whole_object_verified": whole}

    def upload(self, path, *, key, sha256, size, checkpoint_store=None):
        # The digest is part of the key, making concurrent equal-byte uploads
        # converge. No conditional-publication support is assumed for R2.
        if (not re.fullmatch("[0-9a-f]{64}", sha256) or type(size) is not int or size < 0
                or not key.endswith("-" + sha256 + ".zip")):
            raise RemoteStorageError("content_addressed_key_required")
        remote_key = self._key(key)
        with _open_regular(path) as source:
            if stream_hash(source) != (size, sha256):
                raise RemoteStorageError("source_changed")
            source.seek(0)
            try:
                self.download(key=key, sha256=sha256, size=size, destination=_Discard())
                if checkpoint_store:
                    checkpoint_store.clear()
                return self._receipt(key, sha256, size, reused=True)
            except RemoteStorageError as error:
                if error.code != "object_missing":
                    raise
            if size < self.binding.part_size:
                self._call("put_object", Key=remote_key, Body=source, ContentLength=size,
                           ContentType="application/zip", Metadata={"sha256": sha256})
            else:
                if (size + self.binding.part_size - 1) // self.binding.part_size > 10000:
                    raise RemoteStorageError("too_many_parts")
                state = checkpoint_store.load() if checkpoint_store else None
                if state is not None and state["part_size"] != self.binding.part_size:
                    raise RemoteStorageError("resume_part_size_mismatch")
                if state is None:
                    response = self._call("create_multipart_upload", Key=remote_key,
                                          ContentType="application/zip", Metadata={"sha256": sha256})
                    state = {"upload_id": response["UploadId"], "part_size": self.binding.part_size, "parts": []}
                    if checkpoint_store:
                        checkpoint_store.save(state)
                remote_parts, marker = {}, 0
                while True:
                    try:
                        response = self._call("list_parts", Key=remote_key, UploadId=state["upload_id"], PartNumberMarker=marker)
                    except RemoteStorageError as error:
                        if error.code == "upload_missing" and checkpoint_store:
                            checkpoint_store.clear()
                        raise
                    remote_parts.update({part["PartNumber"]: part for part in response.get("Parts", [])})
                    if not response.get("IsTruncated"):
                        break
                    next_marker = response.get("NextPartNumberMarker", 0)
                    if next_marker <= marker or len(remote_parts) > 10000:
                        raise RemoteStorageError("invalid_parts_response")
                    marker = next_marker
                previous = {part["PartNumber"]: part for part in state["parts"]}
                completed, digest, length = [], hashlib.sha256(), 0
                for number in range(1, (size + self.binding.part_size - 1) // self.binding.part_size + 1):
                    block = source.read(min(self.binding.part_size, size - length))
                    expected_size = min(self.binding.part_size, size - length)
                    if len(block) != expected_size:
                        raise RemoteStorageError("source_changed")
                    length += len(block)
                    digest.update(block)
                    part_sha = hashlib.sha256(block).hexdigest()
                    prior, remote = previous.get(number), remote_parts.get(number)
                    if (prior is None or remote is None or prior.get("sha256") != part_sha
                            or prior.get("ETag") != remote.get("ETag") or remote.get("Size") != len(block)):
                        response = self._call("upload_part", Key=remote_key, UploadId=state["upload_id"],
                                              PartNumber=number, Body=block, ContentLength=len(block))
                        prior = {"PartNumber": number, "ETag": response["ETag"], "sha256": part_sha}
                    completed.append(prior)
                    state["parts"] = completed.copy()
                    if checkpoint_store:
                        checkpoint_store.save(state)
                if length != size or source.read(1) or digest.hexdigest() != sha256:
                    raise RemoteStorageError("source_changed")
                self._call("complete_multipart_upload", Key=remote_key, UploadId=state["upload_id"],
                           MultipartUpload={"Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in completed]})
        self.download(key=key, sha256=sha256, size=size, destination=_Discard())
        if checkpoint_store:
            checkpoint_store.clear()
        return self._receipt(key, sha256, size, reused=False)

    def _receipt(self, key, sha256, size, *, reused):
        return {"target_id": self.binding.target_id, "instance_id": self.binding.instance_id,
                "key": key, "sha256": sha256, "size": size, "verified_at": int(time.time()),
                "verification_scope": "entire-package-client-readback", "source_released": False, "reused": reused}

    def abort(self, *, key, checkpoint_store):
        state = checkpoint_store.load()
        if state is not None:
            try:
                self._call("abort_multipart_upload", Key=self._key(key), UploadId=state["upload_id"])
            except RemoteStorageError as error:
                if error.code != "upload_missing":
                    raise
            checkpoint_store.clear()
        return {"target_id": self.binding.target_id, "aborted": state is not None, "source_released": False}
