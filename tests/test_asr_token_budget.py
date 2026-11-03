"""Generation budgets use CPU masks and retain historical numerical semantics."""

import numpy as np
import pytest

from bili_asr.asr.preparation import DecodeInputs
from test_asr_qwen import _Inputs, _runner


@pytest.mark.parametrize("lengths", [(0,), (1,), (49,), (50,), (501,), (18000,), (20, 700)])
def test_cpu_mask_budget_matches_original_formula_with_padding(monkeypatch, lengths):
    runner, _ = _runner(monkeypatch)
    mask = np.zeros((len(lengths), max(max(lengths), 1)), dtype=np.int64)
    for row, length in enumerate(lengths):
        mask[row, :length] = 1
    inputs = _Inputs(input_features_mask=mask)
    prepared = runner._decode_input_budget(inputs)
    assert prepared.max_new_tokens == max(runner.config.min_new_tokens,
        int(max(lengths) / 100 * runner.config.tokens_per_second))
    assert prepared.feature_seconds == max(lengths) / 100
    assert prepared.inputs is inputs


@pytest.mark.parametrize("legacy_mapping", [False, True])
def test_generation_never_reads_budget_mask_after_transfer(monkeypatch, legacy_mapping):
    runner, _ = _runner(monkeypatch)
    models = runner._get_models()
    prepared = runner._prepare_decode_inputs(models.processor, np.zeros(16000, dtype=np.float32))
    assert isinstance(prepared, DecodeInputs)

    class DeviceMask:
        def sum(self, *args):
            pytest.fail("budget calculation synchronized the transferred mask")

    class TransferInputs(_Inputs):
        def to(self, *args):
            self["input_features_mask"] = DeviceMask()
            return self

    inputs = TransferInputs(prepared.inputs)
    value = inputs if legacy_mapping else DecodeInputs(inputs, prepared.max_new_tokens, prepared.feature_seconds)
    runner._transcribe_chunk(models, np.zeros(16000), prepared_inputs=value)
    assert models.model.calls[-1]["max_new_tokens"] == prepared.max_new_tokens
    assert runner._last_generation["budget_metadata"] == {
        "source": "processor_mask_before_transfer", "feature_seconds": prepared.feature_seconds}
