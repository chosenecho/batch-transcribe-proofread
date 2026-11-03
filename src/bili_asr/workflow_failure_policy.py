"""Pure failure taxonomy; persisted historical codes are never rewritten."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

POLICY_VERSION = "workflow-failure-policy/v1"


@dataclass(frozen=True)
class FailurePolicy:
    category: str
    retry_policy: str
    recommended_action: str
    automatic_retry: bool = False
    max_attempts: int = 3
    backoff_seconds: tuple[int, ...] = (0, 600, 1800)

    def as_dict(self) -> dict:
        return {"policy_version": POLICY_VERSION, **asdict(self)}


_TRANSIENT = FailurePolicy("transient", "automatic", "retry_with_backoff", True)
_CONFIG = FailurePolicy("configuration", "blocked", "restore_configuration_then_retry")
_BILLING = FailurePolicy("billing", "blocked", "restore_balance_then_retry")
_SOURCE = FailurePolicy("source_quality", "repair", "repair_source_then_replace_job")
_REVISION = FailurePolicy("revision_validation", "manual", "inspect_revision_or_repair_proofread")
_CONTRACT = FailurePolicy("publication_contract", "repair", "repair_data_then_retry")
_ARTIFACT = FailurePolicy("artifact_unavailable", "blocked", "restore_artifacts_then_retry")
_UNKNOWN = FailurePolicy("unknown", "unknown", "inspect_failure")

_POLICIES = {
    "TimeoutError": _TRANSIENT,
    "TimeoutException": _TRANSIENT,
    "ConnectError": _TRANSIENT,
    "ConnectTimeout": _TRANSIENT,
    "ReadTimeout": _TRANSIENT,
    "WriteTimeout": _TRANSIENT,
    "PoolTimeout": _TRANSIENT,
    "NetworkError": _TRANSIENT,
    "RemoteProtocolError": _TRANSIENT,
    "DeepSeekTransportError": _TRANSIENT,
    "deepseek_transport_failed": _TRANSIENT,
    "deepseek_transport": _TRANSIENT,
    "transport_error": _TRANSIENT,
    "transport_failed": _TRANSIENT,
    "subtitle_candidate_timeout": _TRANSIENT,
    "youtube_timeout": _TRANSIENT,
    "deepseek_missing_api_key": _CONFIG,
    "auth_error": _CONFIG,
    "auth_failed": _CONFIG,
    "youtube_access_unverified": _CONFIG,
    "youtube_js_unavailable": _CONFIG,
    "editorial_source_repetition": _SOURCE,
    "asr_quality_unusable": _SOURCE,
    "asr_quality_unknown": FailurePolicy("source_quality", "repair", "verify_original_audio_and_asr_evidence"),
    "RevisionValidationError": _REVISION,
    "deepseek_non_json_response": _REVISION,
    "deepseek_invalid_envelope": _REVISION,
    "deepseek_output_truncated": _REVISION,
    "deepseek_content_filtered": FailurePolicy("provider_filter", "blocked", "review_provider_filter", max_attempts=1),
    "deepseek_unsupported_completion": FailurePolicy("provider_protocol", "blocked", "inspect_provider_completion", max_attempts=1),
    "deepseek_output_interrupted": FailurePolicy("provider_protocol", "blocked", "inspect_provider_termination", max_attempts=1),
    "deepseek_empty_output": _REVISION,
    "deepseek_invalid_revision_json": _REVISION,
    "subtitle_body_unavailable": _SOURCE,
    "subtitle_timeline_invalid": _SOURCE,
    "subtitle_empty": _SOURCE,
    "subtitle_candidate_budget_exhausted": _SOURCE,
    "subtitle_failed": _SOURCE,
    "youtube_caption_body_unavailable": _SOURCE,
    "publication_contract_failed": _CONTRACT,
    "webvtt_unrepresentable": _CONTRACT,
    "cue_timeline_invalid": _CONTRACT,
    "artifact_input_unavailable": _ARTIFACT,
    "artifact_input_identity_mismatch": _ARTIFACT,
    "audio_missing": _ARTIFACT,
    "audio_digest_mismatch": _ARTIFACT,
    "audio_generation_changed": _ARTIFACT,
    "audio_input_space_unavailable": _ARTIFACT,
    "inference_timeout": FailurePolicy("inference_runtime", "manual", "inspect_inference_deadline_or_replace_profile"),
    "inference_session_failed": FailurePolicy("inference_runtime", "manual", "inspect_inference_runtime"),
    "ASRDependencyError": _CONFIG,
    "ASRRuntimeCompatibilityError": _CONFIG,
    "ASRModelError": FailurePolicy("inference_runtime", "blocked", "verify_model_configuration"),
    "AudioDecodeError": FailurePolicy("source_quality", "repair", "verify_audio_decode"),
    "RuntimeCacheError": _CONFIG,
    "lease_expired": FailurePolicy("lease_recovery", "blocked", "inspect_lease_recovery"),
    "migration_interrupted": FailurePolicy("lease_recovery", "blocked", "inspect_migration_recovery"),
    "rate_limited": FailurePolicy("source_rate_limit", "manual", "inspect_source_rate_limit"),
    "not_found": FailurePolicy("source_unavailable", "blocked", "inspect_source_availability"),
    "shape_error": _SOURCE,
    "response_error": FailurePolicy("source_response", "manual", "inspect_source_response"),
    "gateway_error": FailurePolicy("source_response", "manual", "inspect_source_response"),
    "request_timeout": _TRANSIENT,
    "request_budget_exhausted": FailurePolicy("source_rate_limit", "manual", "wait_for_request_budget"),
    "invalid_response": FailurePolicy("remote_response", "manual", "inspect_remote_response"),
    "model_identity_unverified": _CONFIG,
    "worker_drain_timeout": FailurePolicy("worker_lifecycle", "blocked", "inspect_worker_shutdown"),
    "lease_renewal_failed": FailurePolicy("lease_recovery", "blocked", "inspect_worker_lease"),
    "metadata_owner_conflict": FailurePolicy("metadata_conflict", "blocked", "inspect_metadata_owner"),
    "metadata_part_identity_conflict": FailurePolicy("metadata_conflict", "blocked", "inspect_metadata_identity"),
    "metadata_part_topology_changed": FailurePolicy("metadata_conflict", "repair", "re-enumerate_source_metadata"),
    "metadata_resume_requires_reenumeration": FailurePolicy("metadata_conflict", "repair", "re-enumerate_source_metadata"),
    "restore_unavailable": _ARTIFACT,
    "retention_guard": FailurePolicy("artifact_retention", "blocked", "inspect_retention_guard"),
    "transfer_interrupted": FailurePolicy("artifact_transfer", "manual", "resume_artifact_transfer"),
}

# These named source contracts describe unusable/unsupported source input, not
# transient network failures. No exception-text inference is needed.
for _code in (
    "youtube_caption_shape", "youtube_caption_timeline", "youtube_output_budget",
    "youtube_metadata_shape", "youtube_identity_mismatch", "youtube_live_unsupported",
    "youtube_duration_invalid", "youtube_caption_inventory_shape",
    "youtube_caption_format_unsupported", "youtube_caption_unlisted",
    "youtube_caption_output_budget", "youtube_audio_output_shape", "youtube_audio_output_path",
    "youtube_caption_translation_only", "youtube_caption_candidate_budget",
):
    _POLICIES[_code] = _SOURCE
for _code in ("youtube_unavailable", "youtube_extraction_failed"):
    _POLICIES[_code] = FailurePolicy("source_unavailable", "blocked", "inspect_source_availability")


def failure_policy(error_code: str | None) -> FailurePolicy:
    """Only explicitly known transient failures permit automatic retry.

    Generic exceptions (including old DeepSeekError) lack sufficient evidence.
    HTTP aliases are classified using their numeric status, never message text.
    """
    if isinstance(error_code, str):
        match = re.fullmatch(r"(?:deepseek|provider)_http_([1-5][0-9]{2})", error_code)
        if match:
            status = int(match[1])
            if status == 402:
                return _BILLING
            if status in (401, 403):
                return _CONFIG
            if status in (408, 429) or 500 <= status <= 599:
                return _TRANSIENT
            return FailurePolicy("provider_request", "blocked", "inspect_provider_configuration")
    return _POLICIES.get(error_code, _UNKNOWN)
