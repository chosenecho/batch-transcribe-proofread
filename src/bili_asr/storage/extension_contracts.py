"""Read-only optional contract validators, independent of business repositories."""
from __future__ import annotations
import json
import sqlite3
from functools import lru_cache
from bili_asr.artifact_inventory import portable_artifact_parts
from bili_asr.canonical_json import digest
from bili_asr.contracts.registry import IMPORT_EXTENSION
from bili_asr.storage.schema_contract import schema_resource as _resource
from bili_asr.source_supplements import POLICY as SUPPLEMENT_POLICY

def _storage_key_parts(value):
    if not isinstance(value, str):
        raise ValueError("storage key must be a relative string")
    return portable_artifact_parts("documents/" + value)[1:]

@lru_cache(maxsize=1)
def artifact_catalog_objects() -> dict[str, tuple[str, str]]:
    from bili_asr.storage.schema_contract import schema_resource as _resource
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(_resource("schema-artifact-storage.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def require_artifact_catalog(connection: sqlite3.Connection, *, required: bool = False) -> bool:
    """Read-only verification; reject partial, unknown and altered extensions."""
    from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql as _normalize_manuscript_sql
    expected = artifact_catalog_objects()
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if not expected.keys() & actual.keys():
        if required:
            raise SchemaContractError("artifact-storage-v1: run explicit artifact catalog upgrade into a separate empty target first")
        return False
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or _normalize_manuscript_sql(current[1]) != _normalize_manuscript_sql(sql):
            raise SchemaContractError(f"artifact-storage-v1: missing or altered object: {name}")
    if [tuple(row) for row in connection.execute("SELECT singleton,version FROM artifact_storage_contract")] != [(1, 1)]:
        raise SchemaContractError("artifact-storage-v1: unsupported contract marker")
    if connection.execute(
        "SELECT 1 FROM artifact_audio_bindings b JOIN artifact_objects o USING(object_id) "
        "JOIN audio_objects a USING(audio_id) WHERE o.object_id!=a.sha256 OR o.byte_size!=a.byte_size LIMIT 1"
    ).fetchone():
        raise SchemaContractError("artifact-storage-v1: audio binding differs from retained identity")
    if connection.execute(
        "SELECT 1 FROM artifact_replicas r JOIN artifact_objects o USING(object_id) "
        "WHERE r.verified_byte_size!=o.byte_size LIMIT 1"
    ).fetchone():
        raise SchemaContractError("artifact-storage-v1: replica size differs from byte identity")
    if connection.execute(
        "SELECT 1 FROM artifact_groups g WHERE g.member_count!=(SELECT COUNT(*) FROM artifact_group_members m WHERE m.group_id=g.group_id) LIMIT 1"
    ).fetchone():
        raise SchemaContractError("artifact-storage-v1: incomplete artifact group")
    for relative_key, member_key in connection.execute("SELECT relative_key,member_key FROM artifact_replicas"):
        try:
            _storage_key_parts(relative_key)
            if member_key:
                _storage_key_parts(member_key)
        except ValueError as exc:
            raise SchemaContractError("artifact-storage-v1: unsafe replica location") from exc
    for relative_key, in connection.execute("SELECT relative_key FROM artifact_packages"):
        try:
            _storage_key_parts(relative_key)
        except ValueError as exc:
            raise SchemaContractError("artifact-storage-v1: unsafe package location") from exc
    for relative_key, in connection.execute("SELECT relative_key FROM artifact_group_paths"):
        try:
            portable_artifact_parts(relative_key)
        except ValueError as exc:
            raise SchemaContractError("artifact-storage-v1: unsafe group path") from exc
    if connection.execute(
        "SELECT 1 FROM artifact_groups g WHERE EXISTS(SELECT 1 FROM artifact_group_paths p WHERE p.group_id=g.group_id) "
        "AND g.member_count!=(SELECT COUNT(*) FROM artifact_group_paths p WHERE p.group_id=g.group_id) LIMIT 1"
    ).fetchone():
        raise SchemaContractError("artifact-storage-v1: incomplete group path bindings")
    for row in connection.execute(
        "SELECT b.publication_id,g.owner_kind,g.owner_id,g.member_count,g.group_id,w.artifact_json "
        "FROM artifact_publication_groups b JOIN artifact_groups g USING(group_id) JOIN workflow_publications w USING(publication_id)"
    ):
        try:
            from pathlib import PurePosixPath

            from bili_asr.artifacts import BUNDLE_MARKER_NAME, REQUIRED_ARTIFACT_KEYS, SOURCE_METADATA_KEY
            declared = json.loads(row[5])
            expected_paths = {role: declared[role] for role in REQUIRED_ARTIFACT_KEYS}
            expected_paths["marker"] = str(PurePosixPath(declared["srt_path"]).parent / BUNDLE_MARKER_NAME)
            actual_paths = dict(connection.execute("SELECT role,relative_key FROM artifact_group_paths WHERE group_id=?", (row[4],)))
            metadata_paths = {role: path for role, path in actual_paths.items() if role == SOURCE_METADATA_KEY}
            if metadata_paths:
                expected_metadata = str(PurePosixPath(declared["srt_path"]).parent / "source-metadata.json")
                if metadata_paths[SOURCE_METADATA_KEY] != expected_metadata:
                    raise ValueError("publication metadata sidecar path differs")
            expected_paths.update(metadata_paths)
            if row[1] != "bundle" or row[2] != f"bundle:{row[0]}" or row[3] != len(expected_paths) or actual_paths != expected_paths:
                raise ValueError("publication group identity differs")
        except (TypeError, ValueError, KeyError) as exc:
            raise SchemaContractError("artifact-storage-v1: invalid publication group binding") from exc
    for upgrade_id, report_key, inventory_digest in connection.execute("SELECT upgrade_id,report_key,inventory_sha256 FROM artifact_catalog_upgrades"):
        try:
            portable_artifact_parts(report_key)
            inventory = []
            for key, sha256, size in connection.execute("SELECT relative_key,sha256,byte_size FROM artifact_catalog_upgrade_files WHERE upgrade_id=? ORDER BY relative_key", (upgrade_id,)):
                portable_artifact_parts(key)
                inventory.append({"path": key, "sha256": sha256, "size": size})
                if len(inventory) > 1_000_000:
                    raise ValueError("upgrade inventory exceeds supported limit")
            if digest(inventory) != inventory_digest:
                raise ValueError("upgrade inventory differs from frozen identity")
        except ValueError as exc:
            raise SchemaContractError("artifact-storage-v1: invalid upgrade evidence inventory") from exc
    if connection.execute(
        "SELECT 1 FROM artifact_transfer_items i LEFT JOIN artifact_replicas s ON s.replica_id=i.source_replica_id "
        "LEFT JOIN artifact_replicas t ON t.replica_id=i.target_replica_id "
        "WHERE (i.source_replica_id IS NOT NULL AND s.object_id!=i.object_id) "
        "OR (i.target_replica_id IS NOT NULL AND t.object_id!=i.object_id) LIMIT 1"
    ).fetchone():
        raise SchemaContractError("artifact-storage-v1: transfer replica binding mismatch")
    for source_key, quarantine_key, generation_raw, object_id, target_id, replica_id in connection.execute(
        "SELECT source_key,quarantine_key,source_generation_json,object_id,source_target_id,source_replica_id FROM artifact_release_intents"
    ):
        try:
            _storage_key_parts(source_key)
            _storage_key_parts(quarantine_key)
            generation = json.loads(generation_raw)
            if (not isinstance(generation, dict)
                    or set(generation) != {"device", "inode", "size_bytes", "mtime_ns", "ctime_ns"}
                    or any(type(value) is not int or value < 0 for value in generation.values())):
                raise ValueError("invalid physical source generation")
        except (TypeError, ValueError) as exc:
            raise SchemaContractError("artifact-storage-v1: unsafe release intent or generation") from exc
        if replica_id is not None:
            replica = connection.execute("SELECT object_id,target_id,relative_key FROM artifact_replicas WHERE replica_id=?", (replica_id,)).fetchone()
            if replica is None or tuple(replica) != (object_id, target_id, source_key):
                raise SchemaContractError("artifact-storage-v1: release intent differs from source replica")
    return True


@lru_cache(maxsize=1)
def artifact_online_objects():
    from bili_asr.storage.schema_contract import schema_resource as _resource
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(_resource("schema-artifact-online.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def require_artifact_online(connection: sqlite3.Connection, *, required: bool = False) -> bool:
    from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql as _normalize_manuscript_sql
    expected = artifact_online_objects()
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if not expected.keys() & actual.keys():
        if required:
            raise SchemaContractError("artifact-online-v1: run explicit archive upgrade into a separate empty target first")
        return False
    require_artifact_catalog(connection, required=True)
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or _normalize_manuscript_sql(current[1]) != _normalize_manuscript_sql(sql):
            raise SchemaContractError(f"artifact-online-v1: missing or altered object: {name}")
    if [tuple(row) for row in connection.execute("SELECT singleton,version FROM artifact_online_contract")] != [(1, 1)]:
        raise SchemaContractError("artifact-online-v1: unsupported contract marker")
    return True


@lru_cache(maxsize=1)
def import_objects() -> dict:
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(_resource("schema-preserved-body-import.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def require_import_extension(connection: sqlite3.Connection, *, required: bool = False) -> bool:
    from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql as _normalize_manuscript_sql
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = import_objects()
    if not expected.keys() & actual.keys():
        if required:
            raise SchemaContractError("install preserved-body-import-v1 on an offline archive copy first")
        return False
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or _normalize_manuscript_sql(current[1]) != _normalize_manuscript_sql(sql):
            raise SchemaContractError(f"missing or altered import extension object: {name}")
    if [tuple(row) for row in connection.execute("SELECT name,version FROM archive_contract_extensions")] != [(IMPORT_EXTENSION, 1)]:
        raise SchemaContractError("unknown archive contract extension")
    return True


@lru_cache(maxsize=1)
def supplement_objects() -> dict:
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(_resource("schema-source-supplements.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def require_supplement_extension(connection, *, required=False) -> bool:
    from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql as _normalize_manuscript_sql
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = supplement_objects()
    if not expected.keys() & actual.keys():
        if required:
            raise SchemaContractError("source-supplement: install on an offline archive copy first")
        return False
    if "publication_import_origins" not in actual:
        raise SchemaContractError("source-supplement: preservation extension is required")
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or _normalize_manuscript_sql(current[1]) != _normalize_manuscript_sql(sql):
            raise SchemaContractError(f"source-supplement: missing or altered extension object: {name}")
    if [tuple(row) for row in connection.execute("SELECT singleton,policy FROM source_supplement_contract")] != [(1, SUPPLEMENT_POLICY)]:
        raise SchemaContractError("source-supplement: altered policy marker")
    return True
