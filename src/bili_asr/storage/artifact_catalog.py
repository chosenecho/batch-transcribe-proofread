"""Optional byte identity and replica facts in the archive's existing database.

No method opens files, installs schema, commits, or decides to release bytes.
Callers own the transaction and independently verify bytes before registering
a replica. Verification is historical evidence; presence is a separate, dated
observation and never establishes that a target is currently mounted.
"""
from __future__ import annotations
from bili_asr.storage.extension_contracts import require_artifact_catalog, artifact_catalog_objects as extension_objects

import re
import sqlite3
import time
import uuid

from bili_asr.artifact_inventory import portable_artifact_parts

EXTENSION = "artifact-storage-v1"
SCHEMA_RESOURCE = "schema-artifact-storage.sql"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}\Z")
TRANSFER_STATES = frozenset({"planned", "copying", "verified", "releasing", "complete", "failed"})
ITEM_STATES = frozenset({"planned", "copied", "verified", "released", "restored", "failed"})


class ArtifactCatalogError(ValueError):
    """The requested byte identity or replica fact conflicts with the catalog."""


def storage_key_parts(value: str) -> tuple[str, ...]:
    """Portable relative keys for local files, packages and package members."""
    # Reuse the same cross-platform component policy, without constraining a
    # storage target or ZIP member to the archive's five artifact directories.
    if not isinstance(value, str):
        raise ArtifactCatalogError("storage key must be a relative string")
    return portable_artifact_parts("documents/" + value)[1:]


def _hash(value: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ArtifactCatalogError("object identity must be a lowercase SHA-256")
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ArtifactCatalogError("invalid catalog identity")
    return value


def _number(value: int, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ArtifactCatalogError(f"{name} must be an integer at least {minimum}")
    return value


def _timestamp(value: int | None) -> int:
    return int(time.time()) if value is None else _number(value, "timestamp")






class ArtifactCatalog:
    """Stable objects and mutable observations; the caller commits write groups."""

    def __init__(self, connection: sqlite3.Connection):
        from bili_asr.storage.schema_contract import _validate_connection
        _validate_connection(connection)
        require_artifact_catalog(connection, required=True)
        self.connection = connection

    def register_object(self, sha256: str, byte_size: int, *, created_at: int | None = None) -> str:
        identity = _hash(sha256)
        _number(byte_size, "byte_size")
        row = self.connection.execute("SELECT byte_size FROM artifact_objects WHERE object_id=?", (identity,)).fetchone()
        if row is not None and row[0] != byte_size:
            raise ArtifactCatalogError("same digest has a different byte size")
        if row is None:
            self.connection.execute("INSERT INTO artifact_objects VALUES (?,?,?)", (identity, byte_size, _timestamp(created_at)))
        return identity

    def object(self, object_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM artifact_objects WHERE object_id=?", (_hash(object_id),)).fetchone()
        if row is None:
            raise ArtifactCatalogError("unknown artifact object")
        return dict(row)

    def bind_audio(self, audio_id: int, object_id: str) -> None:
        _number(audio_id, "audio_id", minimum=1)
        obj = self.object(object_id)
        audio = self.connection.execute("SELECT sha256,byte_size FROM audio_objects WHERE audio_id=?", (audio_id,)).fetchone()
        if audio is None or (audio[0], audio[1]) != (object_id, obj["byte_size"]):
            raise ArtifactCatalogError("audio binding differs from retained identity")
        row = self.connection.execute("SELECT object_id FROM artifact_audio_bindings WHERE audio_id=?", (audio_id,)).fetchone()
        if row is not None and row[0] != object_id:
            raise ArtifactCatalogError("audio binding is immutable")
        self.connection.execute("INSERT INTO artifact_audio_bindings VALUES (?,?) ON CONFLICT(audio_id) DO NOTHING", (audio_id, object_id))

    def audio_object(self, audio_id: int) -> str | None:
        row = self.connection.execute("SELECT object_id FROM artifact_audio_bindings WHERE audio_id=?", (audio_id,)).fetchone()
        return row[0] if row else None

    def register_target(self, target_id: str, *, kind: str = "directory", created_at: int | None = None) -> str:
        _identity(target_id)
        if kind not in {"local", "directory"}:
            raise ArtifactCatalogError("unsupported storage target kind")
        row = self.connection.execute("SELECT kind FROM artifact_targets WHERE target_id=?", (target_id,)).fetchone()
        if row is not None and row[0] != kind:
            raise ArtifactCatalogError("target kind is immutable")
        self.connection.execute("INSERT INTO artifact_targets VALUES (?,?,?) ON CONFLICT(target_id) DO NOTHING",
                                (target_id, kind, _timestamp(created_at)))
        return target_id

    def record_package(self, package_id: str, target_id: str, relative_key: str, *, sha256: str,
                       byte_size: int, manifest_sha256: str, verified_at: int | None = None) -> None:
        _identity(package_id)
        _identity(target_id)
        storage_key_parts(relative_key)
        _hash(sha256)
        _hash(manifest_sha256)
        _number(byte_size, "byte_size")
        values = (target_id, relative_key, sha256, byte_size, manifest_sha256)
        identity = self.connection.execute("SELECT sha256,byte_size,manifest_sha256 FROM artifact_packages WHERE package_id=? LIMIT 1", (package_id,)).fetchone()
        if identity is not None and tuple(identity) != (sha256, byte_size, manifest_sha256):
            raise ArtifactCatalogError("package identity or bytes differ from retained verification")
        row = self.connection.execute("SELECT package_id FROM artifact_packages WHERE target_id=? AND relative_key=?", (target_id, relative_key)).fetchone()
        if row is not None and row[0] != package_id:
            raise ArtifactCatalogError("package location already identifies another container")
        if row is None:
            self.connection.execute("INSERT INTO artifact_packages VALUES (?,?,?,?,?,?,?)",
                                    (package_id, *values, _timestamp(verified_at)))

    def record_verified_replica(self, object_id: str, target_id: str, relative_key: str, *,
                                sha256: str, byte_size: int, package_id: str | None = None,
                                member_key: str | None = None, generation: int = 1,
                                verified_at: int | None = None) -> int:
        obj = self.object(object_id)
        if (_hash(sha256), _number(byte_size, "byte_size")) != (object_id, obj["byte_size"]):
            raise ArtifactCatalogError("verified bytes differ from object identity")
        _identity(target_id)
        storage_key_parts(relative_key)
        _number(generation, "generation", minimum=1)
        if (package_id is None) != (member_key is None):
            raise ArtifactCatalogError("package and member must be supplied together")
        if member_key is not None:
            storage_key_parts(member_key)
            package = self.connection.execute("SELECT 1 FROM artifact_packages WHERE package_id=? AND target_id=? AND relative_key=?", (_identity(package_id), target_id, relative_key)).fetchone()
            if package is None:
                raise ArtifactCatalogError("package member belongs to another storage location")
        now = _timestamp(verified_at)
        member = member_key or ""
        location = (target_id, relative_key, member, generation)
        row = self.connection.execute("SELECT replica_id,object_id,package_id FROM artifact_replicas WHERE target_id=? AND relative_key=? AND member_key=? AND generation=?", location).fetchone()
        if row is not None:
            if (row[1], row[2]) != (object_id, package_id):
                raise ArtifactCatalogError("replica generation already identifies different bytes")
            self.connection.execute("UPDATE artifact_replicas SET verified_at=?,presence='present',observed_at=? WHERE replica_id=?", (now, now, row[0]))
            return int(row[0])
        self.connection.execute(
            "UPDATE artifact_replicas SET presence='unknown',observed_at=? WHERE target_id=? AND relative_key=? AND member_key=? AND generation!=? AND presence='present'",
            (now, *location))
        cursor = self.connection.execute(
            "INSERT INTO artifact_replicas(object_id,target_id,relative_key,package_id,member_key,generation,verified_sha256,verified_byte_size,verified_at,presence,observed_at) VALUES (?,?,?,?,?,?,?,?,?,'present',?)",
            (object_id, target_id, relative_key, package_id, member, generation, sha256, byte_size, now, now))
        return int(cursor.lastrowid)

    def replicas_for_object(self, object_id: str) -> list[dict]:
        self.object(object_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT r.*,t.kind AS target_kind,p.sha256 AS package_sha256,p.byte_size AS package_byte_size,p.manifest_sha256 "
            "FROM artifact_replicas r JOIN artifact_targets t USING(target_id) LEFT JOIN artifact_packages p ON p.package_id=r.package_id AND p.target_id=r.target_id AND p.relative_key=r.relative_key "
            "WHERE r.object_id=? ORDER BY r.verified_at DESC,r.replica_id", (object_id,))]

    def observe_replica(self, replica_id: int, *, presence: str, observed_at: int | None = None) -> None:
        _number(replica_id, "replica_id", minimum=1)
        if presence not in {"present", "missing", "released", "unknown"}:
            raise ArtifactCatalogError("unknown replica presence")
        row = self.connection.execute("SELECT observed_at FROM artifact_replicas WHERE replica_id=?", (replica_id,)).fetchone()
        if row is None:
            raise ArtifactCatalogError("unknown replica")
        now = _timestamp(observed_at)
        if now < row[0]:
            raise ArtifactCatalogError("cannot replace a newer replica observation")
        self.connection.execute("UPDATE artifact_replicas SET presence=?,observed_at=? WHERE replica_id=?", (presence, now, replica_id))

    def register_group(self, group_id: str, *, owner_kind: str, owner_id: str, version: str,
                       role: str, members: dict[str, str], created_at: int | None = None) -> str:
        values = tuple(_identity(value) for value in (group_id, owner_kind, owner_id, version, role))
        if not isinstance(members, dict) or not members:
            raise ArtifactCatalogError("artifact group requires its complete member set")
        for member_role, object_id in members.items():
            _identity(member_role)
            self.object(object_id)
        row = self.connection.execute("SELECT owner_kind,owner_id,version,role,member_count FROM artifact_groups WHERE group_id=?", (group_id,)).fetchone()
        if row is not None:
            existing = dict(self.connection.execute("SELECT role,object_id FROM artifact_group_members WHERE group_id=?", (group_id,)))
            if tuple(row) != (*values[1:], len(members)) or existing != members:
                raise ArtifactCatalogError("artifact group is immutable")
            return group_id
        # SAVEPOINT guarantees that an error cannot leave a partial file group,
        # while preserving the caller's enclosing write transaction.
        if not self.connection.in_transaction:
            self.connection.execute("BEGIN")
        self.connection.execute("SAVEPOINT artifact_group")
        try:
            self.connection.execute("INSERT INTO artifact_groups VALUES (?,?,?,?,?,?,?)", (*values, len(members), _timestamp(created_at)))
            self.connection.executemany("INSERT INTO artifact_group_members VALUES (?,?,?)", [(group_id, name, identity) for name, identity in sorted(members.items())])
        except BaseException:
            self.connection.execute("ROLLBACK TO artifact_group")
            raise
        finally:
            self.connection.execute("RELEASE artifact_group")
        return group_id

    def pin_object(self, object_id: str, reason: str, *, pin_id: str | None = None) -> str:
        self.object(object_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise ArtifactCatalogError("pin requires a bounded reason")
        identity = _identity(pin_id or str(uuid.uuid4()))
        row = self.connection.execute("SELECT object_id,reason,released_at FROM artifact_pins WHERE pin_id=?", (identity,)).fetchone()
        if row is not None:
            if tuple(row) != (object_id, reason, None):
                raise ArtifactCatalogError("pin identity differs from retained pin")
        else:
            self.connection.execute("INSERT INTO artifact_pins VALUES (?,?,?,?,NULL)", (identity, object_id, reason, _timestamp(None)))
        return identity

    def pinned(self, object_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM artifact_pins WHERE object_id=? AND released_at IS NULL LIMIT 1", (_hash(object_id),)).fetchone() is not None

    def release_pin(self, pin_id: str, *, released_at: int | None = None) -> None:
        now = _timestamp(released_at)
        row = self.connection.execute("SELECT created_at,released_at FROM artifact_pins WHERE pin_id=?", (_identity(pin_id),)).fetchone()
        if row is None or now < row[0]:
            raise ArtifactCatalogError("unknown pin or release precedes creation")
        if row[1] is None:
            self.connection.execute("UPDATE artifact_pins SET released_at=? WHERE pin_id=?", (now, pin_id))

    def begin_transfer(self, transfer_id: str, *, kind: str, plan_sha256: str,
                       target_id: str | None = None, created_at: int | None = None) -> None:
        _identity(transfer_id)
        _hash(plan_sha256)
        if kind not in {"copy", "offload", "restore"}:
            raise ArtifactCatalogError("unknown transfer kind")
        if target_id is not None:
            _identity(target_id)
        row = self.connection.execute("SELECT kind,plan_sha256,target_id FROM artifact_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is not None and tuple(row) != (kind, plan_sha256, target_id):
            raise ArtifactCatalogError("transfer identity conflicts with its frozen plan")
        if row is None:
            now = _timestamp(created_at)
            self.connection.execute("INSERT INTO artifact_transfers VALUES (?,?,?,?,'planned',NULL,?,?)", (transfer_id, kind, plan_sha256, target_id, now, now))

    def set_transfer_state(self, transfer_id: str, state: str, *, error_code: str | None = None,
                           updated_at: int | None = None) -> None:
        if state not in TRANSFER_STATES:
            raise ArtifactCatalogError("unknown transfer state")
        if self.connection.execute("SELECT 1 FROM artifact_transfers WHERE transfer_id=?", (_identity(transfer_id),)).fetchone() is None:
            raise ArtifactCatalogError("unknown transfer")
        self.connection.execute("UPDATE artifact_transfers SET state=?,error_code=?,updated_at=? WHERE transfer_id=?", (state, error_code, _timestamp(updated_at), transfer_id))

    def record_transfer_item(self, transfer_id: str, object_id: str, *, state: str,
                             source_replica_id: int | None = None, target_replica_id: int | None = None,
                             error_code: str | None = None, updated_at: int | None = None) -> None:
        _identity(transfer_id)
        self.object(object_id)
        if state not in ITEM_STATES:
            raise ArtifactCatalogError("unknown transfer item state")
        for replica_id in (source_replica_id, target_replica_id):
            if replica_id is not None:
                row = self.connection.execute("SELECT object_id FROM artifact_replicas WHERE replica_id=?", (_number(replica_id, "replica_id", minimum=1),)).fetchone()
                if row is None or row[0] != object_id:
                    raise ArtifactCatalogError("transfer item replica identifies different bytes")
        self.connection.execute(
            "INSERT INTO artifact_transfer_items VALUES (?,?,?,?,?,?,?) ON CONFLICT(transfer_id,object_id) DO UPDATE SET source_replica_id=excluded.source_replica_id,target_replica_id=excluded.target_replica_id,state=excluded.state,error_code=excluded.error_code,updated_at=excluded.updated_at",
            (transfer_id, object_id, source_replica_id, target_replica_id, state, error_code, _timestamp(updated_at)))
