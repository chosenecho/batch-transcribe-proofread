"""DeepSeek Flash JSON adapter. No durable state or implicit HTTP retries."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import requests

from bili_asr.editorial_input_contract import EditorialConfig
from bili_asr.editorial_provider import editorial_provider
from bili_asr.editorial_request import request_body
from bili_asr.workflow_errors import JobExecutionError

__all__ = [
    "DeepSeekClient",
    "DeepSeekError",
    "DeepSeekTransportError",
    "normalize_usage",
    "parse_response",
    "request_body",
    "usage_metrics",
]


class DeepSeekError(RuntimeError):
    """Provider failure with a stable code and bounded, reviewed diagnostics.

    The default code preserves compatibility for external adapters that raise
    this exception without an explicit classification.
    """

    def __init__(self, message: str, *, error_code: str = "DeepSeekError",
                 stage: str = "provider_request", reason_code: str = "unclassified_provider_error",
                 **details: Any):
        classified = JobExecutionError(error_code, dict(stage=stage, reason_code=reason_code, **details))
        self.error_code, self.safe_details = classified.error_code, classified.safe_details
        super().__init__(message)


class DeepSeekTransportError(DeepSeekError):
    """A request transport failure, distinct from payment/protocol failures."""


def _nonnegative_int(value: Any) -> int | None:
    """Return provider counters only when they are safe integer values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def normalize_usage(envelope: dict[str, Any], *, latency_ms: int | None = None) -> dict[str, Any]:
    """Normalize DeepSeek response usage without discarding the raw envelope.

    The provider has added fields over time (notably cache counters and
    completion-token details), so callers should persist this compact summary
    alongside the original response and tolerate missing counters.
    """
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    cached_tokens = _nonnegative_int(prompt_details.get("cached_tokens"))
    cache_hit = _nonnegative_int(usage.get("prompt_cache_hit_tokens"))
    summary: dict[str, Any] = {
        "provider_request_id": envelope.get("id") if isinstance(envelope.get("id"), str) else None,
        "model": envelope.get("model") if isinstance(envelope.get("model"), str) else None,
        "prompt_tokens": _nonnegative_int(usage.get("prompt_tokens")),
        "completion_tokens": _nonnegative_int(usage.get("completion_tokens")),
        "total_tokens": _nonnegative_int(usage.get("total_tokens")),
        "cached_tokens": cached_tokens,
        "prompt_cache_hit_tokens": cache_hit if cache_hit is not None else cached_tokens,
        "prompt_cache_miss_tokens": _nonnegative_int(usage.get("prompt_cache_miss_tokens")),
        "reasoning_tokens": _nonnegative_int(details.get("reasoning_tokens")),
    }
    if latency_ms is not None:
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        summary["latency_ms"] = latency_ms
    return summary


# Short alias for callers that use the provider-neutral term.
usage_metrics = normalize_usage


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, *, session: requests.Session | None = None):
        # OpenCode Go keys are separate from direct DeepSeek keys. Keep the
        # explicit argument first, then select the key by endpoint.
        self.api_key = api_key
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.opencode_session = str(uuid.uuid4())

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def complete(self, request: dict[str, Any], config: EditorialConfig) -> dict[str, Any]:
        policy = editorial_provider(config.base_url)
        env_name = policy.credential_name
        api_key = self.api_key if self.api_key is not None else os.environ.get(env_name, "")
        if not api_key.strip():
            raise DeepSeekError(f"{env_name} is not configured", error_code="deepseek_missing_api_key",
                                reason_code="missing_api_key")
        headers = {
            "Authorization": "Bearer " + api_key,
            "User-Agent": "bilibili-asr-archive/1.0",
            "Content-Type": "application/json",
        }
        if policy.opencode_session:
            headers["x-opencode-session"] = self.opencode_session
        try:
            response = self.session.post(
                config.base_url.rstrip("/") + "/chat/completions", json=request,
                headers=headers,
                timeout=(30, config.timeout_seconds), allow_redirects=False)
        except requests.RequestException:
            # Exception strings can include request details. Keep them out of
            # logs, durable attempt records and CLI diagnostics.
            raise DeepSeekTransportError("DeepSeek transport failed", error_code="deepseek_transport_failed",
                                         reason_code="transport_failure") from None
        if response.status_code != 200:
            raise DeepSeekError(f"DeepSeek HTTP {response.status_code}",
                                error_code=f"deepseek_http_{response.status_code}",
                                reason_code="http_status", http_status=response.status_code)
        try:
            value = response.json()
        except ValueError:
            raise DeepSeekError("DeepSeek returned non-JSON HTTP content", error_code="deepseek_non_json_response",
                                reason_code="non_json_response") from None
        if not isinstance(value, dict):
            raise DeepSeekError("DeepSeek returned an invalid envelope", error_code="deepseek_invalid_envelope",
                                reason_code="invalid_envelope")
        return value


def parse_response(envelope: dict[str, Any]) -> Any:
    try:
        choice = envelope["choices"][0]
        if not isinstance(choice, dict):
            raise ValueError("invalid choice")
        # Provider strings are untrusted. Only this finite vocabulary may cross
        # the safe diagnostic boundary; preserve the original in its envelope.
        raw_reason = choice.get("finish_reason")
        reason = (raw_reason if isinstance(raw_reason, str) and raw_reason in {
            "stop", "length", "content_filter", "tool_calls", "function_call"}
            else "missing" if raw_reason is None else "unknown")
        usage = envelope.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        numbers = {"prompt_tokens": usage.get("prompt_tokens"),
                   "completion_tokens": usage.get("completion_tokens"),
                   "reasoning_tokens": details.get("reasoning_tokens")}
        diagnosis = {key: value for key, value in numbers.items()
                     if type(value) is int and 0 <= value < 2**63}
        diagnosis.update(finish_reason=reason, length_exceeded=reason == "length")
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        for name, field in (("output_chars", "content"), ("reasoning_chars", "reasoning_content")):
            value = message.get(field)
            diagnosis[name] = len(value) if isinstance(value, str) else 0
        if reason == "length":
            raise DeepSeekError("DeepSeek output is truncated or unfinished", error_code="deepseek_output_truncated",
                                stage="response_parse", reason_code="unfinished_output", **diagnosis)
        if reason == "content_filter":
            raise DeepSeekError("DeepSeek declined the output", error_code="deepseek_content_filtered",
                                stage="response_parse", reason_code="content_filtered", **diagnosis)
        if reason in {"tool_calls", "function_call"}:
            raise DeepSeekError("DeepSeek returned an unsupported completion", error_code="deepseek_unsupported_completion",
                                stage="response_parse", reason_code="unsupported_finish_reason", **diagnosis)
        if reason != "stop":
            raise DeepSeekError("DeepSeek completion did not finish", error_code="deepseek_output_interrupted",
                                stage="response_parse", reason_code=reason + "_finish_reason", **diagnosis)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("DeepSeek output is empty", error_code="deepseek_empty_output",
                                stage="response_parse", reason_code="empty_output", **diagnosis)
    except (KeyError, IndexError, TypeError, ValueError):
        raise DeepSeekError("DeepSeek output is not a valid JSON revision", error_code="deepseek_invalid_envelope",
                            stage="response_parse", reason_code="invalid_envelope") from None

    try:
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result

        return json.loads(content, object_pairs_hook=object_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except ValueError:
        raise DeepSeekError("DeepSeek output is not a valid JSON revision", error_code="deepseek_invalid_revision_json",
                            stage="response_parse", reason_code="invalid_revision_json") from None
