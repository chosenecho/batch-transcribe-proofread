"""Regression evidence for frozen profiles and observable ASR runs."""

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from bili_asr.asr.config import ASRConfig, default_config
from bili_asr.asr.diagnostics import (
    alignment_evidence,
    assemble_diagnostics,
    generation_evidence,
)
from bili_asr.asr.runner import _load_qwen_models
from bili_asr.cli.workflow import _cmd_workflow, add_workflow_parser
from bili_asr.storage import (
    AcquisitionRunRecord,
    AsrProfile,
    TranscriptRepository,
    TranscriptSegmentRecord,
    WorkflowRepository,
    open_database,
)


@pytest.fixture
def database(tmp_path):
    connection = open_database(tmp_path)
    with connection:
        connection.execute("INSERT INTO bilibili_users(mid, display_name, created_at, updated_at) VALUES (1, 'test', 1, 1)")
        connection.execute("INSERT INTO videos(bvid, aid, mid, title, pubdate, created_at, updated_at) VALUES ('BVtest', 1, 1, 'test', 1, 1, 1)")
        connection.execute("""INSERT INTO video_parts(video_part_id, bvid, page_index, cid, title,
            duration_ms, processing_status, created_at, updated_at)
            VALUES (1, 'BVtest', 0, 1, 'p0', 1000, 'metadata_collected', 1, 1)""")
    yield connection
    connection.close()


@pytest.mark.parametrize("changes", [
    {"aligner_revision": "aligner-commit"}, {"chunk_seconds": 90.0},
    {"inference_timeout_seconds": 60.0}, {"hotwords": ("术语",)},
    {"offline": False}, {"tokens_per_second": 12.0}, {"min_new_tokens": 512},
    {"second_pass_use_cache": True}, {"language": "Chinese"},
])
def test_effective_parameter_changes_create_distinct_profiles(database, changes, monkeypatch):
    repository = WorkflowRepository(database)
    base = AsrProfile("stable", "Qwen/Qwen3-ASR-1.7B-hf", device="cpu")
    first = repository.register_profile(base)
    tuned = replace(base, **changes)
    second = repository.register_profile(tuned)
    assert first != second
    assert repository.register_profile(tuned) == second
    monkeypatch.setenv("BILI_ASR_CHUNK_SECONDS", "invalid-after-planning")
    assert repository.profile(second) == tuned
    assert repository.profile(second).asr_config().chunk_seconds == tuned.chunk_seconds


def test_legacy_profile_preserves_digest_and_shared_revision(database):
    values = {"model_name": "model", "model_revision": "old-commit", "aligner_name": "aligner", "device": "cpu", "language": None}
    digest = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    with database:
        cursor = database.execute("""INSERT INTO workflow_asr_profiles(
            profile_key, model_name, model_revision, aligner_name, device, language,
            config_sha256, created_at) VALUES ('legacy', 'model', 'old-commit', 'aligner', 'cpu', NULL, ?, 1)""", (digest,))
    repository = WorkflowRepository(database)
    profile = repository.profile(cursor.lastrowid)
    assert profile.aligner_revision == "old-commit"
    assert profile.chunk_seconds == 180.0
    assert repository.profile_digest(cursor.lastrowid) == digest


@pytest.mark.parametrize("delete", [False, True])
def test_corrupt_or_missing_new_snapshot_is_rejected(database, delete):
    repository = WorkflowRepository(database)
    profile_id = repository.register_profile(AsrProfile("stable", "model", chunk_seconds=90))
    with database:
        if delete:
            database.execute("DELETE FROM workflow_asr_profile_configs WHERE profile_id=?", (profile_id,))
        else:
            database.execute("""UPDATE workflow_asr_profile_configs
                SET config_json=json_set(config_json, '$.chunk_seconds', 60) WHERE profile_id=?""", (profile_id,))
    with pytest.raises(ValueError, match="hash mismatch|snapshot missing"):
        repository.profile(profile_id)


@pytest.mark.parametrize("name,value", [
    ("chunk_seconds", float("nan")), ("chunk_seconds", float("inf")),
    ("tokens_per_second", float("inf")), ("tokens_per_second", 0),
    ("min_new_tokens", True), ("second_pass_use_cache", 1),
])
def test_nonfinite_or_invalid_parameters_are_rejected(name, value):
    with pytest.raises(ValueError):
        ASRConfig(model_name="model", **{name: value})


def test_cli_precedence_and_snapshot_freeze(database, tmp_path, monkeypatch):
    monkeypatch.setenv("BILI_ASR_CHUNK_SECONDS", "invalid")
    monkeypatch.setenv("BILI_ASR_INFERENCE_TIMEOUT_SECONDS", "72")
    monkeypatch.setenv("BILI_ASR_ALIGNER_REVISION", "aligner-commit")
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    args = parser.parse_args(["workflow", "plan", "--part-id", "1",
                              "--chunk-seconds", "60", "--device", "cpu"])
    assert _cmd_workflow(args) == 0
    profile_id = database.execute("SELECT profile_id FROM workflow_asr_profiles").fetchone()[0]
    profile = WorkflowRepository(database).profile(profile_id)
    assert profile.chunk_seconds == 60
    assert profile.inference_timeout_seconds == 72
    assert profile.aligner_revision == "aligner-commit"
    monkeypatch.setenv("BILI_ASR_INFERENCE_TIMEOUT_SECONDS", "999")
    assert WorkflowRepository(database).profile(profile_id).asr_config().inference_timeout_seconds == 72


def test_model_loader_keeps_revisions_separate_and_enforces_offline(monkeypatch, mock_torch):
    calls = []
    class Loader:
        @staticmethod
        def from_pretrained(name, **kwargs):
            calls.append((name, kwargs))
            return SimpleNamespace(eval=lambda: None)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoProcessor=Loader, AutoModelForMultimodalLM=Loader,
        AutoModelForTokenClassification=Loader,
    ))
    _load_qwen_models(model_name="model", aligner_name="aligner", model_revision="asr-commit",
                      aligner_revision="align-commit", device="cpu", offline=True)
    assert [kwargs["revision"] for _, kwargs in calls] == ["asr-commit", "asr-commit", "align-commit", "align-commit"]
    assert all(kwargs["local_files_only"] for _, kwargs in calls)


def test_alignment_union_detects_internal_gaps_and_invalid_units():
    units = [{"start_time": 0, "end_time": 2}, {"start_time": 1, "end_time": 3},
             {"start_time": 8, "end_time": 10}, {"start_time": 5, "end_time": 4},
             {"start_time": 0, "end_time": float("nan")}]
    evidence = alignment_evidence(units, 10)
    assert evidence["aligned_unit_union_s"] == 5
    assert evidence["longest_unaligned_gap_s"] == 5
    assert evidence["invalid_alignment_units"] == 2


def test_generation_limit_distinguishes_observed_eos():
    model = SimpleNamespace(generation_config=SimpleNamespace(eos_token_id=[2, 3]))
    evidence = generation_evidence(np.array([[1, 2]]), model, 2)
    assert evidence["token_limit_reached"] is True
    assert evidence["ended_with_eos"] is True
    assert generation_evidence(np.array([[1, 4]]), model, 2)["ended_with_eos"] is False


def test_middle_empty_chunk_is_reviewable_even_with_complete_span():
    first = {"completed": True, "decoded_s": 10, "timings_s": {"total": 4}, "chunks": []}
    final = {"completed": True, "decoded_s": 10, "span_coverage_short": False,
             "timings_s": {"total": 3}, "chunks": [
                 {"flags": []}, {"flags": ["empty-output"]}, {"flags": []}]}
    result = assemble_diagnostics([first, final])
    assert result["quality"]["status"] == "needs-review"
    assert result["quality"]["flags"] == ["empty-output"]
    assert result["quality"]["human_reviewed"] is False
    assert result["timings_s"]["total"] == 7
    result["passes"][1]["chunks"][1]["flags"].clear()
    assert final["chunks"][1]["flags"] == ["empty-output"]


def test_each_run_retains_evidence_when_text_is_deduplicated(database, tmp_path, capsys):
    repository = TranscriptRepository(database)
    results = []
    for index in (1, 2):
        run_id = f"asr-{index}"
        repository.start_acquisition_run(AcquisitionRunRecord(
            run_id=run_id, kind="asr", selector_kind="bvid", selector_target="BVtest",
            requested_limit=1, credential_present=False, started_at=1))
        results.append(repository.record_local_transcript(
            run_id=run_id, video_part_id=1, language="zh",
            segments=(TranscriptSegmentRecord(0, 1000, "相同文本"),),
            model_name="model", model_revision=None, started_at=1, finished_at=2, created_at=2,
            asr_evidence={"schema_version": 1, "measurement": index}))
    assert results[0].transcript_id == results[1].transcript_id
    assert repository.read_asr_evidence("asr-1", 1)["measurement"] == 1
    assert repository.read_asr_evidence("asr-2", 1)["measurement"] == 2
    parser = argparse.ArgumentParser()
    add_workflow_parser(parser.add_subparsers(), archive_root=str(tmp_path))
    assert _cmd_workflow(parser.parse_args(["workflow", "asr-evidence", "--run-id", "asr-2", "--part-id", "1"])) == 0
    assert json.loads(capsys.readouterr().out)["measurement"] == 2
    assert _cmd_workflow(parser.parse_args(["workflow", "asr-evidence", "--run-id", "absent", "--part-id", "1"])) == 1


def test_cli_override_can_replace_invalid_environment(monkeypatch):
    monkeypatch.setenv("BILI_ASR_CHUNK_SECONDS", "NaN")
    assert default_config(chunk_seconds=60).chunk_seconds == 60
    with pytest.raises(ValueError):
        default_config()


def test_numeric_representation_does_not_change_profile_identity(database):
    repository = WorkflowRepository(database)
    integer = AsrProfile("stable", "model", chunk_seconds=60, tokens_per_second=8)
    floating = replace(integer, chunk_seconds=60.0, tokens_per_second=8.0)
    assert repository.register_profile(integer) == repository.register_profile(floating)
    for changes in ({"chunk_seconds": True}, {"tokens_per_second": "8"}):
        with pytest.raises(ValueError):
            repository.register_profile(replace(integer, **changes))


def test_profile_snapshot_failure_rolls_back_identity(database):
    with database:
        database.execute("""CREATE TRIGGER reject_profile_snapshot
            BEFORE INSERT ON workflow_asr_profile_configs
            BEGIN SELECT RAISE(ABORT, 'snapshot rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="snapshot rejected"):
        WorkflowRepository(database).register_profile(AsrProfile("stable", "model"))
    assert database.execute("SELECT COUNT(*) FROM workflow_asr_profiles").fetchone()[0] == 0


def test_evidence_failure_rolls_back_transcript_and_attempt(database):
    repository = TranscriptRepository(database)
    repository.start_acquisition_run(AcquisitionRunRecord(
        run_id="rollback", kind="asr", selector_kind="bvid", selector_target="BVtest",
        requested_limit=1, credential_present=False, started_at=1))
    with database:
        database.execute("""CREATE TRIGGER reject_asr_evidence
            BEFORE INSERT ON transcript_asr_evidence
            BEGIN SELECT RAISE(ABORT, 'evidence rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="evidence rejected"):
        repository.record_local_transcript(
            run_id="rollback", video_part_id=1, language="zh",
            segments=(TranscriptSegmentRecord(0, 1000, "事务检查"),),
            model_name="model", model_revision=None, started_at=1, finished_at=2,
            created_at=2, asr_evidence={"schema_version": 1})
    for table in ("asr_models", "transcripts", "transcript_segments",
                  "acquisition_attempts", "transcript_asr_evidence"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_workflow_persists_diagnostics_and_uses_frozen_parameters(database, tmp_path, monkeypatch, device):
    from bili_asr import asr
    from bili_asr.storage import AsrPolicy, JobKind
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers

    repository = WorkflowRepository(database)
    profile_id = repository.register_profile(AsrProfile(
        "quality", "model", device=device, chunk_seconds=60,
        inference_timeout_seconds=72, aligner_revision="align-commit"))
    repository.plan(part_ids=[1], policy=AsrPolicy.ALL, profile_id=profile_id)
    audio_path = tmp_path / "audio" / "test.m4a"
    audio_path.parent.mkdir()
    audio_path.write_bytes(b"fake audio; decoder is substituted")
    audio_sha256 = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    for _ in range(2):
        job = repository.claim("test", kinds=(JobKind.SUBTITLE, JobKind.AUDIO))
        repository.finish(job.job_id, worker_id="test",
                          result={"storage_key": "audio/test.m4a", "sha256": audio_sha256} if job.kind is JobKind.AUDIO else {})
    job = repository.claim("test")
    assert job.kind is JobKind.ASR
    evidence = assemble_diagnostics([{"completed": True, "decoded_s": 1, "chunks": [],
                                     "timings_s": {"total": 2}}])
    segments = [{"start": 0, "end": 1, "text": "转录"}]
    measured_coverage = {"decoded_s": 1.0, "produced_s": 1.0, "coverage": 1.0,
                         "coverage_min": .85, "coverage_short": False}
    seen = []
    fake_runner = SimpleNamespace(
        provenance=lambda: {"language": "Chinese"}, transcribed_coverage=lambda: measured_coverage,
        diagnostics=lambda: evidence, release=lambda: None)
    def make_runner(config):
        seen.append(config)
        return fake_runner
    def gpu_transcribe(config, path, *, diagnostics_sink, timeout_seconds, **kwargs):
        seen.append(config)
        assert timeout_seconds == 72
        diagnostics_sink.update(evidence)
        return segments, {"language": "Chinese"}, measured_coverage
    monkeypatch.setattr(asr, "ASRRunner", make_runner)
    monkeypatch.setattr(asr, "two_pass_transcribe", lambda *args, **kwargs: segments)
    monkeypatch.setattr(asr, "transcribe_with_timeout", gpu_transcribe)
    monkeypatch.setenv("BILI_ASR_CHUNK_SECONDS", "999")
    handlers = ArchiveWorkflowHandlers(database, repository, archive_root=tmp_path, sessdata=None)
    try:
        result = handlers.local_asr(job)
    finally:
        handlers.close()
    assert seen[0].chunk_seconds == 60
    assert seen[0].aligner_revision == "align-commit"
    stored = TranscriptRepository(database).read_asr_evidence(result["run_id"], 1)
    assert stored["config_sha256"] == repository.profile_digest(profile_id)
    assert stored["audio"]["sha256"] == audio_sha256
    assert stored["diagnostics"] == evidence
    assert result["quality"]["status"] == "usable"
    assert result["quality"]["inference_completed"] is True
    assert stored["admission"]["status"] == "usable"
