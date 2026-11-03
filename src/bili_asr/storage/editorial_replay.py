"""Narrow response reuse and an explicit, exactly validated derived index group."""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache

from bili_asr.canonical_json import digest
from bili_asr.sql_identity import schema_sql_sha256
from bili_asr.storage.schema_contract import SchemaContractError, schema_resource
from bili_asr.workflow_errors import JobExecutionError

POLICY = "editorial-replay-index/v1"
FINGERPRINT = "CASE WHEN json_valid(request_json) THEN json_extract(request_json, '$.request_fingerprint') END"
ACCEPTED = "response_json IS NOT NULL AND error_code IS NULL"
MAX_LEGACY_ROWS = 256


class ReplayEvidenceError(JobExecutionError, ValueError):
    error_code = "editorial_replay_evidence_invalid"

    def __init__(self, call_id, reason):
        super().__init__(self.error_code, {"stage": "response_replay", "reason_code": reason, "call_id": call_id})


@lru_cache(maxsize=1)
def objects():
    with sqlite3.connect(":memory:") as reference:
        reference.execute("CREATE TABLE editorial_model_calls(input_id TEXT,chunk_id TEXT,request_json TEXT,response_json TEXT,error_code TEXT,finished_at INTEGER)")
        reference.executescript(schema_resource("schema-editorial-replay-index.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}


def derived_group():
    return {"policy": POLICY, "objects": {name: [{"kind": kind, "sql_sha256": schema_sql_sha256(sql)}]
            for name, (kind, sql) in objects().items()}}


def require_indexes(connection, *, allow_partial=False):
    expected = objects()
    present = {}
    for name in expected:
        row = connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None:
            present[name] = (row[0], row[1])
            if row[0] != expected[name][0] or schema_sql_sha256(row[1]) != schema_sql_sha256(expected[name][1]):
                raise SchemaContractError(f"{POLICY}: altered derived object {name}")
    if present and len(present) != len(expected) and not allow_partial:
        raise SchemaContractError(f"{POLICY}: incomplete derived index group; run explicit recovery")
    return len(present) == len(expected)


def install_indexes(connection, *, recover=False, checkpoint=lambda: None):
    """Atomic DDL; interrupted installs commit both indexes or neither."""
    require_indexes(connection, allow_partial=recover)
    if connection.in_transaction:
        raise ValueError("index installation requires its own explicit transaction")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        require_indexes(connection, allow_partial=recover)
        for name, (_, sql) in objects().items():
            checkpoint()
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is None:
                connection.execute(sql)
        checkpoint()
        require_indexes(connection)
    return {"policy": POLICY, "installed": True, "content_rows_changed": 0}


def _stored_request(row):
    try:
        stored = json.loads(row["request_json"])
        if not isinstance(stored, dict) or not isinstance(stored.get("request", stored), dict):
            raise TypeError()
        request = stored.get("request", stored)
        actual = digest({"input_id": row["input_id"], "chunk_id": row["chunk_id"], "request": request})
    except (ValueError, TypeError):
        raise ReplayEvidenceError(row["call_id"], "request_json_invalid") from None
    return stored, actual


def completed_record(connection, fingerprint, *, input_id=None, chunk_id=None):
    """Read large response only after selecting one matching request identity.

    Runtime always supplies a frozen input/chunk scope for legacy evidence.
    Unscoped compatibility reads have an explicit bounded fallback; exhaustion
    fails rather than silently sending a duplicate paid request.
    """
    indexed = require_indexes(connection)
    if (input_id is None) != (chunk_id is None):
        raise ValueError("legacy replay requires both input_id and chunk_id")
    # Without the expression index, even a JSON-safe fingerprint query would
    # parse every unrelated request. The bounded scoped compatibility path
    # below is the only old-library query; no global JSON expression scan.
    row = connection.execute(
        f"SELECT rowid,finished_at,call_id,input_id,chunk_id,request_json FROM editorial_model_calls WHERE {ACCEPTED} "
        f"AND {FINGERPRINT}=? ORDER BY finished_at DESC,rowid DESC LIMIT 1", (fingerprint,)).fetchone() if indexed else None
    selected = row
    if row is not None:
        stored, actual = _stored_request(row)
        if actual != fingerprint or stored.get("request_fingerprint") != actual:
            raise ReplayEvidenceError(row["call_id"], "request_fingerprint_mismatch")
        input_id, chunk_id = row["input_id"], row["chunk_id"]
    query = f"SELECT rowid,finished_at,call_id,input_id,chunk_id,request_json FROM editorial_model_calls WHERE {ACCEPTED}"
    parameters = []
    if input_id is not None:
        query += " AND input_id=? AND chunk_id=?"
        parameters.extend((input_id, chunk_id))
    if indexed:
        query += f" AND {FINGERPRINT} IS NULL"
    if selected is not None:
        query += " AND (finished_at>? OR (finished_at=? AND rowid>?))"
        parameters.extend((selected["finished_at"], selected["finished_at"], selected["rowid"]))
    query += " ORDER BY finished_at DESC,rowid DESC LIMIT ?"
    parameters.append(MAX_LEGACY_ROWS + 1)
    candidates = connection.execute(query, parameters).fetchall()
    if len(candidates) > MAX_LEGACY_ROWS:
        raise ReplayEvidenceError("legacy-scope", "legacy_replay_scope_exhausted")
    for candidate in candidates:
        stored, actual = _stored_request(candidate)
        if actual == fingerprint:
            if stored.get("request_fingerprint", actual) != actual:
                raise ReplayEvidenceError(candidate["call_id"], "request_fingerprint_mismatch")
            selected = candidate
            break
    if selected is None:
        return None
    raw = connection.execute("SELECT response_json FROM editorial_model_calls WHERE call_id=?", (selected["call_id"],)).fetchone()[0]
    try:
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise TypeError()
    except (ValueError, TypeError):
        raise ReplayEvidenceError(selected["call_id"], "response_json_invalid") from None
    return {"call_id": selected["call_id"], "response": response}


def integrity_report(connection, *, limit=100):
    """Explicit diagnostic scan, separate from a hot-path reuse lookup."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("call integrity limit must be between 1 and 1000")
    rows = connection.execute(
        "SELECT call_id FROM editorial_model_calls WHERE NOT json_valid(request_json) "
        "OR (response_json IS NOT NULL AND NOT json_valid(response_json)) ORDER BY rowid LIMIT ?", (limit + 1,)).fetchall()
    return {"policy": POLICY, "invalid_json_call_ids": [row[0] for row in rows[:limit]], "truncated": len(rows) > limit}
