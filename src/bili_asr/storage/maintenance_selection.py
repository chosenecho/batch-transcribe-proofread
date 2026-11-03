"""Typed native row closure for private, offline maintenance exports.

The destination is a fresh database: excluded rows never enter its freelist.
Selection follows outward FKs and explicitly owned histories, not all reverse
references (a creator, shared profile or acquisition run must not pull siblings).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from bili_asr.storage.archive_contracts import runtime_contract
from bili_asr.storage.migration_source import typed_table_fingerprint
from bili_asr.storage.snapshots import (
    _current_contract,
    _schema,
    validate_snapshot_database,
)
from bili_asr.storage.snapshots import _identifier as quote
from bili_asr.storage.upgrade_evidence import readonly


def _registered_contract(connection: sqlite3.Connection, kind: str) -> tuple[dict, str, bool]:
    """Only native, observation and content-rights schemas have a row closure.

    Other registered extensions remain valid for full export, but their
    archive-wide receipts cannot safely be split by this selector.
    """
    from bili_asr.storage.source_observations import require_observations

    observations = require_observations(connection)
    rights = connection.execute("SELECT 1 FROM sqlite_master WHERE name='content_rights_events' AND type='table'").fetchone() is not None
    expected, fingerprint = _current_contract(kind, observations=observations, rights=rights)
    return expected, fingerprint, observations


_OWNED = {
    ("transcript_segments", "transcript_id"),
    ("workflow_attempts", "job_id"), ("workflow_job_dependencies", "job_id"),
    ("editorial_job_inputs", "job_id"), ("workflow_asr_profile_configs", "profile_id"),
    ("editorial_model_calls", "input_id"), ("editorial_chunk_results", "input_id"),
    ("editorial_revisions", "input_id"), ("editorial_input_versions", "input_id"),
    ("document_artifacts", "revision_id"),
    ("publication_edition_reviews", "edition_id"), ("publication_content_versions", "edition_id"),
    ("video_details", "bvid"), ("video_tags", "bvid"),
    ("video_tag_observations", "bvid"), ("ingestion_discoveries", "bvid"),
    ("ingestion_pages", "run_id"),
}
# Only these durable JSON envelopes carry database identities. Diagnostic
# provider responses and frozen prose are deliberately not recursively guessed.
_JSON_EDGES = {
    "video_part_id": ("video_parts", "video_part_id"),
    "profile_id": ("workflow_asr_profiles", "profile_id"),
    "transcript_id": ("transcripts", "transcript_id"),
    "reference_transcript_id": ("transcripts", "transcript_id"),
    "requested_transcript_id": ("transcripts", "transcript_id"),
    "base_transcript_id": ("transcripts", "transcript_id"),
    "publication_id": ("workflow_publications", "publication_id"),
    "input_id": ("editorial_inputs", "input_id"),
    "revision_id": ("editorial_revisions", "revision_id"),
    "asr_job_id": ("workflow_jobs", "job_id"),
    "proofread_job_id": ("workflow_jobs", "job_id"),
    "repair_of_job_id": ("workflow_jobs", "job_id"),
    "publication_job_id": ("workflow_jobs", "job_id"),
    "run_id": ("acquisition_runs", "run_id"),
}


def table_ledger(database: Path) -> list[dict]:
    with closing(readonly(database)) as connection:
        result = []
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quote(name)})")]
            result.append({**asdict(typed_table_fingerprint(connection, name, columns)), "columns": columns})
        return result


def refuse_unfinished(database: Path) -> None:
    with closing(readonly(database)) as connection:
        for table, condition in (
            ("workflow_jobs", "status='running'"), ("workflow_attempts", "outcome='running'"),
            ("ingestion_runs", "outcome='running'"), ("acquisition_runs", "outcome='running'"),
            ("editorial_model_calls", "finished_at IS NULL"),
        ):
            if connection.execute(f"SELECT 1 FROM {quote(table)} WHERE {condition} LIMIT 1").fetchone():
                raise ValueError("unfinished execution requires explicit recovery before maintenance export: " + table)


def select_database(source: Path, destination: Path, *, part_ids: list[int], bvids: list[str], scope: dict | None = None) -> dict:
    """Materialize a closed, native subset, preserving rowid and SQLite values."""
    from bili_asr.artifact_inventory import require_no_links
    require_no_links(source)
    require_no_links(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("selected destination must be a fresh database distinct from its source")
    validate_snapshot_database(source)
    with closing(readonly(source)) as origin:
        kind = runtime_contract(origin)
        expected, _, has_observations = _registered_contract(origin, kind)
        if _schema(origin) != expected:
            raise ValueError("selective export supports native schemas only with source-observations/v1 and content-rights-v1; other extensions require full export")
        if "migration_records" in expected and origin.execute("SELECT 1 FROM migration_records LIMIT 1").fetchone():
            raise ValueError("selective export cannot split whole-archive migration evidence; use full export")
        objects = list(origin.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        columns = {name: [row[1] for row in origin.execute(f"PRAGMA table_info({quote(name)})")]
                   for obj, name, _ in objects if obj == "table"}
        selected = set(part_ids) | set(scope["part_ids"] if scope is not None else [])
        for bvid in sorted(set(bvids) | set(scope["bvids"] if scope is not None else [])):
            ids = [row[0] for row in origin.execute("SELECT video_part_id FROM video_parts WHERE bvid=?", (bvid,))]
            if not ids:
                raise ValueError("selected video has no archived parts: " + bvid)
            selected.update(ids)
        if not selected and scope is not None:
            selected.update(row[0] for row in origin.execute("SELECT video_part_id FROM video_parts"))
        if not selected:
            raise ValueError("selection must contain at least one part")
        for identity in sorted(selected):
            if type(identity) is not int or identity < 1 or not origin.execute("SELECT 1 FROM video_parts WHERE video_part_id=?", (identity,)).fetchone():
                raise ValueError("selected part does not exist: " + str(identity))

    with closing(sqlite3.connect(destination.as_uri(), uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS source", (source.as_uri() + "?mode=ro&immutable=1",))
        for obj, name, sql in objects:
            if obj == "table":
                connection.execute(sql)
                connection.execute(f"CREATE TEMP TABLE {quote('keep_' + name)} (id INTEGER PRIMARY KEY)")

        def keep(name: str) -> str:
            return "temp." + quote("keep_" + name)

        def add(name: str, query: str, params=()) -> int:
            return connection.execute(f"INSERT OR IGNORE INTO {keep(name)} SELECT rowid FROM source.{quote(name)} WHERE {query}", params).rowcount

        explicit_rows = {}
        if scope is None:
            for identity in sorted(selected):
                add("video_parts", "video_part_id=?", (identity,))
        else:
            from bili_asr.storage.maintenance_scope import DATASETS, predicate
            seeds = set().union(*(DATASETS[key] for key in scope["datasets"])) if scope["datasets"] else set(columns)
            ids = ",".join("?" for _ in selected)
            for name in sorted(seeds & set(columns)):
                names = columns[name]
                clause, arguments = predicate(names, scope)
                if "video_part_id" in names:
                    clause += f" AND video_part_id IN ({ids})"
                    arguments += tuple(sorted(selected))
                elif "bvid" in names:
                    clause += f" AND bvid IN (SELECT bvid FROM source.video_parts WHERE video_part_id IN ({ids}))"
                    arguments += tuple(sorted(selected))
                # Shared no-part tables become dependencies through FKs. They
                # must not make a scoped request include unrelated executions.
                else:
                    continue
                explicit_rows[name] = add(name, clause, arguments)
        for table in ("archive_contract", "manuscript_contract", "platform_observation_contract"):
            if table in columns:
                add(table, "1")
        fks = []
        for name in columns:
            groups = defaultdict(list)
            for row in connection.execute(f"PRAGMA source.foreign_key_list({quote(name)})"):
                groups[row[0]].append(row)
            for rows in groups.values():
                parent = rows[0][2]
                condition = " AND ".join(f"c.{quote(row[3])}=p.{quote(row[4])}" for row in rows)
                fks.append((name, parent, condition, (name, rows[0][3]) in _OWNED))
        examined = set()
        observation_boundary = {"shared_creator_pages": [], "raw_observation_rows": 0}
        while True:
            changed = 0
            if "content_rights_events" in columns:
                changed += add("content_rights_events", f"video_part_id IN (SELECT video_part_id FROM source.video_parts WHERE rowid IN (SELECT id FROM {keep('video_parts')}))")
            if scope is not None and "publication_editions" in columns and "publication_heads" in columns:
                changed += add("publication_heads", f"video_part_id IN (SELECT video_part_id FROM source.publication_editions WHERE rowid IN (SELECT id FROM {keep('publication_editions')}))")
            # Every newly introduced part includes its complete history.
            for name, names in columns.items():
                if scope is None and "video_part_id" in names and name != "video_parts":
                    changed += add(name, f"video_part_id IN (SELECT video_part_id FROM source.video_parts WHERE rowid IN (SELECT id FROM {keep('video_parts')}))")
            for child, parent, condition, owned in fks:
                changed += add(parent, f"rowid IN (SELECT p.rowid FROM source.{quote(parent)} p JOIN source.{quote(child)} c ON {condition} WHERE c.rowid IN (SELECT id FROM {keep(child)}))")
                if owned and (scope is None or child in {"transcript_segments", "workflow_attempts", "workflow_job_dependencies",
                                                           "editorial_model_calls", "editorial_chunk_results", "editorial_input_versions",
                                                           "workflow_asr_profile_configs", "publication_edition_reviews", "publication_content_versions"}):
                    changed += add(child, f"rowid IN (SELECT c.rowid FROM source.{quote(child)} c JOIN source.{quote(parent)} p ON {condition} WHERE p.rowid IN (SELECT id FROM {keep(parent)}))")
            # Raw receipts have no authority FKs. Their explicit closure also
            # follows adopted caches and frozen source metadata references.
            from bili_asr.storage.maintenance_observations import close_observations
            changed += close_observations(connection, columns, keep, add,
                                          has_observations=has_observations)
            for table, column in (("workflow_jobs", "payload_json"), ("workflow_attempts", "result_json"),
                                  ("transcript_asr_evidence", "evidence_json")):
                for rowid, raw in connection.execute(f"SELECT rowid,{quote(column)} FROM source.{quote(table)} WHERE rowid IN (SELECT id FROM {keep(table)})"):
                    token = (table, rowid)
                    if token in examined:
                        continue
                    examined.add(token)
                    if raw is None:
                        continue
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("unsupported selected workflow JSON envelope")  # noqa: TRY004 - invalid stored JSON
                    if table == "workflow_jobs" and payload.get("schema_version", 1) != 1:
                        raise ValueError("unsupported selected workflow payload version")
                    if table == "transcript_asr_evidence" and payload.get("schema_version") != 1:
                        raise ValueError("unsupported selected ASR evidence version")
                    if table == "workflow_jobs":
                        for key in payload:
                            if key.endswith("_id") and key not in _JSON_EDGES and key != "publication_request_id":
                                raise ValueError("unsupported selected workflow payload identity: " + key)
                    values = [(key, value) for key, value in payload.items()]
                    if table == "transcript_asr_evidence":
                        values = [(key, payload[key]) for key in ("reference_transcript_id", "profile_id") if key in payload]
                    fence = payload.get("preserve_publication")
                    if isinstance(fence, dict):
                        if set(fence) != {"publication_id", "published_at", "transcript_sha256", "segments_sha256"}:
                            raise ValueError("unsupported selected publication refresh fence")
                        values.extend(fence.items())
                    for key, value in values:
                        if key not in _JSON_EDGES or value is None:
                            continue
                        target, key_column = _JSON_EDGES[key]
                        if not connection.execute(f"SELECT 1 FROM source.{quote(target)} WHERE {quote(key_column)}=?", (value,)).fetchone():
                            raise ValueError("dangling selected workflow JSON identity: " + key)
                        changed += add(target, f"{quote(key_column)}=?", (value,))
            if not changed:
                break
        resolved = [row[0] for row in connection.execute(f"SELECT video_part_id FROM source.video_parts WHERE rowid IN (SELECT id FROM {keep('video_parts')}) ORDER BY video_part_id")]
        for name, names in columns.items():
            fields = ",".join(["rowid", *(quote(field) for field in names)])
            connection.execute(f"INSERT INTO {quote(name)}({fields}) SELECT {fields} FROM source.{quote(name)} WHERE rowid IN (SELECT id FROM {keep(name)}) ORDER BY rowid")
        for obj, _, sql in objects:
            if obj != "table":
                connection.execute(sql)
        if "platform_observations" in columns:
            observation_boundary["shared_creator_pages"] = [row[0] for row in connection.execute(
                f"SELECT observation_id FROM source.platform_observations "
                f"WHERE operation='creator-page' AND rowid IN (SELECT id FROM {keep('platform_observations')}) "
                "ORDER BY observation_id"
            )]
            observation_boundary["raw_observation_rows"] = connection.execute(
                f"SELECT COUNT(*) FROM {keep('platform_observations')}"
            ).fetchone()[0]
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("selected closure contains a dangling foreign key")
    validate_snapshot_database(destination)
    result = {"requested_part_ids": sorted(selected), "resolved_part_ids": resolved,
              "policy": "native-full-part-history-fk-owned-json/v1"}
    if scope is not None:
        result.update({"policy": "native-explicit-scope-fk-owned-json/v2", "scope": scope,
                       "explicit_row_counts": explicit_rows})
    if has_observations:
        result["observation_policy"] = "source-observations-closure/v1"
        observation_boundary["shared_creator_pages"].sort()
        result["observation_boundary"] = observation_boundary
    return result
