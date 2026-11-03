"""Pure request serialization shared by planning and provider transport."""
from __future__ import annotations

from typing import Any

from bili_asr.canonical_json import canonical
from bili_asr.editorial_input_contract import SYSTEM_PROMPT, EditorialConfig


def request_body(chunk: dict[str, Any], config: EditorialConfig, *, system_prompt: str = SYSTEM_PROMPT) -> dict[str, Any]:
    thinking = {"type": config.thinking_mode}
    if config.thinking_mode == "enabled" and config.request_policy == "legacy-v1":
        thinking["reasoning_effort"] = config.reasoning_effort
    body = {"model": config.model, "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": canonical(chunk)}],
        "response_format": {"type": "json_object"}, "stream": False,
        "thinking": thinking,
        "top_p": config.top_p,
        "max_tokens": config.max_output_tokens}
    if config.request_policy == "deepseek-v2":
        if config.thinking_mode == "enabled":
            body["reasoning_effort"] = config.reasoning_effort
        else:
            # Non-thinking DeepSeek ignores top_p: freeze and send its effective
            # value explicitly. OpenCode receives the same compatibility shape;
            # this does not assert that the proxy honors these controls.
            body["top_p"] = 1
            if config.temperature is not None:
                body["temperature"] = config.temperature
    return body


