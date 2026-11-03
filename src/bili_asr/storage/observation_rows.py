"""Validated raw observation storage without metadata-selection dependencies."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from bili_asr.canonical_json import canonical, digest
from bili_asr.source_observations import (
    CONTRACT,
    PlatformObservation,
    validate_observation,
)
from bili_asr.storage.schema_contract import (
    SchemaContractError,
    normalize_schema_sql,
    schema_resource,
)


@lru_cache(maxsize=1)
def observation_objects() -> dict[str, tuple[str, str]]:
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(schema_resource("schema-source-observations.sql"))
        return {row[0]: (row[1], row[2]) for row in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def require_observations(connection: sqlite3.Connection, *, required: bool = False) -> bool:
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = observation_objects()
    if not expected.keys() & actual.keys():
        if required:
            raise SchemaContractError(CONTRACT + ": explicit observation upgrade into an empty target is required")
        return False
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or normalize_schema_sql(current[1]) != normalize_schema_sql(sql):
            raise SchemaContractError(CONTRACT + ": missing or altered object: " + name)
    if [tuple(row) for row in connection.execute("SELECT singleton,version FROM platform_observation_contract")] != [(1, 1)]:
        raise SchemaContractError(CONTRACT + ": altered contract marker")
    return True


def install_observations_in_staged_copy(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise ValueError("observation installation requires a stopped staged copy")
    if require_observations(connection):
        raise ValueError("observation contract is already installed")
    connection.executescript(schema_resource("schema-source-observations.sql"))
    connection.commit()
    require_observations(connection, required=True)


def _validated_row(row: Any) -> PlatformObservation:
    item = dict(row) if hasattr(row, "keys") else dict(zip(
        ("observation_id", "platform", "operation", "external_video_id", "creator_id", "observed_at",
         "envelope_json", "envelope_sha256", "payload_json"), row, strict=True))
    observation = PlatformObservation(item["observation_id"], item["envelope_json"], item["payload_json"])
    validate_observation(observation)
    envelope = observation.envelope
    if (canonical(envelope) != item["envelope_json"] or digest(envelope) != item["envelope_sha256"]
            or envelope["version"] != 1 or envelope["adapterVersion"] != CONTRACT
            or envelope["platform"] != item["platform"] or envelope["operation"] != item["operation"]
            or envelope["observedAt"] != item["observed_at"]
            or envelope["scope"].get("externalVideoId") != item["external_video_id"]
            or envelope["scope"].get("creatorId") != item["creator_id"]
            or envelope["payloadSha256"] != (None if item["payload_json"] is None else
                hashlib.sha256(item["payload_json"].encode("utf-8")).hexdigest())):
        raise ValueError("platform observation identity or digest differs")
    return observation


def append_observations(connection: sqlite3.Connection, observations: Iterable[PlatformObservation]) -> None:
    require_observations(connection, required=True)
    for observation in observations:
        envelope = observation.envelope
        item = {"observation_id": observation.observation_id, "platform": envelope["platform"],
                "operation": envelope["operation"], "external_video_id": envelope["scope"].get("externalVideoId"),
                "creator_id": envelope["scope"].get("creatorId"), "observed_at": envelope["observedAt"],
                "envelope_json": observation.envelope_json, "envelope_sha256": digest(envelope),
                "payload_json": observation.payload_json}
        _validated_row(item)
        existing = connection.execute("SELECT envelope_json,payload_json FROM platform_observations WHERE observation_id=?",
                                           (observation.observation_id,)).fetchone()
        if existing is not None:
            if tuple(existing) != (observation.envelope_json, observation.payload_json):
                raise ValueError("raw observation ID already has different evidence")
            continue
        connection.execute("INSERT INTO platform_observations VALUES (?,?,?,?,?,?,?,?,?)", tuple(item.values()))

