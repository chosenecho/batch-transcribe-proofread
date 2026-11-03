"""Frozen cold-audio identities for inactive task evidence.

Capture explicitly reads bound packages and hashes their original members without
extracting media. Offline validation uses only the embedded SQLite snapshot.
Neither operation restores bytes or changes replica observations.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bili_asr.artifact_packages import (
    capture_verified_file_generation,
    check_artifact_package,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage.artifact_catalog import ArtifactCatalog, storage_key_parts
from bili_asr.storage_targets import open_directory_target

POLICY = "verified-references/v1"
VERIFICATION = "sha256-container-and-original-member/v1"
_MAX_REPLICAS = 4096
_FIELDS = ("replica_id", "target_id", "relative_key", "package_id", "member_key", "generation",
           "verified_sha256", "verified_byte_size", "verified_at", "observed_at", "presence",
           "target_kind", "package_sha256", "package_byte_size", "manifest_sha256")


def _identity(catalog, path, digest):
    connection = catalog.connection
    row = connection.execute("SELECT sha256,byte_size FROM audio_objects WHERE storage_key=?", (path,)).fetchone()
    if row is None or digest is None or row[0] != digest:
        raise ValueError("cold task-state audio has no original frozen identity: " + path)
    obj = catalog.object(digest)
    if obj["byte_size"] != row[1]:
        raise ValueError("cold task-state catalog size differs from original audio: " + path)
    return row[1]


def _replicas(connection, digest):
    # A corrupt or accumulated catalog must not turn validation into an
    # unbounded in-memory scan, nor silently hide alternatives by truncation.
    cursor = connection.execute(
        "SELECT r.*,t.kind AS target_kind,p.sha256 AS package_sha256,p.byte_size AS package_byte_size,p.manifest_sha256 "
        "FROM artifact_replicas r JOIN artifact_targets t USING(target_id) "
        "JOIN artifact_packages p ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
        "WHERE r.object_id=? AND r.presence='present' ORDER BY r.verified_at DESC,r.replica_id LIMIT ?",
        (digest, _MAX_REPLICAS + 1))
    rows = cursor.fetchmany(_MAX_REPLICAS + 1)
    if len(rows) > _MAX_REPLICAS:
        raise ValueError("cold task-state replica scan exceeds its bound")
    return [{key: row[key] for key in _FIELDS} for row in rows]


def validate_reference(connection, entry, *, catalog=None):
    """Compare every portable replica fact with the frozen, validated catalog."""
    catalog = ArtifactCatalog(connection) if catalog is None else catalog
    if entry["size"] != _identity(catalog, entry["path"], entry["sha256"]):
        raise ValueError("cold task-state reference size differs from original audio")
    if (entry["available"] is not False or entry["verification"] != VERIFICATION
            or len(entry["replicas"]) != 1):
        raise ValueError("unsupported cold task-state reference verification")
    candidates = _replicas(connection, entry["sha256"])
    for replica in entry["replicas"]:
        storage_key_parts(replica["relative_key"])
        storage_key_parts(replica["member_key"])
        expected = {key: replica[key] for key in _FIELDS}
        if (expected not in candidates or replica["verified_sha256"] != entry["sha256"]
                or replica["verified_byte_size"] != entry["size"]):
            raise ValueError("cold task-state replica identity differs from frozen catalog")
        if replica["backend"] == "directory/v1":
            if replica["instance_id"] is not None:
                raise ValueError("directory task-state reference cannot claim a remote instance")
        elif (replica["backend"] != "ssh-directory/v1" or not isinstance(replica["instance_id"], str)
                or re.fullmatch("[0-9a-f]{32}", replica["instance_id"]) is None):
            raise ValueError("cold task-state remote instance is missing or unsupported")


def validate_references(connection, entries):
    """Validate the optional catalog once per frozen database, not per audio."""
    catalog = ArtifactCatalog(connection)
    for entry in entries:
        validate_reference(connection, entry, catalog=catalog)


@dataclass
class ReferenceCapture:
    roots: ArtifactRoots
    local_targets: dict
    remote_backends: dict
    scratch: Path
    deadline: float
    packages: dict = field(default_factory=dict)
    guards: list = field(default_factory=list)
    catalog: ArtifactCatalog | None = field(default=None, init=False, repr=False)

    def _timeout(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("cold task-state reference capture exceeded its observation interval")

    def _package(self, replica):
        target_id = replica["target_id"]
        if target_id in self.local_targets and target_id in self.remote_backends:
            raise ValueError("cold task-state target has ambiguous backend bindings")
        cache_key = (target_id, replica["relative_key"], replica["package_sha256"])
        if cache_key in self.packages:
            return self.packages[cache_key]
        self._timeout()
        if target_id in self.remote_backends:
            from bili_asr.services.remote_artifacts import fetch_remote_package
            backend = self.remote_backends[target_id]
            binding = backend.binding
            if (binding.target_id != target_id or not isinstance(binding.instance_id, str)
                    or re.fullmatch("[0-9a-f]{32}", binding.instance_id) is None):
                raise ValueError("cold task-state needs an exact remote target instance")
            instance_id = binding.instance_id
            checked = fetch_remote_package(backend, replica, self.scratch / (uuid.uuid4().hex + ".zip"))
            def guard():
                if backend.binding.target_id != target_id or backend.binding.instance_id != instance_id:
                    raise ValueError("cold task-state remote binding changed across capture")
                if backend.probe().get("instance_id") != instance_id:
                    raise ValueError("cold task-state remote instance changed across capture")
            guard()
            identity = {"backend": "ssh-directory/v1", "instance_id": instance_id}
        elif target_id in self.local_targets:
            target = open_directory_target(self.local_targets[target_id], target_id, roots=self.roots)
            path = target.root.joinpath(*storage_key_parts(replica["relative_key"]))
            generation = capture_verified_file_generation(path)
            checked = check_artifact_package(path, expected_sha256=replica["package_sha256"])
            def guard():
                target.check()
                if capture_verified_file_generation(path) != generation:
                    raise ValueError("cold task-state package changed across capture")
            guard()
            identity = {"backend": "directory/v1", "instance_id": None}
        else:
            raise ValueError("cold task-state target is unbound")
        self._timeout()
        if (checked["package_id"] != replica["package_id"]
                or checked["manifest_sha256"] != replica["manifest_sha256"]
                or checked["package_size_bytes"] != replica["package_byte_size"]):
            raise ValueError("cold task-state package differs from frozen replica")
        self.guards.append(guard)
        self.packages[cache_key] = (checked, identity)
        return checked, identity

    def reference(self, connection, path, digest):
        if self.catalog is None:
            self.catalog = ArtifactCatalog(connection)
        size = _identity(self.catalog, path, digest)
        for replica in _replicas(connection, digest):
            try:
                if replica["verified_sha256"] != digest or replica["verified_byte_size"] != size:
                    raise ValueError("cold task-state verified replica has a different original identity")
                checked, identity = self._package(replica)
                item = next((item for item in checked["objects"] if item["object_id"] == digest), None)
                if item is None or (item["sha256"], item["size_bytes"], item["member"]) != (digest, size, replica["member_key"]):
                    raise ValueError("cold task-state package member differs from original audio")
                entry = {"path": path, "size": size, "sha256": digest, "available": False,
                         "kind": "audio-reference", "verification": VERIFICATION, "replicas": [{**replica, **identity}]}
                validate_reference(connection, entry, catalog=self.catalog)
                return entry
            except TimeoutError:
                raise
            except (ValueError, OSError):
                # One unavailable copy must not hide another valid alternative.
                # Reasons stay bounded and cannot echo remote connection secrets.
                continue
        raise ValueError("cold task-state audio has no bound, currently verified replica: " + path)

    def finish(self):
        self._timeout()
        for guard in self.guards:
            guard()
        self._timeout()


def media_availability(manifest):
    """Availability at capture is evidence, never a claim of restored media."""
    references = sum(entry["available"] is False for entry in manifest["media_dependencies"])
    return {"media_restored": False, "media_restore_required": bool(manifest["media_dependencies"]),
            "external_audio_references": references, "external_media_verified_now": False,
            "media_verification_scope": "frozen-catalog-and-capture-evidence-only"}
