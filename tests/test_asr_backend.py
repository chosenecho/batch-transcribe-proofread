"""The backend port executes both real runner stages without owning workflow state."""

from dataclasses import replace

import pytest

from bili_asr.asr.backend import HuggingFaceBackend
from bili_asr.asr.runner import ASRRunner
from test_asr_qwen import _runner


def test_runner_routes_both_stages_through_injected_backend(monkeypatch):
    original, _ = _runner(monkeypatch, text="今天。")
    models = original._get_models()
    expected = original.transcribe("/virtual/audio")

    class ObservedBackend(HuggingFaceBackend):
        def __init__(self):
            self.calls = []

        def generate(self, *args, **kwargs):
            self.calls.append(("generate", kwargs["disable_cache"]))
            return super().generate(*args, **kwargs)

        def align(self, *args):
            self.calls.append(("align", None))
            return super().align(*args)

    backend = ObservedBackend()
    runner = ASRRunner(original.config, model_factory=lambda **kwargs: models, backend=backend)
    assert runner.transcribe("/virtual/audio", bust_cache=True) == expected
    assert backend.calls == [("generate", True), ("align", None)]
    assert runner.diagnostics()["execution_policy"]["backend"]["capabilities"]["cancellation_scope"] == "owned_process"


def test_backend_rejects_unsupported_precision_before_model_loading(monkeypatch):
    runner, _ = _runner(monkeypatch)
    backend = HuggingFaceBackend()
    backend.capabilities = replace(backend.capabilities, precisions=("float16",))
    with pytest.raises(ValueError, match="precision"):
        ASRRunner(runner.config, backend=backend, model_factory=lambda **kwargs: pytest.fail("loaded"))


def test_hf_capabilities_do_not_claim_shared_abort_or_fp8():
    capabilities = HuggingFaceBackend.capabilities
    assert capabilities.quantization_scopes == ()
    assert capabilities.cancellation_scope == "owned_process"


def test_backend_capability_schema_and_compile_are_checked_before_loading(monkeypatch):
    runner, _ = _runner(monkeypatch)
    backend = HuggingFaceBackend()
    backend.capabilities = replace(backend.capabilities, compiled_alignment=False)
    with pytest.raises(ValueError, match="runtime strategy"):
        ASRRunner(replace(runner.config, aligner_compile=True), backend=backend)
    backend.capabilities = replace(backend.capabilities, schema_version=2)
    with pytest.raises(ValueError, match="capability schema"):
        ASRRunner(runner.config, backend=backend)
