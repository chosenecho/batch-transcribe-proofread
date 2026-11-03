"""Read projections of immutable editorial identities, without repository orchestration."""
from __future__ import annotations
import json
import sqlite3
from typing import Any
from bili_asr.canonical_json import digest
from bili_asr.storage.archive_contracts import frozen_version

def template_for_input(connection: sqlite3.Connection, input_id: str) -> str:
    """Resolve rendering from the persisted input contract, including migrated v1."""
    return "ai-draft-v2" if frozen_version(connection, "input", input_id) == 2 else "ai-draft-v1"


def input_for_proofread(connection: sqlite3.Connection, job_id: str) -> str | None:
    row = connection.execute("SELECT input_id FROM editorial_job_inputs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        row = connection.execute("SELECT input_id FROM editorial_revisions WHERE job_id=?", (job_id,)).fetchone()
    if row is not None:
        return str(row[0])
    row = connection.execute("SELECT payload_json FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
    return None if row is None else json.loads(row[0]).get("input_id")


def load_input(connection, input_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT prepared_json FROM editorial_inputs WHERE input_id = ?", (input_id,)).fetchone()
    if row is None:
        raise ValueError("unknown editorial input")
    frozen_version(connection, "input", input_id)
    try:
        prepared = json.loads(row["prepared_json"])
        if prepared["input_id"] != input_id or digest(prepared["snapshot"]) != input_id:
            raise ValueError("editorial input identity mismatch")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid frozen editorial input") from exc
    from bili_asr.editorial_budget import require_frozen_plan
    require_frozen_plan(prepared)
    return prepared


def revision(connection, revision_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = connection.execute("SELECT * FROM editorial_revisions WHERE revision_id = ?", (revision_id,)).fetchone()
    if row is None:
        raise ValueError("unknown editorial revision")
    prepared = load_input(connection, row["input_id"])
    try:
        blocks = json.loads(row["blocks_json"])
        if digest({"input_id": row["input_id"], "blocks": blocks}) != revision_id:
            raise ValueError("editorial revision identity mismatch")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid frozen editorial revision") from exc
    return prepared, blocks
