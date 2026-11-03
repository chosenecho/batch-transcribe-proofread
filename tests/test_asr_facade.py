"""Runner substitution is explicit and does not mutate another module."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

import pytest

from bili_asr import asr
from bili_asr.asr import runner


class _Runner:
    def transcribe(self, audio_path):
        return [{"start": 0, "end": 1, "text": audio_path}]

    def provenance(self):
        return {"model_name": "injected", "language": "und"}


def test_asr_boundaries_are_ordinary_modules():
    assert type(asr) is ModuleType
    assert type(runner) is ModuleType
    assert type(import_module("bili_asr.asr.provenance")) is ModuleType


def test_public_function_annotations_resolve_without_loading_runtime():
    from typing import Any, get_type_hints

    assert get_type_hints(asr.transcribe)["return"] == list[dict[str, Any]]
    assert "runner_factory" in get_type_hints(asr.provenance)


def test_falsey_callable_boundary_factory_is_honored(monkeypatch):
    class Factory:
        def __bool__(self):
            return False

        def __call__(self, config):
            return _Runner()

    monkeypatch.setattr(asr, "ASRRunner", Factory())
    assert asr.provenance()["model_name"] == "injected"
    assert asr.transcribe("audio.flac")[0]["text"] == "audio.flac"


def test_package_runner_substitution_applies_to_one_shot_calls(monkeypatch):
    seen = []
    original = runner.ASRRunner

    def factory(config):
        seen.append(config)
        return _Runner()

    monkeypatch.setattr(asr, "ASRRunner", factory)
    assert asr.transcribe("audio.m4a") == [{"start": 0, "end": 1, "text": "audio.m4a"}]
    assert asr.provenance() == {"model_name": "injected", "language": "und"}
    assert seen[0] is None
    assert isinstance(seen[1], asr.ASRConfig)
    assert runner.ASRRunner is original


@pytest.mark.parametrize("boundary", [asr, runner])
def test_explicit_factory_honors_model_override(boundary):
    seen = []

    def factory(config):
        seen.append(config)
        return _Runner()

    assert boundary.transcribe("audio.flac", "custom-model", runner_factory=factory)[0]["text"] == "audio.flac"
    assert seen[0].model_name == "custom-model"


@pytest.mark.parametrize("boundary", [asr, runner])
def test_explicit_factory_takes_precedence_over_boundary_default(boundary, monkeypatch):
    def unexpected(config):
        pytest.fail("default runner was constructed despite explicit injection")

    monkeypatch.setattr(boundary, "ASRRunner", unexpected)
    assert boundary.transcribe("audio.m4a", runner_factory=lambda config: _Runner())
    assert boundary.provenance(runner_factory=lambda config: _Runner())["model_name"] == "injected"


def test_direct_runner_substitution_does_not_change_public_factory(monkeypatch):
    original = asr.ASRRunner
    monkeypatch.setattr(runner, "ASRRunner", lambda config: _Runner())
    assert runner.provenance()["model_name"] == "injected"
    assert asr.ASRRunner is original


def test_default_provenance_does_not_load_models(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("provenance must not construct models")

    monkeypatch.setattr(runner, "_load_qwen_models", unexpected)
    metadata = asr.provenance()
    assert metadata["model_name"]
    assert metadata["offline"] == "True"
