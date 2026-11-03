"""Current, independent directory copies required by explicit audio retention."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bili_asr.artifact_packages import (
    capture_verified_file_generation,
    check_artifact_package,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage_targets import open_directory_target


@dataclass
class AudioReplicaRequirement:
    roots: ArtifactRoots
    bindings: dict[str, Path]
    target_ids: tuple[str, ...]
    minimum_copies: int
    packages: dict = field(default_factory=dict)

    def verify(self, connection, object_id):
        """Hash containers and original members outside the final DB write lock."""
        catalog = ArtifactCatalog(connection)
        replicas = catalog.replicas_for_object(object_id)
        evidence, directories = [], set()
        for target_id in self.target_ids:
            root = self.bindings.get(target_id)
            if root is None:
                raise ValueError("audio_replica_target_unbound: " + target_id)
            target = open_directory_target(root, target_id, roots=self.roots)
            directory = (target.device, target.inode)
            if directory in directories:
                raise ValueError("audio_replica_targets_are_not_distinct")
            for replica in replicas:
                if replica["target_id"] != target_id or replica["presence"] != "present" or replica["package_id"] is None:
                    continue
                path = target.root / replica["relative_key"]
                generation = capture_verified_file_generation(path)
                key = (target_id, replica["package_id"])
                cached = self.packages.get(key)
                if cached is None or cached[0] != generation:
                    checked = check_artifact_package(path, expected_sha256=replica["package_sha256"])
                    if capture_verified_file_generation(path) != generation:
                        raise ValueError("audio_replica_changed_during_verification")
                    self.packages[key] = (generation, checked)
                else:
                    checked = cached[1]
                entry = next((item for item in checked["objects"] if item["object_id"] == object_id), None)
                if (checked["package_id"] != replica["package_id"]
                        or checked["manifest_sha256"] != replica["manifest_sha256"]
                        or entry is None or entry["sha256"] != object_id
                        or entry["size_bytes"] != replica["verified_byte_size"]
                        or entry["member"] != replica["member_key"]):
                    raise ValueError("audio_replica_catalog_identity_mismatch")
                directories.add(directory)
                evidence.append((target, path, generation, replica))
                break
        if len(evidence) < self.minimum_copies:
            raise ValueError("audio_replica_requirement_unsatisfied")

        def guard():
            # Queue/pin protection and this check share the caller's short write
            # transaction. Previously observed catalog verification is not enough.
            for target, path, generation, replica in evidence:
                target.check()
                row = connection.execute("SELECT presence,generation,verified_sha256,verified_byte_size "
                    "FROM artifact_replicas WHERE replica_id=?", (replica["replica_id"],)).fetchone()
                if (row is None or tuple(row) != ("present", replica["generation"], object_id, replica["verified_byte_size"])
                        or capture_verified_file_generation(path) != generation):
                    raise ValueError("audio_replica_changed_before_deletion")
        return guard

    def statistics(self):
        return [{"target_id": key[0], "package_id": key[1], "package_sha256": value[1]["package_sha256"],
                 **value[1].get("compression_statistics", {})} for key, value in self.packages.items()]
