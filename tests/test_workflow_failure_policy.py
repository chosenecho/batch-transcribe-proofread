"""Decisions use bounded codes, never historical exception-message guesses."""

import pytest

from bili_asr.workflow_failure_policy import failure_policy


@pytest.mark.parametrize("code", ["deepseek_http_429", "provider_http_503", "deepseek_transport_failed", "transport_error"])
def test_known_transient_failures_have_finite_budget(code):
    policy = failure_policy(code)
    assert policy.automatic_retry
    assert policy.max_attempts == 3
    assert policy.backoff_seconds == (0, 600, 1800)


@pytest.mark.parametrize("code,category", [
    ("deepseek_http_402", "billing"), ("provider_http_401", "configuration"),
    ("editorial_source_repetition", "source_quality"), ("subtitle_body_unavailable", "source_quality"),
    ("RevisionValidationError", "revision_validation"), ("publication_contract_failed", "publication_contract"),
    ("DeepSeekError", "unknown"), ("ValueError", "unknown"), (None, "unknown"),
    ("deepseek_http_402:balance", "unknown"), ("provider_http_404", "provider_request"),
])
def test_nontransient_and_legacy_codes_never_automatically_retry(code, category):
    policy = failure_policy(code)
    assert not policy.automatic_retry
    assert policy.category == category


def test_classification_does_not_change_original_code():
    code = "deepseek_http_402"
    assert "error_code" not in failure_policy(code).as_dict()
    assert code == "deepseek_http_402"


def test_installed_asr_runtime_failure_requires_configuration_repair():
    policy = failure_policy("ASRRuntimeCompatibilityError")
    assert policy.category == "configuration"
    assert policy.retry_policy == "blocked"
    assert policy.recommended_action == "restore_configuration_then_retry"
    assert not policy.automatic_retry


@pytest.mark.parametrize("code", ["audio_generation_changed", "audio_input_space_unavailable"])
def test_audio_input_integrity_and_capacity_failures_require_explicit_repair(code):
    policy = failure_policy(code)
    assert policy.category == "artifact_unavailable"
    assert policy.recommended_action == "restore_artifacts_then_retry"
    assert not policy.automatic_retry
