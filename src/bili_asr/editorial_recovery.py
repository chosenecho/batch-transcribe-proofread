"""Versioned, bounded regeneration policy; source snapshots remain immutable."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bili_asr.canonical_json import canonical
from bili_asr.editorial import EditorialConfig, RevisionValidationError
from bili_asr.editorial_budget import admit_request
from bili_asr.workflow_errors import JobExecutionError

RECOVERY_POLICY = "paragraph-reference-v1"


def recoverable(details: dict[str, Any] | None) -> bool:
    return isinstance(details, dict) and details.get("reason") == "cross_paragraph_reference"


def diagnostic_for_error(error: Exception) -> dict[str, Any] | None:
    if not isinstance(error, RevisionValidationError):
        return None
    details = getattr(error, "safe_details", None)
    return dict(details) if recoverable(details) else None


def provider_recovery_blocker(previous: dict[str, Any] | None) -> JobExecutionError | None:
    """Saved filter/protocol terminations never authorize another paid send.

    Historical generic errors stay untouched: this is explicitly a derived
    diagnosis over their preserved envelope, shared by execution and status.
    """
    if previous is None or previous.get("response") is None:
        return None
    from bili_asr.deepseek import DeepSeekError, parse_response
    try:
        parse_response(previous["response"])
    except DeepSeekError as error:
        if error.error_code in {"deepseek_content_filtered", "deepseek_unsupported_completion",
                                "deepseek_output_interrupted"}:
            return JobExecutionError(error.error_code, dict(error.safe_details,
                diagnostic_source="stored_response_replay", previous_call_id=previous["call_id"]))
    return None


def recovery_request(original: dict[str, Any], previous: dict[str, Any] | None,
                     config: EditorialConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct the next request from durable evidence after a restart."""
    metadata: dict[str, Any] = {"policy": RECOVERY_POLICY,
                              "max_physical_calls": 1 + config.max_chunk_regenerations,
                              "regeneration": 0}
    if previous is None:
        return admit_request(original, config), metadata
    stored = previous["stored_request"]
    prior_metadata = stored.get("recovery", {})
    diagnostic = stored.get("validation_diagnostic")
    if recoverable(diagnostic) and config.max_chunk_regenerations:
        ordinal = prior_metadata.get("regeneration", 0) + 1
        metadata.update(regeneration=ordinal, previous_call_id=previous["call_id"],
                        diagnostic=diagnostic)
        request = deepcopy(original)
        request["messages"].append({"role": "user", "content": canonical({
            "recovery_policy": RECOVERY_POLICY, "regeneration": ordinal,
            "validation_diagnostic": diagnostic,
            "instruction": "重新生成整个 editable chunk，保留精确覆盖和顺序。每段 evidence_refs 只能引用本段 allowed_issue_refs 的并集；无合法证据时用空列表。继续遵守原始所有规则。"})})
        return admit_request(request, config), metadata
    # An uncertain send or transport error consumed its reservation. Retry its
    # exact request (if budget remains), without inventing another diagnostic.
    resumed = dict(prior_metadata or metadata)
    resumed.update(policy=RECOVERY_POLICY, max_physical_calls=1 + config.max_chunk_regenerations)
    return admit_request(stored["request"], config), resumed


def exhausted(chunk_id: str, consumed: int, maximum: int) -> JobExecutionError:
    return JobExecutionError("EditorialRecoveryExhausted", {
        "chunk_id": chunk_id, "physical_calls": consumed,
        "max_physical_calls": maximum, "recovery_policy": RECOVERY_POLICY})
