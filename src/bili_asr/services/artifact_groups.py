"""Freeze complete text versions before their mutable publication slots change."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from bili_asr.archive import archive_bundle_complete
from bili_asr.archive_maintenance import archive_access
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    portable_artifact_parts,
    require_regular_file,
    stream_hash,
)
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.artifacts import BUNDLE_MARKER_NAME, REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY, BUNDLE_SCHEMA_V3
from bili_asr.canonical_json import digest
from bili_asr.services.artifact_access import ArtifactAccess
from bili_asr.services.artifact_inventory_service import inventory_artifacts
from bili_asr.storage.artifact_catalog import ArtifactCatalog, require_artifact_catalog
from bili_asr.storage_targets import require_safe_path, sync_directory


def object_storage_key(object_id: str) -> str:
    return f"documents/artifact-objects/{object_id}"


@contextmanager
def _group_session(roots):
    with (archive_access(roots.archive_root, exclusive=True, create_root=False),
          ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE, artifact_roots=roots) as session):
        yield session


def _preserve_object(roots: ArtifactRoots, path: Path, expected: str) -> tuple[str, int]:
    require_regular_file(path)
    key = object_storage_key(expected)
    target = roots.write_base / key
    require_safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        require_regular_file(target)
        with target.open("rb") as source:
            size, actual = stream_hash(source)
        if actual != expected:
            raise ValueError("immutable artifact object contains different bytes")
        return key, size
    with tempfile.TemporaryDirectory(prefix=".artifact-freeze-", dir=target.parent) as temporary:
        staged = Path(temporary) / "object"
        with path.open("rb") as source, staged.open("xb") as destination:
            size, actual = stream_hash(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        if actual != expected:
            raise ValueError("artifact changed during version preservation")
        if os.name == "nt":
            os.rename(staged, target)
        else:
            os.link(staged, target)
        sync_directory(target.parent)
    return key, size


def preserve_group(connection: sqlite3.Connection, roots: ArtifactRoots, *, owner_kind: str,
                   owner_id: str, members: dict[str, tuple[str, str, Path]]) -> str:
    """Preserve all verified bytes before atomically registering the full group.

    A failed transaction can leave unreferenced immutable bytes, which inventory
    reports. It cannot expose a partly registered group or change a domain fact.
    """
    version = digest({role: [key, sha] for role, (key, sha, _) in sorted(members.items())})
    group_id = digest({"kind": owner_kind, "owner": owner_id, "version": version})
    preserved = {role: _preserve_object(roots, path, sha) for role, (_, sha, path) in members.items()}
    catalog = ArtifactCatalog(connection)
    target_id = "local-artifacts" if roots.configured else "local"
    catalog.register_target(target_id, kind="local")
    objects = {}
    for role, (_, sha, _) in members.items():
        key, size = preserved[role]
        objects[role] = catalog.register_object(sha, size)
        catalog.record_verified_replica(sha, target_id, key, sha256=sha, byte_size=size)
    catalog.register_group(group_id, owner_kind=owner_kind, owner_id=owner_id,
                           version=version, role=owner_kind, members=objects)
    for role, (key, _, _) in members.items():
        portable_artifact_parts(key)
        connection.execute("INSERT OR IGNORE INTO artifact_group_paths VALUES (?,?,?)", (group_id, role, key))
    return group_id


def preserve_transcript_bundle(connection: sqlite3.Connection, roots: ArtifactRoots,
                               publication_id: int, paths: dict[str, str], *, base: Path | None = None) -> str:
    base = base or roots.write_base
    if not archive_bundle_complete(base, paths):
        raise ValueError("cannot preserve incomplete or mixed transcript bundle")
    marker_key = str(Path(paths["srt_path"]).parent / BUNDLE_MARKER_NAME).replace("\\", "/")
    marker_path = base / marker_key
    encoded = marker_path.read_bytes()
    marker = json.loads(encoded)
    members = {role: (paths[role], marker["artifacts"][role]["sha256"], base / paths[role])
               for role in REQUIRED_ARTIFACT_KEYS}
    if marker.get("schema") == BUNDLE_SCHEMA_V3:
        metadata_key = str(Path(paths["srt_path"]).parent / "source-metadata.json").replace("\\", "/")
        item = marker.get("artifacts", {}).get(SOURCE_METADATA_KEY)
        if not isinstance(item, dict) or item.get("path") != metadata_key:
            raise ValueError("transcript bundle metadata sidecar is not declared")
        members[SOURCE_METADATA_KEY] = (metadata_key, item["sha256"], base / metadata_key)
    members["marker"] = (marker_key, hashlib.sha256(encoded).hexdigest(), marker_path)
    group_id = preserve_group(connection, roots, owner_kind="bundle", owner_id=f"bundle:{publication_id}", members=members)
    connection.execute("INSERT INTO artifact_publication_groups VALUES (?,?) ON CONFLICT(publication_id) DO UPDATE SET group_id=excluded.group_id",
                       (publication_id, group_id))
    return group_id


def preserve_existing_publication(connection: sqlite3.Connection, roots: ArtifactRoots, publication) -> str:
    paths = json.loads(publication["artifact_json"])
    selected = next((base for base in roots.read_bases() if archive_bundle_complete(base, paths)), None)
    if selected is None:
        raise ValueError("complete_group_unverified")
    raw = json.loads((selected / paths["raw_path"]).read_bytes())
    segments = [{"start": row[0] / 1000, "end": row[1] / 1000, "text": row[2]} for row in connection.execute(
        "SELECT start_ms,end_ms,text FROM transcript_segments WHERE transcript_id=? ORDER BY ordinal", (publication["transcript_id"],))]
    if not isinstance(raw, dict) or raw.get("segments") != segments:
        raise ValueError("transcript_version_unverified")
    return preserve_transcript_bundle(connection, roots, publication["publication_id"], paths, base=selected)


def capture_artifact_groups(roots: ArtifactRoots) -> dict:
    """Register extant immutable text groups and proven current bundle versions."""
    with _group_session(roots) as session:
        require_artifact_catalog(session.connection, required=True)
        inventory = inventory_artifacts(session.database_path, roots, deep=True, external_holds={})
        references = {ref["reference_id"]: ref for obj in inventory["objects"] for ref in obj["references"]}
        copies = {ref_id: copy for copy in reversed(inventory["copies"]) if copy["state"] == "verified" and not copy["issues"]
                  for ref_id in copy["reference_ids"]}
        groups = {}
        for ref in references.values():
            if ref["kind"] not in {"audio", "bundle"} and not ref["reference_id"].startswith("group:"):
                groups.setdefault(ref["group_id"], []).append(ref)
        captured, blocked = [], []
        for owner, refs in groups.items():
            if any(ref["reference_id"] not in copies or not ref["sha256"] for ref in refs):
                blocked.append({"owner": owner, "reason": "complete_group_unverified"})
                continue
            expected = {"document": 2, "source-evidence": 2, "migration": 3}.get(refs[0]["kind"], 1)
            if len(refs) != expected:
                blocked.append({"owner": owner, "reason": "complete_group_unverified"})
                continue
            members = {f"member-{index}": (ref["path"], ref["sha256"], Path(copies[ref["reference_id"]]["root"]) / ref["path"])
                       for index, ref in enumerate(sorted(refs, key=lambda ref: ref["reference_id"]))}
            with session.connection:
                captured.append(preserve_group(session.connection, roots, owner_kind=refs[0]["kind"], owner_id=owner, members=members))
        # Historical mutable slots have no durable version binding. Only the
        # latest publication can be matched to its actual stored transcript.
        for publication in session.connection.execute("SELECT * FROM workflow_publications p WHERE NOT EXISTS (SELECT 1 FROM workflow_publications newer WHERE newer.video_part_id=p.video_part_id AND (newer.published_at>p.published_at OR (newer.published_at=p.published_at AND newer.publication_id>p.publication_id)))").fetchall():
            try:
                with session.connection:
                    captured.append(preserve_existing_publication(session.connection, roots, publication))
            except ValueError as error:
                blocked.append({"owner": f"bundle:{publication['publication_id']}", "reason": str(error)})
        return {"operation": "artifact-capture-groups", "groups": captured, "blocked": blocked}


def restore_artifact_group(roots: ArtifactRoots, group_id: str, *, storage_targets: dict[str, Path]) -> dict:
    """Stage an entire original file group and install its completion marker last."""
    with _group_session(roots) as session:
        catalog = ArtifactCatalog(session.connection)
        group = session.connection.execute("SELECT * FROM artifact_groups WHERE group_id=?", (group_id,)).fetchone()
        if group is None:
            raise ValueError("unknown artifact group")
        members = session.connection.execute("SELECT p.role,p.relative_key,m.object_id,o.byte_size FROM artifact_group_paths p JOIN artifact_group_members m USING(group_id,role) JOIN artifact_objects o USING(object_id) WHERE p.group_id=? ORDER BY p.role='marker',p.role", (group_id,)).fetchall()
        if len(members) != group["member_count"]:
            raise ValueError("artifact group lacks its complete path bindings")
        if shutil.disk_usage(roots.write_base).free < sum(member["byte_size"] for member in members) + 16 * 1024**2:
            raise ValueError("insufficient local space to stage the complete artifact group")
        access = ArtifactAccess(session.connection, roots, storage_targets)
        def verify_destinations(*, require_present=False, include_marker=True):
            for member in members:
                if not include_marker and member["role"] == "marker":
                    continue
                path = roots.write_base.joinpath(*portable_artifact_parts(member["relative_key"]))
                require_safe_path(path)
                if require_present or path.exists():
                    require_regular_file(path)
                    with path.open("rb") as source:
                        if stream_hash(source) != (member["byte_size"], member["object_id"]):
                            raise ValueError("group restore refuses a different current version")
        verify_destinations()
        with tempfile.TemporaryDirectory(prefix=".artifact-group-restore-", dir=roots.write_base) as temporary:
            stage = Path(temporary)
            for member in members:
                staged = stage / member["relative_key"]
                staged.parent.mkdir(parents=True, exist_ok=True)
                local = next((base / object_storage_key(member["object_id"]) for base in roots.read_bases() if (base / object_storage_key(member["object_id"])).is_file()), None)
                with staged.open("xb") as destination:
                    if local is not None:
                        require_regular_file(local)
                        with local.open("rb") as source:
                            actual = stream_hash(source, destination)
                        if actual != (member["byte_size"], member["object_id"]):
                            raise ValueError("local immutable group object is corrupt")
                    else:
                        access.locate_external(member["object_id"]).copy_to(destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            if group["owner_kind"] == "bundle":
                paths = {member["role"]: member["relative_key"] for member in members if member["role"] != "marker"}
                if not archive_bundle_complete(stage, paths):
                    raise ValueError("restored transcript group has a mismatched marker")
            restored = 0
            target_id = "local-artifacts" if roots.configured else "local"
            # External retrieval can be slow. Never treat a newly appeared file
            # as our staged object merely because its name now exists.
            verify_destinations()
            installed_marker = None
            try:
                for member in members:
                    path = roots.write_base / member["relative_key"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    require_safe_path(path)
                    if member["role"] == "marker":
                        verify_destinations(require_present=True, include_marker=False)
                    if not path.exists():
                        if os.name == "nt":
                            os.rename(stage / member["relative_key"], path)
                        else:
                            os.link(stage / member["relative_key"], path)
                        if member["role"] == "marker":
                            installed_marker = (path, path.stat())
                        sync_directory(path.parent)
                        restored += member["byte_size"]
                verify_destinations(require_present=True)
                if group["owner_kind"] == "bundle" and not archive_bundle_complete(roots.write_base, paths):
                    raise ValueError("installed transcript group changed before registration")
            except BaseException:
                if installed_marker is not None:
                    path, created = installed_marker
                    try:
                        current = path.lstat()
                        if (current.st_dev, current.st_ino, current.st_mtime_ns) == (created.st_dev, created.st_ino, created.st_mtime_ns):
                            path.unlink()
                            sync_directory(path.parent)
                    except FileNotFoundError:
                        pass
                raise
            with session.connection:
                catalog.register_target(target_id, kind="local")
                for member in members:
                    previous = session.connection.execute("SELECT MAX(generation) FROM artifact_replicas WHERE target_id=? AND relative_key=? AND member_key=''", (target_id, member["relative_key"])).fetchone()[0]
                    catalog.record_verified_replica(member["object_id"], target_id, member["relative_key"], sha256=member["object_id"], byte_size=member["byte_size"], generation=int(previous or 0) + 1)
        return {"operation": "artifact-restore-group", "group_id": group_id, "members": len(members), "restored_bytes": restored}
