import itertools

import numpy as np
import pytest

from bili_asr.asr.constrained_alignment import decode_timestamp_path


def test_decoder_is_frozen_without_changing_historical_profile_bytes():
    import dataclasses
    import json
    from bili_asr.workflow_models import AsrProfile
    from bili_asr.profile_codec import decode_profile
    old = AsrProfile(profile_key="test", model_name="fixture/model")
    old_json = old.canonical()
    assert "alignment_decoding" not in old_json
    assert "alignment_decoder" not in old_json
    explicit = dataclasses.replace(old, alignment_decoder="monotone-max-score/v1")
    encoded = explicit.canonical()
    assert json.loads(encoded)["alignment_decoding"] == {
        "schema_version": 1, "algorithm": "monotone-max-score/v1"}
    restored = AsrProfile(profile_key="test", **decode_profile(encoded))
    assert restored.canonical() == encoded and restored.asr_config().alignment_decoder == explicit.alignment_decoder
    assert AsrProfile(profile_key="test", **decode_profile(old_json)).canonical() == old_json
    with pytest.raises(ValueError, match="alignment decoder"):
        decode_profile(old_json[:-1] + ',"alignment_decoder":"processor"}')


def test_score_decoder_uses_each_measured_window_and_retains_original():
    from types import SimpleNamespace
    from bili_asr.asr.constrained_alignment import decode_alignment
    class Array:
        def __init__(self, value): self.value = np.asarray(value)
        def __getitem__(self, key): return Array(self.value[key])
        def __eq__(self, value): return self.value == value
        def detach(self): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self.value
    scores = np.array([[[0,4,1,0], [0,5,3,0]], [[4,0,0,0], [5,3,0,0]]], dtype=float)
    original = [[{"text": "one", "start_time": .1, "end_time": .1}],
                [{"text": "two", "start_time": 0., "end_time": 0.}]]
    processor = SimpleNamespace(timestamp_segment_time=100, decode_forced_alignment=lambda **kwargs: original)
    decoded, report = decode_alignment(processor,
        logits=Array(scores), input_ids=Array([[7,7], [7,7]]),
        word_lists=[["one"], ["two"]], timestamp_token_id=7,
        durations=[.3,.1], policy="monotone-max-score/v1")
    assert decoded[0][0]["start_time"] == .1 and decoded[0][0]["end_time"] == .2
    assert decoded[1][0]["start_time"] == 0 and decoded[1][0]["end_time"] == .1
    assert report[0]["private_original_units"] == original[0]
    assert report[1]["valid_classes"] == 2 and len(report[0]["scores_sha256"]) == 64
    assert original[0][0]["start_time"] == original[0][0]["end_time"]


def test_path_matches_exhaustive_maximum_and_preserves_scores():
    scores = np.random.default_rng(71).normal(size=(4, 5))
    original = scores.copy()
    path, evidence = decode_timestamp_path(scores, duration_seconds=.4, step_seconds=.1)
    valid = [p for p in itertools.product(range(5), repeat=4)
             if p[0] < p[1] <= p[2] < p[3]]
    best = max(sum(scores[i, p[i]] for i in range(4)) for p in valid)
    assert sum(scores[i, path[i]] for i in range(4)) == pytest.approx(best)
    assert evidence['algorithm'] == 'monotone-max-score/v1'
    np.testing.assert_array_equal(scores, original)


def test_selects_real_classes_instead_of_interpolating_equal_argmaxes():
    scores = np.array([[0, 4, 1, 0], [0, 5, 3, 0]], dtype=float)
    path, evidence = decode_timestamp_path(scores, duration_seconds=.3, step_seconds=.1)
    assert path == [1, 2]
    assert evidence['changed_argmax_positions'] == 1


@pytest.mark.parametrize('scores,duration,step', [
    (np.zeros((3, 5)), 1, .1), (np.zeros((4, 2)), .1, .1),
    (np.array([[float('nan'), 1], [2, 3]]), 1, .1),
    (np.zeros((2, 5)), -1, .1), (np.zeros((2, 5)), 1, 0),
])
def test_invalid_or_infeasible_lattice_is_rejected(scores, duration, step):
    with pytest.raises(ValueError):
        decode_timestamp_path(scores, duration_seconds=duration, step_seconds=step)


def test_shared_decoder_routes_real_score_lattice_and_attests_decoder():
    from types import SimpleNamespace
    from bili_asr.asr.config import ASRConfig
    from bili_asr.asr.runner import ASRRunner
    from bili_asr.asr.constrained_alignment import decode_alignment

    class Array:
        def __init__(self, value): self.value = np.asarray(value)
        def __getitem__(self, key): return Array(self.value[key])
        def __eq__(self, value): return self.value == value
        def detach(self): return self
        def float(self): return self
        def cpu(self): return self
        def numpy(self): return self.value

    original = [[{"text": "one", "start_time": .1, "end_time": .1}]]
    received = []
    def processor(**kwargs):
        received.append(kwargs)
        return original
    models = SimpleNamespace(
        aligner=SimpleNamespace(config=SimpleNamespace(timestamp_token_id=7)),
        aligner_processor=SimpleNamespace(
            timestamp_segment_time=100, decode_forced_alignment=processor))
    arguments = dict(logits=Array([[[0,4,1,0], [0,5,3,0]]]),
                     input_ids=Array([[7,7]]), word_lists=[["one"]], durations=[.3])
    runner = ASRRunner(ASRConfig(model_name="fixture/model", device="cpu",
                                alignment_decoder="monotone-max-score/v1"))
    decoded, report = decode_alignment(models.aligner_processor, **arguments,
        timestamp_token_id=7, policy=runner.config.alignment_decoder)
    assert decoded == [[{"text": "one", "start_time": .1, "end_time": .2}]]
    assert received[0]["timestamp_token_id"] == 7
    assert report[0]["private_original_units"] == original[0]
    assert runner.provenance()["alignment_decoder"] == "monotone-max-score/v1"
    legacy = ASRRunner(ASRConfig(model_name="fixture/model", device="cpu"))
    legacy_units, report = decode_alignment(models.aligner_processor, **arguments,
        timestamp_token_id=7, policy=legacy.config.alignment_decoder)
    assert legacy_units is original and report == [{}]
    assert "alignment_decoder" not in legacy.provenance()


def test_config_rejects_unknown_alignment_decoder():
    from bili_asr.asr.config import ASRConfig
    with pytest.raises(ValueError, match="unsupported alignment decoder"):
        ASRConfig(model_name="fixture/model", alignment_decoder="sort-or-clamp")


@pytest.mark.parametrize("decoder", ["processor", "monotone-max-score/v1"])
@pytest.mark.parametrize("coverage", ["full-span", "quiet-edges"])
def test_deployed_aliases_keep_canonical_bytes_and_accept_new_coverage(decoder, coverage):
    from bili_asr.workflow_models import AsrProfile
    from bili_asr.profile_codec import decode_profile
    profile = AsrProfile("compat", "offline", alignment_decoder=decoder, coverage_policy=coverage)
    encoded = profile.canonical()
    restored = AsrProfile("compat", **decode_profile(encoded))
    assert restored.canonical() == encoded
    assert restored.asr_config().coverage_policy == coverage


@pytest.mark.parametrize("extension", [
    {"schema_version": True, "algorithm": "monotone-max-score/v1"},
    {"schema_version": 2, "algorithm": "monotone-max-score/v1"},
    {"schema_version": 1, "algorithm": "unknown"},
    {"schema_version": 1}, [],
])
def test_deployed_extension_rejects_malformed_schema(extension):
    import json
    from bili_asr.workflow_models import AsrProfile
    from bili_asr.profile_codec import decode_profile
    value = json.loads(AsrProfile("compat", "offline").canonical())
    value["alignment_decoding"] = extension
    with pytest.raises(ValueError, match="alignment decoding schema"):
        decode_profile(json.dumps(value))


def test_deployed_and_new_extensions_cannot_ambiguously_coexist():
    import json
    from bili_asr.workflow_models import AsrProfile
    from bili_asr.profile_codec import decode_profile
    value = json.loads(AsrProfile("compat", "offline", coverage_policy="quiet-edges").canonical())
    value["alignment_decoding"] = {"schema_version": 1, "algorithm": "monotone-max-score/v1"}
    with pytest.raises(ValueError, match="alignment decoding schema"):
        decode_profile(json.dumps(value))


def test_environment_default_does_not_rewrite_frozen_profile(monkeypatch):
    from bili_asr.asr.config import ASRConfig
    from bili_asr.workflow_models import AsrProfile
    monkeypatch.setenv("BILI_ASR_ALIGNMENT_DECODER", "monotone-max-score/v1")
    assert ASRConfig(model_name="offline").alignment_decoder == "monotone-max-score/v1"
    assert AsrProfile("frozen", "offline").asr_config().alignment_decoder == "default"
