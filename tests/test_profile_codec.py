"""Persisted identities are independent of runtime tuning defaults."""
from dataclasses import asdict, fields, replace
import hashlib
import json

import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.profile_codec import decode_profile, encode_profile
from bili_asr.workflow_models import AsrProfile


@pytest.mark.parametrize("extension,expected", [
    ({}, "0ddbfafe9b9ae3ddabb4f1f80a219392732ad1e72855cba888eb25eb66b27416"),
    ({"model_dtype": "float16", "asr_batch_size": 2, "asr_cache_implementation": "static", "asr_compile": True},
     "853400f25caa3a046f7e7a0d871f8f47b487eecc0bb5a2a783d823d97acb7a97"),
])
def test_frozen_v2_digest_and_extensions_roundtrip(extension, expected):
    # Values fixed from the historical v2 encoder at 98904edbb01f.
    profile = replace(AsrProfile("frozen", "Qwen/Qwen3-ASR-1.7B-hf"), **extension)
    encoded = profile.canonical()
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == expected
    restored = AsrProfile(profile_key=profile.profile_key, **decode_profile(encoded))
    assert restored == profile
    assert encode_profile(asdict(restored)) == encoded


def test_runtime_default_mutation_cannot_change_persisted_identity(monkeypatch):
    from bili_asr.asr.strategies import STRATEGY_DEFAULTS
    profile = AsrProfile("frozen", "offline")
    before = profile.canonical()
    monkeypatch.setitem(STRATEGY_DEFAULTS, "asr_attention", "sdpa")
    assert profile.canonical() == before
    assert "runtime_strategies" not in json.loads(before)


@pytest.mark.parametrize("version", [2, True, "1"])
def test_extension_schema_versions_are_exact(version):
    profile = replace(AsrProfile("frozen", "offline"), model_dtype="float16")
    value = json.loads(profile.canonical())
    value["precision"]["schema_version"] = version
    with pytest.raises(ValueError, match="precision schema"):
        decode_profile(json.dumps(value))


def test_runtime_and_frozen_fields_have_explicit_complete_mapping():
    frozen = {field.name for field in fields(AsrProfile)} - {"profile_key"}
    runtime = {field.name for field in fields(ASRConfig)} - {"local_source"}
    assert frozen == runtime
    profile = replace(AsrProfile("all", "offline", device="cpu"), hotwords=("synthetic",),
                      chunk_seconds=17, min_new_tokens=12, model_dtype="float16", aligner_batch_size=2,
                      asr_attention="eager", compile_max_buckets=3)
    config = asdict(profile.asr_config())
    config.pop("local_source")
    expected = asdict(profile)
    expected.pop("profile_key")
    expected["model_revision"] = None
    assert config == expected
