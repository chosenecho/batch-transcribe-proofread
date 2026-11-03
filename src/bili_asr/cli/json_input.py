"""Strict JSON-object inputs shared by CLI commands."""
import json
from pathlib import Path


def _json_object(path: str) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate metadata key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    result = json.loads(Path(path).read_text(encoding="utf-8"),
                        object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("metadata file must contain a JSON object")
    return result
