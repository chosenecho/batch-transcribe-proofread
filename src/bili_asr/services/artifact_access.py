"""Read exact retained bytes from explicitly bound storage targets.

Access never changes production or catalog state, creates a mount, downloads
media, or invokes inference. Callers hold archive maintenance while collecting
snapshot bytes. Machine paths exist only in the invocation's target bindings.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from bili_asr.artifact_packages import copy_package_object, read_package_manifest
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage.artifact_catalog import ArtifactCatalog, storage_key_parts
from bili_asr.storage_targets import DirectoryTarget, open_directory_target


class ArtifactAccessError(ValueError):
    """The exact retained object cannot be read from any supplied target."""


def parse_target_bindings(values: list[str] | None) -> dict[str, Path]:
    bindings = {}
    for value in values or ():
        identity, separator, root = value.partition("=")
        if not separator or not identity or not root or identity in bindings:
            raise ArtifactAccessError("storage target requires one unique TARGET_ID=DIRECTORY binding")
        bindings[identity] = Path(root)
    return bindings


@dataclass(frozen=True)
class PackagedArtifact:
    target: DirectoryTarget
    replica: dict
    size: int
    object_id: str

    def copy_to(self, destination: BinaryIO) -> dict:
        self.target.check()
        path = self.target.root.joinpath(*storage_key_parts(self.replica["relative_key"]))
        if path.stat().st_size != self.replica["package_byte_size"]:
            raise ArtifactAccessError("package size differs from frozen catalog identity")
        result = copy_package_object(path, self.object_id, destination, expected_size=self.size,
                                     package_id=self.replica["package_id"],
                                     manifest_sha256=self.replica["manifest_sha256"])
        self.target.check()
        return result


class ArtifactAccess:
    def __init__(self, connection: sqlite3.Connection, roots: ArtifactRoots,
                 target_bindings: dict[str, Path]):
        self.catalog = ArtifactCatalog(connection)
        self.roots = roots
        self.target_bindings = target_bindings

    def locate_external(self, object_id: str) -> PackagedArtifact:
        obj = self.catalog.object(object_id)
        failures = []
        for replica in self.catalog.replicas_for_object(object_id):
            if replica["package_id"] is None or replica["presence"] == "released":
                continue
            identity = replica["target_id"]
            root = self.target_bindings.get(identity)
            if root is None:
                failures.append(f"{identity}: target_unbound")
                continue
            try:
                target = open_directory_target(root, identity, roots=self.roots)
                path = target.root.joinpath(*storage_key_parts(replica["relative_key"]))
                manifest = read_package_manifest(path)
                if (manifest["package_id"] != replica["package_id"]
                        or manifest["manifest_sha256"] != replica["manifest_sha256"]
                        or path.stat().st_size != replica["package_byte_size"]):
                    raise ArtifactAccessError("package_identity_mismatch")
                entry = next((entry for entry in manifest["objects"] if entry["object_id"] == object_id), None)
                if entry is None or entry["member"] != replica["member_key"] or entry["size_bytes"] != obj["byte_size"]:
                    raise ArtifactAccessError("package_member_mismatch")
                return PackagedArtifact(target, replica, obj["byte_size"], object_id)
            except (ValueError, OSError) as error:
                failures.append(f"{identity}: {type(error).__name__}")
        raise ArtifactAccessError(f"artifact_input_unavailable: {object_id}; " + "; ".join(failures or ["no_external_replica"]))
