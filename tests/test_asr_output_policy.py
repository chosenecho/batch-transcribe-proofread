"""Opt-in output policies retain raw evidence and reject unproven repairs."""

import argparse
from copy import deepcopy
from dataclasses import replace
from itertools import product
import json
from types import SimpleNamespace

import numpy as np
import pytest

from bili_asr.asr.admission import assess_output
from bili_asr.asr.config import ASRConfig
from bili_asr.asr.constrained_alignment import decode_alignment, decode_timestamp_path
from bili_asr.asr.coverage import _coverage_record
from bili_asr.asr.diagnostics import assemble_diagnostics
from bili_asr.asr.edge_coverage import covered_edges, measure_edges
from bili_asr.cli.workflow import _cmd_workflow, add_workflow_parser
from bili_asr.profile_codec import decode_profile
from bili_asr.storage import AsrProfile, WorkflowRepository
from test_asr_batching import runner_for
from test_asr_admission import runtime as runtime
from test_asr_quality_foundation import database as database
from test_asr_qwen import _Inputs, _runner


@pytest.mark.parametrize("seed", range(8))
def test_decoder_matches_exhaustive_maximum_not_timestamp_sorting(seed):
    scores = np.random.default_rng(seed).normal(size=(4, 5))
    paths = [p for p in product(range(5), repeat=4) if p[0] < p[1] <= p[2] < p[3]]
    best = max(sum(scores[i, p[i]] for i in range(4)) for p in paths)
    path, evidence = decode_timestamp_path(scores, duration_seconds=.32, step_seconds=.08)
    assert tuple(path) in paths
    assert sum(scores[i, path[i]] for i in range(4)) == pytest.approx(best)
    assert evidence["score"] == pytest.approx(best)
    assert len(evidence["scores_sha256"]) == 64


def test_decoder_ignores_padded_classes_and_preserves_an_already_valid_argmax():
    scores = np.zeros((4, 9))
    scores[np.arange(4), [0, 1, 1, 2]] = 5
    scores[:, 8] = 100
    path, report = decode_timestamp_path(scores, duration_seconds=.19, step_seconds=.08)
    assert path == [0, 1, 1, 2]
    assert report["valid_classes"] == 3 and report["changed_argmax_positions"] == 4
    path, report = decode_timestamp_path(scores[:, :3], duration_seconds=.19, step_seconds=.08)
    assert path == [0, 1, 1, 2] and report["changed_argmax_positions"] == 0


@pytest.mark.parametrize("scores,duration,step", [
    (np.ones((3, 5)), 1, .08), (np.ones((0, 5)), 1, .08),
    (np.ones((4, 5)), .1, .08), (np.ones((2, 5)), 0, .08),
    (np.ones((2, 5)), True, .08), (np.ones((2, 5)), 1, float("nan")),
    (np.array([[0, 1, np.nan], [1, 0, 0]]), .08, .08),
])
def test_infeasible_or_invalid_alignment_fails_closed(scores, duration, step):
    with pytest.raises(ValueError):
        decode_timestamp_path(scores, duration_seconds=duration, step_seconds=step)


def test_alignment_memory_budget_is_checked_before_lattice_allocation(monkeypatch):
    monkeypatch.setattr("bili_asr.asr.constrained_alignment.MAX_LATTICE_CELLS", 5)
    with pytest.raises(ValueError, match="budget"):
        decode_timestamp_path(np.ones((2, 4)), duration_seconds=1, step_seconds=.08)


class ScoreProcessor:
    timestamp_segment_time = 80

    def prepare_forced_aligner_inputs(self, audio, transcript, language):
        texts = transcript if isinstance(transcript, list) else [transcript]
        words = [[c for c in text if c != "。"] for text in texts]
        assert len({len(w) for w in words}) == 1
        return _Inputs(input_ids=np.full((len(words), 2 * len(words[0])), 10)), words

    def decode_forced_alignment(self, *, word_lists, **kwargs):
        return [[{"text": w, "start_time": 0., "end_time": 0.} for w in words] for words in word_lists]


class ScoreAligner:
    device, dtype = "cpu", None
    config = SimpleNamespace(timestamp_token_id=10)

    def __call__(self, **inputs):
        shape = (*inputs["input_ids"].shape, 16)
        scores = np.zeros(shape)
        scores[..., 2] = 5  # Raw independent argmax collapses every word.
        return SimpleNamespace(logits=scores)


@pytest.mark.parametrize("width,fallback", [(1, False), (2, False), (2, True)])
@pytest.mark.parametrize("decoder", ["monotone-max-score", "monotone-max-score/v1"])
def test_serial_native_batch_and_capability_fallback_share_decoding(monkeypatch, width, fallback, decoder):
    runner, models = runner_for(monkeypatch, asr_batch_size=width, aligner_batch_size=width,
        alignment_decoder=decoder)
    models.aligner_processor, models.aligner = ScoreProcessor(), ScoreAligner()
    if fallback:
        runner.backend.capabilities = replace(runner.backend.capabilities, native_alignment_batch=False)
    cues = runner.transcribe("/virtual/audio")
    assert "".join(c["text"] for c in cues).replace(" ", "") == "词11。词12。词13。词14。"
    for chunk in runner.diagnostics()["passes"][0]["chunks"]:
        assert chunk["alignment"]["zero_duration_alignment_units"] == 0
        assert chunk["alignment"]["backward_alignment_units"] == 0
        assert chunk["alignment_decode"]["original_zero_duration_units"] == 3
        assert chunk["alignment_decode"]["original_units_truncated"] is False
    assert runner.provenance()["alignment_decoder"] == decoder


@pytest.mark.parametrize("width", [1, 2])
def test_padding_cannot_make_an_impossible_short_tail_alignable(monkeypatch, width):
    runner, models = runner_for(monkeypatch, lengths=(1, .1), identifiers=(11, 12),
        asr_batch_size=width, aligner_batch_size=width, alignment_decoder="monotone-max-score")
    models.aligner_processor, models.aligner = ScoreProcessor(), ScoreAligner()
    with pytest.raises(ValueError, match="positive-span"):
        runner.transcribe("/virtual/audio")
    assert runner.transcribed_segments() is None
    assert runner.diagnostics()["passes"][0]["completed"] is False


def test_default_decoder_does_not_require_logits_access_or_change_processor_results():
    units = [[{"text": "x", "start_time": 0, "end_time": 0}]]
    processor = SimpleNamespace(decode_forced_alignment=lambda **kwargs: units)
    result, evidence = decode_alignment(processor, logits=None, input_ids=None, word_lists=[["x"]],
        timestamp_token_id=10, durations=[1], policy="default")
    assert result is units and evidence == [{}]


def test_original_prediction_witness_has_unit_and_total_text_bounds():
    words = ["x" * 200] * 600
    logits = np.ones((1, 1200, 602), dtype=np.float32)
    result, evidence = decode_alignment(ScoreProcessor(), logits=logits,
        input_ids=np.full((1, 1200), 10), word_lists=[words], timestamp_token_id=10,
        durations=[60], policy="monotone-max-score")
    assert len(result[0]) == 600 and result[0][0]["text"] == words[0]
    report = evidence[0]
    assert len(report["private_original_units"]) <= 512 and report["original_units_truncated"]
    assert sum(len(u["text"]) for u in report["private_original_units"]) <= 8192


def quiet_case():
    samples = np.r_[np.zeros(100), np.full(800, .1), np.full(100, 1e-6)]
    cues = [{"start": 1., "end": 9., "text": "fixture speech"}]
    report = {"completed": True, "decoded_s": 10., "span_coverage_short": True, "chunks": [],
        "quiet_edges": measure_edges(samples, 100), "coverage_cue_span": [1., 9.],
        "execution_policy": {"output_policy": {"schema_version": 1,
            "coverage_policy": "quiet-edges", "alignment_decoder": "default"}}}
    return cues, _coverage_record(10., cues), report


def test_only_proven_quiet_edges_explain_short_span_without_rewriting_raw_evidence():
    cues, coverage, report = quiet_case()
    before = deepcopy((cues, coverage, report))
    diagnostics = assemble_diagnostics([report])
    result = assess_output(cues, coverage=coverage, diagnostics=diagnostics)
    assert result["status"] == "usable" and result["coverage_basis"] == "quiet-edges/v1"
    assert coverage["coverage"] == .8 and coverage["coverage_short"] is True
    assert result["full_span_coverage_short"] is True and result["confirmed_silence"] is None
    assert diagnostics["quality"]["status"] == "no-known-risk"
    assert (cues, coverage, report) == before


@pytest.mark.parametrize("change", ["no-policy", "old-schema", "bool-schema", "wrong-duration",
    "bool-offset", "wrong-threshold", "empty-signal", "unverified-extra-field"])
def test_missing_or_corrupt_witness_cannot_launder_short_coverage(change):
    cues, coverage, report = quiet_case()
    if change == "no-policy":
        report.pop("execution_policy")
    elif change in ("old-schema", "bool-schema"):
        report["execution_policy"]["output_policy"]["schema_version"] = True if change == "bool-schema" else 2
    else:
        name, value = {"wrong-duration": ("sample_count", 2000), "bool-offset": ("first_signal_sample", True),
            "wrong-threshold": ("peak_dbfs", -30), "empty-signal": ("status", "no_signal_above_threshold"),
            "unverified-extra-field": ("coverage", 1)}[change]
        report["quiet_edges"][name] = value
    report["edge_coverage"] = {"method": "quiet-edges/v1", "coverage_short": False, "coverage": 1}
    result = assess_output(cues, coverage=coverage, diagnostics=assemble_diagnostics([report]))
    assert result["status"] == "needs-review" and result["issue_counts"]["asr_coverage_short"] == 1


def test_ordinary_quiet_speech_and_interior_gaps_are_not_removed():
    samples = np.r_[np.full(100, .001), np.zeros(800), np.full(100, .001)]
    evidence = measure_edges(samples, 100)
    assert evidence["first_signal_sample"] == 0 and evidence["last_signal_sample_exclusive"] == 1000
    measured = covered_edges(evidence, decoded_s=10, cue_start=1, cue_end=9, minimum=.97)
    assert measured["coverage"] == .8 and measured["coverage_short"]
    evidence = measure_edges(np.zeros(100), 100)
    assert evidence["status"] == "no_signal_above_threshold"
    assert covered_edges(evidence, decoded_s=1, cue_start=0, cue_end=1, minimum=.97) is None


@pytest.mark.parametrize("samples", [np.zeros((2, 4)), np.array([]), np.array([np.nan, 1]), np.array([1, np.inf])])
def test_unmeasurable_boundary_audio_is_not_admissible(samples):
    assert measure_edges(samples, 100)["status"] == "unavailable"


def test_nonfinite_interior_and_oversized_numeric_witness_fail_closed():
    assert measure_edges(np.r_[1., np.zeros(100), np.nan, np.zeros(100), 1.], 100)["status"] == "unavailable"
    _, _, report = quiet_case()
    report["quiet_edges"]["sample_count"] = 10**400
    assert covered_edges(report["quiet_edges"], decoded_s=10, cue_start=1, cue_end=9, minimum=.97) is None
    report["quiet_edges"].update(sample_rate=10**400, first_signal_sample=10**400 - 1,
                                last_signal_sample_exclusive=10**400)
    assert covered_edges(report["quiet_edges"], decoded_s=1, cue_start=0, cue_end=1, minimum=.97) is None


def test_final_pass_cannot_borrow_another_pass_policy_or_witness():
    cues, coverage, report = quiet_case()
    diagnostics = assemble_diagnostics([report])
    diagnostics["passes"][0]["execution_policy"]["output_policy"]["coverage_policy"] = "full-span"
    assert assess_output(cues, coverage=coverage, diagnostics=diagnostics)["status"] == "needs-review"


def test_edge_evidence_never_masks_alignment_timestamp_or_generation_failures():
    cues, coverage, report = quiet_case()
    report["chunks"] = [{"flags": ["token-limit-reached"], "alignment": {"zero_duration_alignment_units": 1}}]
    result = assess_output(cues, coverage=coverage, diagnostics=assemble_diagnostics([report]))
    assert result["status"] == "unusable" and "asr_alignment_unresolved" in result["issue_counts"]
    report["chunks"][0]["alignment"]["zero_duration_alignment_units"] = 0
    result = assess_output(cues, coverage=coverage, diagnostics=assemble_diagnostics([report]))
    assert result["status"] == "needs-review" and "asr_diagnostics_review_required" in result["issue_counts"]
    cues[0]["end"] = 11
    assert assess_output(cues, coverage=coverage, diagnostics=assemble_diagnostics([report]))["status"] == "unusable"


def test_runner_measures_actual_audio_and_keeps_full_span_record(monkeypatch):
    runner, _ = _runner(monkeypatch, text="甲。", coverage_policy="quiet-edges")
    models = runner._get_models()
    models.aligner_processor.units = [{"text": "甲", "start_time": .1, "end_time": .9}]
    samples = np.r_[np.zeros(1600), np.full(12800, .1), np.zeros(1600)].astype(np.float32)
    monkeypatch.setattr("bili_asr.asr.audio._read_audio", lambda *args: (samples, 16000))
    cues = runner.transcribe("/virtual/audio")
    assert runner.transcribed_coverage()["coverage_short"] is True
    result = assess_output(cues, coverage=runner.transcribed_coverage(), diagnostics=runner.diagnostics())
    assert result["status"] == "usable"


@pytest.mark.parametrize("extension", [{"alignment_decoder": "monotone-max-score"},
    {"coverage_policy": "quiet-edges"}, {"alignment_decoder": "monotone-max-score", "coverage_policy": "quiet-edges"}])
def test_explicit_policies_have_new_frozen_identity_and_roundtrip(database, extension):
    repository = WorkflowRepository(database)
    old = AsrProfile("baseline", "offline", device="cpu")
    profile = replace(old, **extension)
    old_id, new_id = repository.register_profile(old), repository.register_profile(profile)
    assert new_id != old_id and repository.profile(old_id) == old
    assert repository.profile(new_id) == profile
    assert json.loads(profile.canonical())["output_policy"]["schema_version"] == 1
    assert profile.asr_config().alignment_decoder == profile.alignment_decoder


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_output_policy_schema_is_exact(version):
    value = json.loads(AsrProfile("x", "offline", coverage_policy="quiet-edges").canonical())
    value["output_policy"]["schema_version"] = version
    with pytest.raises(ValueError, match="output policy schema"):
        decode_profile(json.dumps(value))


def test_unversioned_fields_and_invalid_policy_names_are_rejected():
    value = json.loads(AsrProfile("x", "offline").canonical())
    value["coverage_policy"] = "quiet-edges"
    with pytest.raises(ValueError, match="versioned extension"):
        decode_profile(json.dumps(value))
    for name in ("alignment_decoder", "coverage_policy"):
        for invalid in (True, [], "unknown"):
            with pytest.raises(ValueError):
                ASRConfig(model_name="offline", **{name: invalid})


def test_cli_freezes_explicit_policy_and_checkpoint_identity_is_separate(database, tmp_path, monkeypatch):
    # The split repository requires registered local audio before planning.
    import hashlib
    from bili_asr.storage.workflow_outputs import record_audio
    media = tmp_path / "audio" / "offline-fixture.wav"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"offline fixture; no decoder runs in this test")
    with database:
        database.execute("BEGIN")
        record_audio(database, part_id=1, sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
            byte_size=media.stat().st_size, media_format="wav", duration_ms=1000,
            storage_key="audio/offline-fixture.wav", target_id="local")
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    args = parser.parse_args(["workflow", "plan", "--part-id", "1", "--device", "cpu",
        "--alignment-decoder", "monotone-max-score", "--coverage-policy", "quiet-edges"])
    assert _cmd_workflow(args) == 0
    row = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()
    profile = WorkflowRepository(database).profile(row[0])
    assert profile.alignment_decoder == "monotone-max-score" and profile.coverage_policy == "quiet-edges"
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"checkpoint identity fixture")
    old, _ = _runner(monkeypatch)
    new, _ = _runner(monkeypatch, alignment_decoder="monotone-max-score")
    assert old._checkpoint_identity(str(audio), 2) != new._checkpoint_identity(str(audio), 2)
    assert "output_policy" not in old._checkpoint_identity(str(audio), 2)


def test_explicit_policy_repair_succeeds_without_upgrading_old_failed_run(runtime):
    from bili_asr.services.asr_repair import repair_asr
    from bili_asr.storage import JobKind
    from bili_asr.storage.asr_quality import assess_transcript
    from bili_asr.workflow import WorkflowExecutor
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    from test_asr_admission import executor

    connection, workflow, profile, root = runtime
    cues, coverage, report = quiet_case()
    handlers, worker = executor(runtime, cues, coverage)
    try:
        assert worker.run().failed == 1
    finally:
        handlers.close()
    old = next(j for j in workflow.list_jobs() if j.kind is JobKind.ASR)
    old_run = connection.execute("SELECT run_id FROM acquisition_runs WHERE kind='asr'").fetchone()[0]
    attempts = [tuple(r) for r in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (old.job_id,))]
    repair_asr(workflow, job_id=old.job_id, profile=replace(profile, coverage_policy="quiet-edges"),
        reason="original audio verified; measured quiet edge policy selected")

    def infer(config, path, *, diagnostics_sink, **kwargs):
        assert config.coverage_policy == "quiet-edges"
        diagnostics_sink.update(assemble_diagnostics([report]))
        return cues, {"language": "Chinese"}, coverage

    handlers = ArchiveWorkflowHandlers(connection, workflow, archive_root=root, sessdata=None, timeout_transcriber=infer)
    try:
        worker = WorkflowExecutor(workflow, worker_id="repaired", handlers=handlers.handlers(), kinds=(JobKind.ASR,))
        assert worker.run().succeeded == 1
    finally:
        handlers.close()
    transcript_id = connection.execute("SELECT transcript_id FROM transcripts LIMIT 1").fetchone()[0]
    assert assess_transcript(connection, transcript_id)["status"] == "usable"
    assert assess_transcript(connection, transcript_id, run_id=old_run)["status"] == "needs-review"
    assert workflow.explain_job(old.job_id)["status"] == "failed"
    assert attempts == [tuple(r) for r in connection.execute("SELECT * FROM workflow_attempts WHERE job_id=?", (old.job_id,))]
    assert all(r[0] == .8 and r[1] == 1 for r in connection.execute(
        "SELECT coverage,coverage_short FROM transcript_coverage_attestations"))
