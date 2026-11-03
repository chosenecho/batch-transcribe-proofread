"""Explicit runtime contracts. Old archives are never altered during access.

The v1 resources and fixed migration source are independent. Only an explicit
empty-target bootstrap installs v2; its compatibility tables contain Bilibili
facts, while neutral source tables carry all providers.
"""
from __future__ import annotations

from functools import lru_cache
import re
import sqlite3

from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql as _normalize_manuscript_sql, schema_resource as _resource
from bili_asr.contracts.registry import BILIBILI_V1, UNIVERSAL_V2, RUNTIME_CONTRACTS

SUPPORTED_RUNTIME_CONTRACTS = RUNTIME_CONTRACTS
_BASE = ("schema.sql", "schema-transcripts.sql", "schema-workflow.sql", "schema-editorial.sql")


@lru_cache(maxsize=2)
def contract_scripts(contract: str) -> tuple[str, ...]:
    if contract not in SUPPORTED_RUNTIME_CONTRACTS:
        raise ValueError(f"unsupported archive contract: {contract}")
    scripts = [_resource(name) for name in _BASE]
    if contract == BILIBILI_V1:
        return tuple(scripts)
    # Preserve unchanged authority-table column ordering. Only parts gain a
    # neutral FK; legacy IDs and compatibility columns retain their positions.
    match = re.search(r"CREATE TABLE IF NOT EXISTS video_parts \(.*?\n\);", scripts[0], re.S)
    if match is None:
        raise RuntimeError("missing source part schema")
    part = match.group(0).replace("bvid TEXT NOT NULL", "bvid TEXT").replace(
        "cid INTEGER NOT NULL CHECK (cid > 0)", "cid INTEGER CHECK (cid IS NULL OR cid > 0)")
    part = part.replace("    UNIQUE (bvid, page_index),", "    source_video_id INTEGER REFERENCES source_videos(source_video_id),\n"
        "    UNIQUE (source_video_id, page_index),\n"
        "    CHECK ((bvid IS NOT NULL AND cid IS NOT NULL) OR (bvid IS NULL AND cid IS NULL AND source_video_id IS NOT NULL)),\n"
        "    UNIQUE (bvid, page_index),")
    scripts[0] = scripts[0][:match.start()] + part + scripts[0][match.end():]
    scripts[1] = scripts[1].replace("selector_kind IN ('pending', 'bvid')", "selector_kind IN ('pending', 'bvid', 'source-ref')").replace(
        "selector_kind = 'bvid' AND selector_target IS NOT NULL", "selector_kind IN ('bvid','source-ref') AND selector_target IS NOT NULL")
    scripts[1] = scripts[1].replace("vp.bvid || ':p' || vp.page_index", "CASE WHEN vp.bvid IS NOT NULL THEN vp.bvid || ':p' || vp.page_index "
        "ELSE (SELECT platform || ':' || external_id FROM source_videos WHERE source_video_id=vp.source_video_id) || ':p' || vp.page_index END")
    scripts[1] = scripts[1].replace("JOIN videos AS v ON vp.bvid = v.bvid", "JOIN source_videos AS v ON vp.source_video_id = v.source_video_id").replace(
        "    v.pubdate", "    v.published_at AS pubdate")
    scripts[1] = scripts[1].replace("CREATE VIEW IF NOT EXISTS v_missing_audio AS", "CREATE VIEW IF NOT EXISTS v_bilibili_missing_audio AS").replace(
        "CREATE VIEW IF NOT EXISTS v_part_pipeline AS", "CREATE VIEW IF NOT EXISTS v_bilibili_part_pipeline AS")
    scripts[3] = scripts[3].replace("template_version = 'ai-draft-v1'", "template_version IN ('ai-draft-v1','ai-draft-v2')").replace(
        "template_version = 'publish-v1'", "template_version IN ('publish-v1','publish-v2')")
    return (_resource("schema-content-v2.sql"), *scripts,
            _resource("schema-observations.sql"), _resource("schema-source-sync.sql"))


def runtime_contract(connection: sqlite3.Connection) -> str:
    """Read an explicit target marker; an unmarked archive is the legacy kind."""
    marker = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_contract'").fetchone()
    if marker is None:
        return BILIBILI_V1
    rows = [tuple(row) for row in connection.execute("SELECT singleton,contract FROM archive_contract")]
    if rows != [(1, UNIVERSAL_V2)]:
        raise ValueError("unsupported or altered archive contract marker")
    return UNIVERSAL_V2


@lru_cache(maxsize=2)
def contract_objects(contract: str) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(":memory:") as reference:
        for script in contract_scripts(contract):
            reference.executescript(script)
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def bootstrap_contract(connection: sqlite3.Connection, contract: str = UNIVERSAL_V2) -> sqlite3.Connection:
    """Install a registered schema only into a database with no user objects."""
    if connection.in_transaction:
        raise ValueError("archive bootstrap requires no active transaction")
    contract_scripts(contract)  # Validate before inspecting/mutating the target.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise ValueError("explicit bootstrap target database must be empty; use offline archive migrate")
    connection.execute("PRAGMA foreign_keys=ON")
    for script in contract_scripts(contract):
        connection.executescript(script)
    connection.commit()
    return connection


def require_universal_contract(connection: sqlite3.Connection) -> None:
    from bili_asr.storage.archive_validation import require_optional_extensions
    require_optional_extensions(connection)
    if runtime_contract(connection) != UNIVERSAL_V2:
        raise SchemaContractError("universal-v2 requires an explicitly initialized or migrated target")
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    for name, (kind, sql) in contract_objects(UNIVERSAL_V2).items():
        current = actual.get(name)
        if current is None or current[0] != kind or _normalize_manuscript_sql(current[1]) != _normalize_manuscript_sql(sql):
            raise SchemaContractError(f"universal-v2 schema missing or altered object: {name}")
    if connection.execute("SELECT 1 FROM video_parts WHERE source_video_id IS NULL LIMIT 1").fetchone():
        raise SchemaContractError("universal-v2 part is missing its neutral source identity")


def frozen_version(connection: sqlite3.Connection, entity: str, identity: str) -> int:
    """Legacy archives implicitly carry v1; target rows must register explicitly."""
    tables = {"input": ("editorial_input_versions", "input_id"),
              "content": ("publication_content_versions", "edition_id")}
    if entity not in tables:
        raise ValueError("unknown frozen contract entity")
    if runtime_contract(connection) == BILIBILI_V1:
        return 1
    table, key = tables[entity]
    row = connection.execute(f"SELECT version FROM {table} WHERE {key}=?", (identity,)).fetchone()
    if row is None:
        raise ValueError(f"missing frozen {entity} contract version")
    return int(row[0])


def register_frozen_version(connection: sqlite3.Connection, entity: str, identity: str, version: int) -> None:
    if version not in (1, 2) or type(version) is not int:
        raise ValueError("unsupported frozen contract version")
    if runtime_contract(connection) == BILIBILI_V1:
        if version != 1:
            raise ValueError("new frozen content requires universal-v2")
        return
    table, key = {"input": ("editorial_input_versions", "input_id"),
                  "content": ("publication_content_versions", "edition_id")}[entity]
    connection.execute(f"INSERT INTO {table}({key},version) VALUES (?,?) ON CONFLICT({key}) DO NOTHING", (identity, version))
    if frozen_version(connection, entity, identity) != version:
        raise ValueError("frozen contract version mismatch")
