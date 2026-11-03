"""Versioned policy validation and reading without executing maintenance."""
from __future__ import annotations

import json

from bili_asr.services.artifact_policy_defaults import DEFAULT_POLICY
from bili_asr.storage.artifact_online import require_artifact_online


def validate_policy(value):
    from bili_asr.contracts.json_schema import validate_json
    try:
        value = json.loads(json.dumps(value))
    except (TypeError, ValueError) as error:
        raise ValueError("artifact policy must be a JSON value") from error
    if isinstance(value, dict) and value.get("version") == 2:
        from bili_asr.services.audio_retention_policy import (
            validate_audio_retention_policy,
        )
        validate_audio_retention_policy(value)
    else:
        validate_json("artifact-policy-v1", value)
    if (value["backoff_seconds"] < value["scan_interval_seconds"]
            or value["trigger_free_bytes"] >= value["stop_free_bytes"]
            or value["minimum_free_bytes"] > value["trigger_free_bytes"]):
        raise ValueError("invalid artifact policy bounds or watermarks")
    if value["mode"] != "off" and value["target_id"] is None:
        raise ValueError("enabled artifact policy requires a target identity")
    return value


def read_policy(connection, policy_id="default", *, default_version=1):
    require_artifact_online(connection, required=True)
    row = connection.execute("SELECT config_json FROM artifact_policies WHERE policy_id=?", (policy_id,)).fetchone()
    if row:
        return validate_policy(json.loads(row[0]))
    if default_version == 2:
        from bili_asr.services.audio_retention_policy import (
            default_audio_retention_policy,
        )
        return validate_policy(default_audio_retention_policy())
    return validate_policy(DEFAULT_POLICY)
