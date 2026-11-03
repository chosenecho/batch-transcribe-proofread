"""Artifact transport/report structures have packaged offline contracts."""
from __future__ import annotations

from copy import deepcopy

import pytest

from bili_asr.artifact_packages import (
    PackageSource,
    capture_source_generation,
    package_manifest,
)
from bili_asr.contracts.json_schema import ContractValidationError, validate_json
from bili_asr.services.artifact_inventory_service import (
    inventory_artifacts,
    plan_artifact_copy,
    plan_artifact_offload,
)
from tests.test_artifact_transfer import archive as artifact_archive


@pytest.fixture
def archive(tmp_path):
    return artifact_archive.__wrapped__(tmp_path)


def documents(archive):
    inventory = inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True, external_holds={})
    plan = plan_artifact_offload(inventory, target_id="cold")
    path = archive.root / "audio/input.m4a"
    source = PackageSource(archive.digest, path, "audio/input.m4a", archive.digest,
                           len(archive.data), capture_source_generation(path))
    package = package_manifest((source,), "fixture", plan["plan_sha256"], 0)
    return {"artifact-inventory-v1": inventory, "artifact-offload-plan-v1": plan,
            "artifact-copy-plan-v1": plan_artifact_copy(inventory, target_id="cold"),
            "artifact-package-v1": package}


def test_actual_inventory_plan_and_package_validate_offline(archive):
    import json
    for identity, value in documents(archive).items():
        validate_json(identity, json.loads(json.dumps(value)))


@pytest.mark.parametrize("identity", ["artifact-inventory-v1", "artifact-offload-plan-v1", "artifact-copy-plan-v1", "artifact-package-v1"])
@pytest.mark.parametrize("change", ["unknown", "missing", "wrong_type"])
def test_artifact_schema_rejects_unknown_missing_and_bad_fields(archive, identity, change):
    value = deepcopy(documents(archive)[identity])
    if change == "unknown":
        value["unknown"] = True
    elif change == "missing":
        value.pop(next(iter(value)))
    elif identity == "artifact-inventory-v1":
        value["copies"][0]["candidate"] = "false"
    elif identity in {"artifact-offload-plan-v1", "artifact-copy-plan-v1"}:
        value["items"][0]["sha256"] += "\n"
    else:
        value["objects"][0]["sha256"] += "\n"
    with pytest.raises(ContractValidationError):
        validate_json(identity, value)
