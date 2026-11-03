"""Adapt reviewed domain failures before the generic safe exception fallback."""

import re

from bili_asr.exception_diagnostics import capture_failure
from bili_asr.workflow_errors import safe_job_details

_PROVIDER_CODES = frozenset({"DeepSeekError", "deepseek_missing_api_key", "deepseek_transport_failed",
    "deepseek_non_json_response", "deepseek_invalid_envelope", "deepseek_invalid_revision_json",
    "deepseek_output_truncated", "deepseek_empty_output", "deepseek_content_filtered",
    "deepseek_unsupported_completion", "deepseek_output_interrupted"})


def execution_failure(exc: Exception) -> tuple[str, dict]:
    """Keep known retry/hold semantics without trusting arbitrary error_code attributes."""
    from bili_asr.asr.runner import ASRInferenceTimeoutError
    from bili_asr.asr.session import InferenceSessionError
    from bili_asr.deepseek import DeepSeekError
    from bili_asr.editorial import RevisionValidationError
    from bili_asr.editorial_budget import BudgetExceeded
    from bili_asr.runtime_bindings import ModelBindingError

    diagnostic = capture_failure(exc)
    for kind, code in ((ASRInferenceTimeoutError, "inference_timeout"),
                       (InferenceSessionError, "inference_session_failed"),
                       (ModelBindingError, "model_identity_unverified"),
                       (BudgetExceeded, "editorial_budget_exceeded"),
                       (RevisionValidationError, "RevisionValidationError")):
        if isinstance(exc, kind):
            if isinstance(exc, (BudgetExceeded, RevisionValidationError)):
                diagnostic = safe_job_details(exc.safe_details)
            return code, diagnostic
    if isinstance(exc, DeepSeekError):
        code = exc.error_code
        if type(code) is not str or not (code in _PROVIDER_CODES or re.fullmatch(r"(?:deepseek|provider)_http_[1-5][0-9]{2}", code)):
            code = "DeepSeekError"
        return code, safe_job_details(exc.safe_details)
    return diagnostic.get("exception_type", "Exception"), diagnostic
