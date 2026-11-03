"""Frozen budget identities and bounded real-progress supervision."""

import argparse
import hashlib
import json
from dataclasses import asdict, replace

import pytest

from bili_asr.asr.config import ASRConfig
from bili_asr.asr.deadlines import DEADLINE_DEFAULTS, ProgressWatchdog, inference_budget
from bili_asr.cli.workflow import _cmd_workflow, add_workflow_parser
from bili_asr.profile_codec import decode_profile
from bili_asr.storage import AsrProfile, WorkflowRepository
from bili_asr.workflow_models import AsrPolicy, JobKind
from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
from bili_asr.workflow_errors import JobExecutionError
from tests.test_asr_quality_foundation import database as _database


@pytest.fixture
def database(tmp_path):
    yield from _database.__wrapped__(tmp_path)


def test_existing_v2_bytes_and_digest_are_unchanged_and_scaled_extension_is_frozen(database):
    profile = AsrProfile("baseline", "offline", device="cpu")
    legacy = asdict(profile)
    for name in (*DEADLINE_DEFAULTS, "profile_key", "model_dtype", "aligner_dtype",
                 "asr_batch_size", "aligner_batch_size", "batch_max_audio_seconds", "batch_max_input_bytes",
                 "batch_max_tokens", "asr_attention", "aligner_attention", "asr_cache_implementation",
                 "asr_compile", "aligner_compile", "compile_max_buckets", "compile_max_input_tokens", "compile_max_output_tokens"):
        legacy.pop(name)
    for name in ("chunk_seconds", "inference_timeout_seconds", "tokens_per_second"):
        legacy[name] = float(legacy[name])
    old_bytes = json.dumps({"schema_version": 2, **legacy}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert profile.canonical() == old_bytes
    assert decode_profile(old_bytes)["inference_timeout_policy"] == "fixed"
    repository = WorkflowRepository(database)
    old_id = repository.register_profile(profile)
    assert repository.profile_digest(old_id) == hashlib.sha256(old_bytes.encode()).hexdigest()
    scaled = replace(profile, inference_timeout_policy="audio-scaled")
    new_id = repository.register_profile(scaled)
    assert new_id != old_id
    assert repository.profile(new_id) == scaled
    assert repository.profile(old_id) == profile
    assert json.loads(scaled.canonical())["deadline"]["schema_version"] == 1


def test_runtime_budget_default_mutation_cannot_change_frozen_identity(monkeypatch):
    profile = AsrProfile("frozen", "offline")
    encoded = profile.canonical()
    monkeypatch.setitem(DEADLINE_DEFAULTS, "inference_max_rtf", .5)
    assert profile.canonical() == encoded
    assert decode_profile(encoded)["inference_max_rtf"] == .25


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_deadline_extension_schema_version_is_exact(version):
    value = json.loads(AsrProfile("scaled", "offline", inference_timeout_policy="audio-scaled").canonical())
    value["deadline"]["schema_version"] = version
    with pytest.raises(ValueError, match="deadline schema"):
        decode_profile(json.dumps(value))


def test_unversioned_top_level_deadline_fields_cannot_override_legacy_defaults():
    value = json.loads(AsrProfile("frozen", "offline").canonical())
    value["inference_timeout_policy"] = "audio-scaled"
    with pytest.raises(ValueError, match="versioned extension"):
        decode_profile(json.dumps(value))


def test_audio_scaled_budget_keeps_minimum_but_has_independent_stall_bound():
    config = ASRConfig(model_name="offline", inference_timeout_policy="audio-scaled")
    budget = inference_budget(config, duration_ms=4 * 3600 * 1000)
    assert budget.total_seconds == 3900
    assert budget.no_progress_seconds == 600
    assert inference_budget(config, duration_ms=1000).total_seconds == 1800
    assert inference_budget(config, duration_ms=1000000000).total_seconds == 14400
    assert inference_budget(config, duration_ms=1000000000).capped
    assert inference_budget(replace(config, inference_timeout_policy="fixed"), duration_ms=None).total_seconds == 1800


@pytest.mark.parametrize("duration", [None, 0, -1, True, 4000.0, "4000"])
def test_scaled_budget_refuses_unverified_duration(duration):
    with pytest.raises(ValueError, match="verified positive"):
        inference_budget(ASRConfig(model_name="offline", inference_timeout_policy="audio-scaled"), duration_ms=duration)


@pytest.mark.parametrize("changes", [
    {"inference_timeout_policy": []}, {"inference_max_rtf": False}, {"inference_startup_seconds": float("nan")},
    {"inference_hard_cap_seconds": 0}, {"inference_no_progress_seconds": float("inf")},
    {"inference_timeout_policy": "audio-scaled", "inference_hard_cap_seconds": 100},
])
def test_invalid_budget_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        ASRConfig(model_name="offline", **changes)


def test_total_deadline_startup_and_stall_are_distinct_and_heartbeats_do_not_count():
    watch = ProgressWatchdog(started=0, total_seconds=100, startup_seconds=5, no_progress_seconds=3)
    assert not watch.observe({"sequence": 1, "phase": "heartbeat"}, 4)
    assert not watch.observe({"sequence": 1, "phase": []}, 4)
    assert watch.expired(5) == "startup_deadline"
    assert watch.observe({"sequence": 1, "phase": "model_ready"}, 4)
    assert not watch.observe({"sequence": 1, "phase": "model_ready"}, 6)
    assert watch.expired(7) == "no_progress"
    assert watch.observe({"sequence": 2, "phase": "decoded"}, 99)
    assert watch.expired(100) == "total_deadline"


@pytest.mark.parametrize("now", [3, 2.5, float("nan"), float("inf"), True])
def test_progress_watchdog_rejects_nonmonotonic_or_nonfinite_progress_times(now):
    watch = ProgressWatchdog(started=5, total_seconds=100, no_progress_seconds=3)
    assert not watch.observe({"sequence": 1, "phase": "decoded"}, now)
    assert watch.progress_count == 0 and watch.last_progress == 5
    assert watch.observe({"sequence": 1, "phase": "decoded"}, 5)
    assert watch.last_progress == 5


def test_cli_explicit_scaled_policy_persists_all_frozen_budget_inputs(database, tmp_path):
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    args = parser.parse_args(["workflow", "plan", "--part-id", "1", "--device", "cpu",
        "--inference-timeout-policy", "audio-scaled", "--inference-max-rtf", "0.3",
        "--inference-startup-seconds", "301", "--inference-hard-cap-seconds", "15000",
        "--inference-no-progress-seconds", "601"])
    assert _cmd_workflow(args) == 0
    profile_id = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()[0]
    frozen = WorkflowRepository(database).profile(profile_id)
    assert frozen.inference_timeout_policy == "audio-scaled"
    assert frozen.inference_max_rtf == .3 and frozen.inference_startup_seconds == 301
    assert frozen.inference_hard_cap_seconds == 15000 and frozen.inference_no_progress_seconds == 601


@pytest.mark.parametrize("device", ["cpu", "cuda"])
@pytest.mark.parametrize("duration_ms", [None, 14400000])
def test_runtime_scaled_deadline_uses_fixed_input_duration_and_killable_boundary(database, tmp_path, device, duration_ms):
    repository = WorkflowRepository(database)
    profile_id = repository.register_profile(AsrProfile("scaled", "offline", device=device,
        inference_timeout_policy="audio-scaled"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id)
    audio_path = tmp_path / "audio" / "test.m4a"
    audio_path.parent.mkdir()
    audio_path.write_bytes(b"synthetic audio; decoder is substituted")
    audio = repository.claim("test", kinds=(JobKind.AUDIO,))
    result = {"storage_key": "audio/test.m4a", "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest()}
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    repository.finish(audio.job_id, worker_id="test", result=result)
    job = repository.claim("test", kinds=(JobKind.ASR,))
    seen = []
    def infer(config, path, *, timeout_seconds, diagnostics_sink, **kwargs):
        seen.append(timeout_seconds)
        return [{"start": 0, "end": 14400, "text": "完整文本"}], {"language": "Chinese"}, {
            "decoded_s": 14400, "produced_s": 14400, "coverage": 1, "coverage_min": .85, "coverage_short": False}
    def forbidden_unbounded_runner(_config):
        raise AssertionError("scaled CPU inference must also use the supervised boundary")
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None,
        runner_factory=forbidden_unbounded_runner, timeout_transcriber=infer)
    try:
        if duration_ms is None:
            with pytest.raises(JobExecutionError) as caught:
                handlers.local_asr(job)
            assert caught.value.error_code == "asr_deadline_duration_unavailable" and not seen
        else:
            outcome = handlers.local_asr(job)
            from bili_asr.storage import TranscriptRepository
            evidence = TranscriptRepository(database).read_asr_evidence(outcome["run_id"], 1)
            assert seen == [3900]
            assert evidence["diagnostics"]["deadline"]["audio_duration_ms"] == duration_ms
            assert evidence["config_sha256"] == repository.profile_digest(profile_id)
    finally:
        handlers.close()
