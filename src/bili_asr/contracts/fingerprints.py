"""Pinned snapshot identities, separate from migration-source signatures."""
from importlib import resources
import json
from typing import Any


def database_fingerprint(kind: str, imports: bool = False, supplements: bool = False,
                         artifacts: bool | str = False, online: bool = False,
                         rights: bool = False, observations: bool = False, packages: bool | str = False, batches: bool = False) -> str:
    if any(type(flag) is not bool for flag in (imports, supplements, online, rights, observations, batches)):
        raise ValueError("snapshot extension flag must be boolean")
    if type(artifacts) is not bool and artifacts != "v2":
        raise ValueError("unsupported artifact storage contract")
    key = kind + ("+preserved-body-import-v1" if imports else "")
    if supplements:
        key += "+legacy-part-title-supplement-v1"
    if artifacts:
        key += "+artifact-storage-v2" if artifacts == "v2" else "+artifact-storage-v1"
    if online:
        key += "+artifact-online-v1"
    if rights:
        key += "+content-rights-v1"
    if observations:
        key += "+source-observations/v1"
    if type(packages) is not bool and packages != "embedded":
        raise ValueError("unsupported publication package contract")
    if packages:
        key += "+publication-packages/embedded-v0" if packages == "embedded" else "+publication-packages/v1"
    if batches:
        key += "+workflow-batches/v1"
    document: dict[str, Any] = json.loads(resources.files("bili_asr.contracts").joinpath(
        "database-fingerprints.json").read_text(encoding="utf-8"))
    try:
        fingerprint: str = document["contracts"][key]
        return fingerprint
    except KeyError as exc:
        raise ValueError(f"unsupported snapshot database contract: {key}") from exc
