"""The v2 policy is independent of stored-only v1 and defaults to no deletion."""
from __future__ import annotations

from copy import deepcopy

import pytest

from bili_asr.contracts.json_schema import validate_json
from bili_asr.services.audio_retention_policy import (
    default_audio_retention_policy,
    validate_audio_retention_policy,
)


def test_audio_policy_default_is_off_and_has_no_delete_authorization():
    policy = default_audio_retention_policy()
    validate_audio_retention_policy(deepcopy(policy))
    validate_json("artifact-policy-v2", deepcopy(policy))
    assert policy["version"] == 2 and policy["mode"] == "off"
    assert policy["allow_delete"] is False
    assert policy["selection"]["kinds"] == ["audio"]
    assert policy["minimum_verified_copies"] > 0


@pytest.mark.parametrize("changes", [{"allow_delete": "true"}, {"minimum_verified_copies": 0},
    {"minimum_verified_copies": 2}, {"replica_target_ids": ["cold"]}, {"max_concurrency": 2},
    {"minimum_age_seconds": 1}, {"selection": {"kinds": ["document"]}}, {"unknown": True}])
def test_audio_policy_rejects_unsafe_or_ambiguous_values(changes):
    import json
    config = default_audio_retention_policy()
    config.update(target_id="cold", minimum_age_seconds=0, local_retention_seconds=0)
    config.update(changes)
    with pytest.raises(ValueError):
        validate_audio_retention_policy(json.loads(json.dumps(config)))
