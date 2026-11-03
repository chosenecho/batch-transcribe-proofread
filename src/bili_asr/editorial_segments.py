"""Pure transcript segment and language identity helpers."""
from __future__ import annotations

from typing import Any


def segments(record: Any) -> list[dict[str, Any]]:
    return [
        {"segment_id": f"t{record.transcript_id}:s{i}", "start_ms": s.start_ms,
         "end_ms": s.end_ms, "text": s.text}
        for i, s in enumerate(record.segments)
    ]


def language_key(value: str) -> str:
    """Compare Qwen language names with Bilibili caption codes without changing provenance."""
    code = value.strip().casefold().replace("_", "-")
    if code.startswith("ai-"):
        code = code[3:]
    code = {"chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
            "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
            "portuguese": "pt", "italian": "it"}.get(code, code)
    if code in {"zh", "zh-cn", "zh-hans"}:
        return "zh-hans"
    if code in {"zh-tw", "zh-hk", "zh-mo", "zh-hant"}:
        return "zh-hant"
    return code.split("-", 1)[0]
