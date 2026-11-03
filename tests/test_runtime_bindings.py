"""Relocation must preserve model identity and verify all checkpoint bytes."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json

import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.runtime_bindings import ModelBindingError, load_runtime_bindings


def bind(tmp_path, *, revision="a" * 40):
    entries = []
    for name, logical in (("model", "Qwen/test-asr"), ("aligner", "Qwen/test-aligner")):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "config.json").write_text("{}")
        (directory / "weights.safetensors").write_bytes(name.encode())
        files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}
        manifest = directory / "model-manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "model_id": logical, "revision": revision, "files": files}))
        entries.append({"original": logical, "path": name, "model_id": logical, "revision": revision,
                        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()})
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"schema_version": 1, "bindings": [{"model": entries[0], "aligner": entries[1]}]}))
    config = ASRConfig("Qwen/test-asr", aligner_name="Qwen/test-aligner", model_revision=revision, aligner_revision=revision, device="cpu")
    return load_runtime_bindings(path), config, path


def test_verified_relocation_preserves_profile_and_only_exposes_logical_identity(tmp_path):
    bindings, config, _path = bind(tmp_path)
    resolved, identity = bindings.resolve(config)
    assert resolved.model_name == str(tmp_path / "model")
    assert resolved.device == config.device and resolved.offline == config.offline
    assert config.model_name == "Qwen/test-asr"
    assert str(tmp_path) not in json.dumps(identity)
    assert bindings.resolve(config)[1] == identity


def test_changed_weight_invalidates_verified_cache(tmp_path):
    bindings, config, _path = bind(tmp_path)
    bindings.resolve(config)
    (tmp_path / "model" / "weights.safetensors").write_bytes(b"changed-weight")
    with pytest.raises(ModelBindingError, match="hash mismatch"):
        bindings.resolve(config)


@pytest.mark.parametrize("revision", [None, "main", "b" * 40])
def test_unpinned_or_different_frozen_revision_cannot_bind(tmp_path, revision):
    bindings, config, _path = bind(tmp_path)
    with pytest.raises(ModelBindingError, match="exact revision"):
        bindings.resolve(replace(config, model_revision=revision))


def test_unlisted_nested_manifest_or_file_is_rejected(tmp_path):
    bindings, config, _path = bind(tmp_path)
    nested = tmp_path / "model" / "nested"
    nested.mkdir()
    (nested / "model-manifest.json").write_text("{}")
    with pytest.raises(ModelBindingError, match="inventory"):
        bindings.resolve(config)


def test_unknown_and_duplicate_binding_contracts_fail_closed(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text('{"schema_version":1,"schema_version":1,"bindings":[]}')
    with pytest.raises(ModelBindingError, match="duplicate"):
        load_runtime_bindings(path)
    path.write_text('{"schema_version":2,"bindings":[]}')
    with pytest.raises(ModelBindingError, match="unsupported"):
        load_runtime_bindings(path)


def test_symlinked_weight_is_rejected(tmp_path):
    bindings, config, _path = bind(tmp_path)
    weight = tmp_path / "model" / "weights.safetensors"
    source = tmp_path / "outside"
    source.write_bytes(weight.read_bytes())
    weight.unlink()
    weight.symlink_to(source)
    with pytest.raises(ModelBindingError, match="linked"):
        bindings.resolve(config)
