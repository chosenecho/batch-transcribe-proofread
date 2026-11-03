"""Verified remote package copying and explicit restoration; no source deletion."""
from __future__ import annotations

import os
import shutil
import tempfile
import hashlib
from contextlib import closing
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession, open_archive_connection
from bili_asr.archive_maintenance import archive_access
from bili_asr.artifact_packages import check_artifact_package
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.remote_storage import RemoteStorageError, SSHDirectoryBackend
from bili_asr.services.artifact_restore import restore_artifact
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage_targets import bind_directory_target


def push_remote_package(roots: ArtifactRoots, package: Path, backend: SSHDirectoryBackend) -> dict:
    """Register remote replicas only after durable publication and client readback."""
    with archive_access(roots.archive_root, exclusive=True, create_root=False), closing(
        open_archive_connection(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots)
    ) as connection:
        catalog = ArtifactCatalog(connection)
        checked = check_artifact_package(package)
        for entry in checked["objects"]:
            if catalog.object(entry["object_id"])["byte_size"] != entry["size_bytes"]:
                raise RemoteStorageError("catalog_object_mismatch")
        key = f"packages/artifact-{checked['package_id']}.zip"
        s3 = getattr(backend, "target_kind", None) == "s3"
        if s3:
            from bili_asr.storage.extension_contracts import artifact_catalog_version
            from bili_asr.storage.s3_upload_state import S3UploadState
            if artifact_catalog_version(connection) != 2:
                raise RemoteStorageError("explicit_catalog_v2_upgrade_required")
            key = f"packages/artifact-{checked['package_sha256']}.zip"
            transfer_id = "s3-" + hashlib.sha256((backend.binding.target_id + ":" + checked["package_sha256"]).encode()).hexdigest()
            with connection:
                catalog.register_target(backend.binding.target_id, kind="s3", instance_id=backend.binding.instance_id)
                catalog.begin_transfer(transfer_id, kind="copy", plan_sha256=checked["package_sha256"], target_id=backend.binding.target_id)
                for entry in checked["objects"]:
                    catalog.record_transfer_item(transfer_id, entry["object_id"], state="planned")
                catalog.set_transfer_state(transfer_id, "copying")
            try:
                receipt = backend.upload(package, key=key, sha256=checked["package_sha256"],
                                         size=checked["package_size_bytes"], checkpoint_store=S3UploadState(connection, transfer_id))
            except (ValueError, OSError) as error:
                with connection:
                    code = error.code if isinstance(error, RemoteStorageError) else "upload_interrupted"
                    catalog.set_transfer_state(transfer_id, "failed", error_code=code)
                    for entry in checked["objects"]:
                        catalog.record_transfer_item(transfer_id, entry["object_id"], state="failed", error_code=code)
                raise
        else:
            receipt = backend.upload(package, key=key, sha256=checked["package_sha256"], size=checked["package_size_bytes"])
        with connection:
            if not s3:
                catalog.register_target(backend.binding.target_id)
            catalog.record_package(checked["package_id"], backend.binding.target_id, key,
                                   sha256=checked["package_sha256"], byte_size=checked["package_size_bytes"],
                                   manifest_sha256=checked["manifest_sha256"])
            for entry in checked["objects"]:
                replica_id = catalog.record_verified_replica(entry["object_id"], backend.binding.target_id, key,
                                                sha256=entry["object_id"], byte_size=entry["size_bytes"],
                                                package_id=checked["package_id"], member_key=entry["member"])
                if s3:
                    catalog.record_transfer_item(transfer_id, entry["object_id"], state="verified", target_replica_id=replica_id)
            if s3:
                catalog.set_transfer_state(transfer_id, "complete")
        return {**receipt, "package_id": checked["package_id"], "object_count": len(checked["objects"])}


def fetch_remote_package(backend: SSHDirectoryBackend, replica: dict, destination: Path) -> dict:
    """Caller owns private staging; failure never exposes an installed package."""
    if shutil.disk_usage(destination.parent).free < replica["package_byte_size"] + 16 * 1024**2:
        raise RemoteStorageError("insufficient_space")
    with destination.open("xb") as output:
        backend.download(key=replica["relative_key"], sha256=replica["package_sha256"],
                         size=replica["package_byte_size"], destination=output)
        output.flush()
        os.fsync(output.fileno())
    checked = check_artifact_package(destination, expected_sha256=replica["package_sha256"])
    if checked["package_id"] != replica["package_id"] or checked["manifest_sha256"] != replica["manifest_sha256"]:
        raise RemoteStorageError("package_identity_mismatch")
    return checked


def restore_remote_artifact(roots: ArtifactRoots, object_id: str, storage_key: str,
                            backend: SSHDirectoryBackend, *, scratch_root: Path | None = None,
                            _online: bool = False) -> dict:
    """Download one bounded package to scratch, then use the exact-byte restorer."""
    mode = ArchiveAccessMode.WRITE if _online else ArchiveAccessMode.MAINTENANCE
    with ArchiveSession(roots.archive_root, mode=mode, artifact_roots=roots) as session:
        if _online:
            from bili_asr.storage.artifact_online import require_artifact_online
            require_artifact_online(session.connection, required=True)
        catalog = ArtifactCatalog(session.connection)
        candidates = [item for item in catalog.replicas_for_object(object_id)
                      if item["target_id"] == backend.binding.target_id and item["package_id"] and item["presence"] != "released"
                      and item["target_kind"] == getattr(backend, "target_kind", "directory")]
        if getattr(backend, "target_kind", None) == "s3":
            instance = session.connection.execute("SELECT instance_id FROM artifact_remote_instances WHERE target_id=?",
                                                   (backend.binding.target_id,)).fetchone()
            if instance is None or instance[0] != backend.binding.instance_id:
                raise RemoteStorageError("target_instance_mismatch")
        if not candidates:
            raise RemoteStorageError("no_retained_remote_replica")
        failures = []
        for replica in candidates:
            try:
                with tempfile.TemporaryDirectory(prefix="bili-remote-restore-", dir=scratch_root) as temporary:
                    cache = Path(temporary)
                    bind_directory_target(cache, backend.binding.target_id, roots=roots)
                    (cache / "packages").mkdir()
                    destination = cache / replica["relative_key"]
                    fetch_remote_package(backend, replica, destination)
                    backend.probe()
                    report = restore_artifact(roots, object_id, target_id=backend.binding.target_id,
                                              target_root=cache, storage_key=storage_key,
                                              _online=_online,
                                              _remote_instance=backend.binding.instance_id if getattr(backend, "target_kind", None) == "s3" else None)
                    return {**report, "remote_package_verified": True, "automatic_retry": False}
            except (ValueError, OSError) as error:
                failures.append(error.code if isinstance(error, RemoteStorageError) else "restore_unavailable")
        raise RemoteStorageError(failures[-1])


def abort_remote_upload(roots, backend, transfer_id):
    from bili_asr.storage.s3_upload_state import S3UploadState
    if getattr(backend, "target_kind", None) != "s3":
        raise RemoteStorageError("s3_backend_required")
    with archive_access(roots.archive_root, exclusive=True, create_root=False), closing(
        open_archive_connection(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots)
    ) as connection:
        catalog = ArtifactCatalog(connection)
        row = connection.execute(
            "SELECT plan_sha256,target_id,state FROM artifact_transfers WHERE transfer_id=? AND kind='copy'", (transfer_id,)).fetchone()
        if row is None or row[1] != backend.binding.target_id or row[2] == "complete":
            raise RemoteStorageError("resumable_transfer_required")
        instance = connection.execute("SELECT instance_id FROM artifact_remote_instances WHERE target_id=?", (row[1],)).fetchone()
        if instance is None or instance[0] != backend.binding.instance_id:
            raise RemoteStorageError("target_instance_mismatch")
        report = backend.abort(key=f"packages/artifact-{row[0]}.zip",
                               checkpoint_store=S3UploadState(connection, transfer_id))
        with connection:
            catalog.set_transfer_state(transfer_id, "failed", error_code="upload_aborted")
        return {**report, "transfer_id": transfer_id}
