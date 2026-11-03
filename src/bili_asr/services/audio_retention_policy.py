"""Versioned audio policy defaults and explicit deletion authorization bounds."""
from __future__ import annotations

from dataclasses import asdict

from bili_asr.contracts.json_schema import validate_json
from bili_asr.services.artifact_inventory_service import ArtifactSelection


def default_audio_retention_policy():
    from bili_asr.services.artifact_policy_defaults import DEFAULT_POLICY
    selection = {key: list(values) for key, values in asdict(ArtifactSelection(kinds=("audio",))).items()}
    return {**DEFAULT_POLICY, "version": 2, "selection": selection,
        "allow_delete": False, "compression_level": 6, "minimum_verified_copies": 1,
        "replica_target_ids": [], "local_retention_seconds": 7 * 86400,
        "maximum_local_audio_bytes": 20 * 1024**3}


def validate_audio_retention_policy(value):
    validate_json("artifact-policy-v2", value)
    targets = [value["target_id"], *value["replica_target_ids"]]
    if len(set(targets)) != len(targets) or value["minimum_verified_copies"] > len(targets):
        raise ValueError("audio retention requires distinct configured replica targets")
    if value["local_retention_seconds"] < value["minimum_age_seconds"]:
        raise ValueError("local retention must cover the minimum completion age")
