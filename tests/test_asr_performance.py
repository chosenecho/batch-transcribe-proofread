from __future__ import annotations

import json
import sqlite3

import pytest

from bili_asr.services.asr_performance import asr_performance_report
from bili_asr.storage.asr_performance import performance_attempts


def _database(tmp_path):
    connection = sqlite3.connect(tmp_path / "report.db")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE workflow_asr_profiles(profile_id INTEGER PRIMARY KEY, config_sha256 TEXT);
        CREATE TABLE workflow_jobs(job_id TEXT PRIMARY KEY, kind TEXT, profile_id INTEGER, video_part_id INTEGER, status TEXT);
        CREATE TABLE workflow_attempts(
            attempt_id TEXT PRIMARY KEY, job_id TEXT, started_at INTEGER, finished_at INTEGER,
            outcome TEXT, result_json TEXT
        );
        CREATE TABLE transcript_asr_evidence(
            run_id TEXT, video_part_id INTEGER, transcript_id INTEGER, evidence_json TEXT
        );
        CREATE TABLE acquisition_runs(run_id TEXT, kind TEXT, outcome TEXT, started_at INTEGER, finished_at INTEGER);
    """)
    connection.execute("INSERT INTO workflow_asr_profiles VALUES (1, ?)", ("a" * 64,))
    connection.execute("INSERT INTO workflow_jobs VALUES ('j1', 'asr', 1, 1, 'succeeded')")
    evidence = {
        "schema_version": 1,
        "audio": {"sha256": "b" * 64, "duration_ms": 10000},
        "runtime_binding": {"model": "m"},
        "diagnostics": {"passes": [{"prefetch": {"submitted": 1, "consumed": 1,
            "discarded": 0, "input_wait_s": 0.25, "fallback_counts": {}}}]},
    }
    connection.execute("INSERT INTO acquisition_runs VALUES ('r1', 'asr', 'complete', 100, 112)")
    connection.execute("INSERT INTO transcript_asr_evidence VALUES ('r1', 1, 4, ?)",
                       (json.dumps(evidence),))
    connection.execute("INSERT INTO workflow_attempts VALUES ('a1', 'j1', 101, 112, 'succeeded', ?)",
                       (json.dumps({"run_id": "r1"}),))
    connection.commit()
    return connection


def test_report_counts_unique_audio_and_prefetch_without_writing(tmp_path):
    connection = _database(tmp_path)
    before = (connection.total_changes, connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0])
    report = asr_performance_report(connection, start=100, end=120)
    assert report["totals"]["unique_success_audio_s"] == 10
    assert report["totals"]["audio_s_per_wall_s"] == 0.5
    assert report["totals"]["prefetch_observed_totals"]["submitted"] == 1
    assert report["totals"]["successful_attempt_latency_s"]["p50"] == 11
    assert (connection.total_changes, connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0]) == before


def test_report_marks_missing_and_boundary_evidence_unknown(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_jobs VALUES ('j2', 'asr', 1, 2, 'succeeded')")
    connection.execute("INSERT INTO workflow_attempts VALUES ('a2', 'j2', 90, 130, 'succeeded', '{}')")
    connection.commit()
    report = asr_performance_report(connection, start=100, end=120)
    totals = report["totals"]
    assert totals["excluded_boundary_success_attempts"] == 1
    assert totals["unknown_success_audio_attempts"] == 0
    assert totals["evidence_counts"]["missing"] == 1


def test_report_rejects_unbounded_attempt_scan_and_invalid_window(tmp_path):
    connection = _database(tmp_path)
    with pytest.raises(ValueError, match="window"):
        asr_performance_report(connection, start=2, end=2)
    with pytest.raises(ValueError, match="max_attempts"):
        asr_performance_report(connection, start=100, end=120, max_attempts=0)
    with pytest.raises(ValueError, match="limit exceeded"):
        performance_attempts(connection, 100, 120, 0)


def test_parallel_two_pass_and_retry_cost_use_common_window_and_unique_audio(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_attempts VALUES ('failed', 'j1', 95, 100, 'failed', '{}')")
    connection.execute("INSERT INTO workflow_attempts VALUES ('retry', 'j1', 113, 119, 'succeeded', ?)",
                       (json.dumps({"run_id": "r1"}),))
    connection.execute("INSERT INTO workflow_jobs VALUES ('j2', 'asr', 1, 2, 'succeeded')")
    evidence = {"schema_version": 1, "audio": {"sha256": "c" * 64, "duration_ms": 20000},
                "diagnostics": {"passes": [{}, {}]}}
    connection.execute("INSERT INTO acquisition_runs VALUES ('r2', 'asr', 'complete', 103, 116)")
    connection.execute("INSERT INTO transcript_asr_evidence VALUES ('r2', 2, 5, ?)", (json.dumps(evidence),))
    connection.execute("INSERT INTO workflow_attempts VALUES ('parallel', 'j2', 103, 116, 'succeeded', ?)",
                       (json.dumps({"run_id": "r2"}),))
    report = asr_performance_report(connection, start=90, end=120)
    totals = report["totals"]
    assert totals["unique_success_audio_s"] == 30
    assert totals["audio_s_per_wall_s"] == 1
    assert totals["overlapping_attempt_wall_s"] == 35
    assert totals["failure_cancelled_attempt_wall_s"] == 5
    assert totals["retry_attempts"] == 2
    assert totals["retry_attempt_wall_s"] == 17
    assert totals["pass_counts"] == {"single": 2, "multiple": 1, "unknown": 0}
    assert totals["terminal_attempt_success_rate"] == 0.75


def test_missing_success_audio_is_unknown_not_zero_throughput(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_jobs VALUES ('j2', 'asr', 1, 2, 'succeeded')")
    connection.execute("INSERT INTO workflow_attempts VALUES ('a2', 'j2', 105, 115, 'succeeded', '{}')")
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["audio_s_per_wall_s"] is None
    assert totals["known_audio_s_per_wall_s_lower_bound"] == 0.5
    assert totals["unknown_success_audio_attempts"] == 1


def _add_success(connection, name, digest, duration_ms, *, model="m"):
    part = connection.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] + 1
    connection.execute("INSERT INTO workflow_jobs VALUES (?, 'asr', 1, ?, 'succeeded')", (name, part))
    evidence = {"schema_version": 1, "audio": {"sha256": digest, "duration_ms": duration_ms},
                "runtime_binding": {"model": model}, "diagnostics": {"passes": [{}]}}
    connection.execute("INSERT INTO transcript_asr_evidence VALUES (?, ?, ?, ?)",
                       (name, part, part, json.dumps(evidence)))
    connection.execute("INSERT INTO workflow_attempts VALUES (?, ?, 102, 114, 'succeeded', ?)",
                       (name, name, json.dumps({"run_id": name})))


@pytest.mark.parametrize("duration_ms, expected_audio_s, expected_unknown", [(10000, 10, 0), (20000, 0, 2)])
def test_repeated_audio_identity_keeps_only_consistent_duration_credit(
        tmp_path, duration_ms, expected_audio_s, expected_unknown):
    connection = _database(tmp_path)
    _add_success(connection, "repeat", "b" * 64, duration_ms)
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["unique_success_audio_s"] == expected_audio_s
    assert totals["known_unique_audio_count"] == int(expected_unknown == 0)
    assert totals["unknown_success_audio_attempts"] == expected_unknown
    assert totals["conflicting_success_audio_attempts"] == expected_unknown
    assert totals["conflicting_audio_identity_count"] == int(expected_unknown > 0)
    assert totals["audio_credit_complete"] is (expected_unknown == 0)
    assert totals["audio_s_per_wall_s"] == (0.5 if expected_unknown == 0 else None)


@pytest.mark.parametrize("third_duration", [10000, 20000, 30000])
def test_duration_conflicts_invalidate_all_attempts_and_groups_order_independently(
        tmp_path, monkeypatch, third_duration):
    import bili_asr.services.asr_performance as service
    connection = _database(tmp_path)
    _add_success(connection, "conflict", "b" * 64, 20000, model="other")
    _add_success(connection, "third", "b" * 64, third_duration)
    _add_success(connection, "reliable", "c" * 64, 30000, model="reliable")
    _add_success(connection, "duplicate", "c" * 64, 30000, model="other")
    rows = performance_attempts(connection, 100, 120, 100)
    reports = []
    for ordered in (rows, list(reversed(rows)), rows[2:] + rows[:2]):
        monkeypatch.setattr(service, "performance_attempts", lambda *args: ordered)
        reports.append(service.asr_performance_report(connection, start=100, end=120))
    assert reports[0] == reports[1] == reports[2]
    report = reports[0]
    totals = report["totals"]
    assert totals["unique_success_audio_s"] == 30
    assert totals["known_unique_audio_count"] == 1
    assert totals["conflicting_audio_identity_count"] == 1
    assert totals["conflicting_success_audio_attempts"] == 3
    assert totals["unknown_success_audio_attempts"] == 3
    assert totals["audio_credit_complete"] is False
    assert totals["audio_s_per_wall_s"] is None
    assert totals["known_audio_s_per_wall_s_lower_bound"] == 1.5
    assert totals["attempt_outcomes"]["succeeded"] == 5
    assert totals["overlapping_attempt_wall_s"] == 59
    assert totals["successful_attempt_latency_s"]["samples"] == 5
    assert totals["pass_counts"] == {"single": 5, "multiple": 0, "unknown": 0}
    affected = [group for group in report["groups"] if group["conflicting_audio_identity_count"]]
    assert sorted(group["unknown_success_audio_attempts"] for group in affected) == [1, 2]
    assert all(group["audio_s_per_wall_s"] is None for group in affected)
    reliable = [group for group in report["groups"] if group["audio_credit_complete"]]
    assert len(reliable) == 1
    assert reliable[0]["audio_s_per_wall_s"] == 1.5
    # Groups deduplicate within each configuration; they are explicitly nonadditive.
    assert sum(group["unique_success_audio_s"] for group in report["groups"]) == 60
    assert "not_additive" in report["accounting"]["group_audio_credit"]


def test_conflict_domain_excludes_boundary_attempts_and_combines_missing_audio(tmp_path):
    connection = _database(tmp_path)
    _add_success(connection, "boundary", "b" * 64, 20000)
    connection.execute("UPDATE workflow_attempts SET started_at=90 WHERE attempt_id='boundary'")
    _add_success(connection, "missing", "d" * 64, None)
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["unique_success_audio_s"] == 10
    assert totals["conflicting_audio_identity_count"] == 0
    assert totals["excluded_boundary_success_attempts"] == 1
    assert totals["unknown_success_audio_attempts"] == 1
    assert totals["audio_credit_complete"] is False


def test_cli_emits_valid_incomplete_report_for_conflicting_projected_evidence(
        tmp_path, monkeypatch, capsys):
    import bili_asr.services.asr_performance as service
    from bili_asr.cli import main
    from bili_asr.storage import open_database
    connection = _database(tmp_path)
    _add_success(connection, "conflict", "b" * 64, 20000, model="other")
    _add_success(connection, "third", "b" * 64, 10000)
    _add_success(connection, "reliable", "c" * 64, 30000)
    rows = performance_attempts(connection, 100, 120, 100)
    connection.close()
    root = tmp_path / "archive"
    open_database(root).close()
    monkeypatch.setattr(service, "performance_attempts", lambda *args: rows)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert main(["workflow", "asr-performance", "--archive-root", str(root),
                 "--start", "100", "--end", "120"]) == 0
    totals = json.loads(capsys.readouterr().out)["totals"]
    assert totals["audio_credit_complete"] is False
    assert totals["audio_s_per_wall_s"] is None
    assert totals["unique_success_audio_s"] == 30
    assert totals["conflicting_success_audio_attempts"] == 3
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_job_success_rate_counts_a_retry_chain_once_and_keeps_queued_nonterminal(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_attempts VALUES ('prior', 'j1', 100, 101, 'failed', '{}')")
    connection.execute("INSERT INTO workflow_jobs VALUES ('j2', 'asr', 1, 2, 'queued')")
    connection.execute("INSERT INTO workflow_attempts VALUES ('deferred', 'j2', 101, 103, 'failed', '{}')")
    connection.execute("INSERT INTO workflow_jobs VALUES ('j3', 'asr', 1, 3, 'failed')")
    connection.execute("INSERT INTO workflow_attempts VALUES ('terminal', 'j3', 101, 105, 'failed', '{}')")
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["terminal_attempt_success_rate"] == 0.25
    assert totals["terminal_asr_job_success_rate_at_snapshot"] == 0.5
    assert totals["unique_asr_job_status_at_snapshot"] == {
        "queued": 1, "running": 0, "succeeded": 1, "failed": 1, "cancelled": 0}


def test_projection_does_not_copy_transcript_or_credentials_and_legacy_reason_stays_unknown(tmp_path):
    connection = _database(tmp_path)
    evidence = {"schema_version": 1, "audio": {"sha256": "b" * 64, "duration_ms": 10000},
                "cookie": "private-cookie", "diagnostics": {"passes": [{
                    "chunks": [{"text": "private-transcript"}],
                    "prefetch": {"submitted": 2, "fallback": "private-error"}}]}}
    connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(evidence),))
    rows = performance_attempts(connection, 100, 120, 10)
    assert "private-transcript" not in rows[0]["evidence_json"]
    assert "private-cookie" not in rows[0]["evidence_json"]
    report = asr_performance_report(connection, start=100, end=120)
    assert "private-error" not in json.dumps(report)
    assert report["totals"]["prefetch_legacy_passes_without_reason_counts"] == 1
    assert report["totals"]["prefetch_fallback_counts"] == {}


def test_window_excludes_finished_before_start_and_accepts_finish_at_end(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_attempts VALUES ('old', 'j1', 90, 100, 'failed', '{}')")
    connection.execute("UPDATE workflow_attempts SET finished_at=120 WHERE attempt_id='a1'")
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["attempt_outcomes"]["failed"] == 0
    assert totals["unique_success_audio_s"] == 10
    assert totals["excluded_boundary_success_attempts"] == 0


def test_real_archive_cli_is_readonly_and_missing_archive_is_not_created(tmp_path, capsys):
    from bili_asr.cli import main
    from bili_asr.storage import open_database
    root = tmp_path / "archive"
    connection = open_database(root)
    connection.close()
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    args = ["workflow", "asr-performance", "--archive-root", str(root), "--start", "100", "--end", "120"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["unique_success_audio_s"] == 0
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert after == before
    missing = tmp_path / "missing"
    args[3] = str(missing)
    assert main(args) == 1
    assert not missing.exists()


def test_report_uses_one_snapshot_while_writer_finishes_another_run(tmp_path, monkeypatch):
    import bili_asr.services.asr_performance as service
    connection = _database(tmp_path)
    connection.execute("PRAGMA journal_mode=WAL")
    original = service.performance_attempts
    def concurrent_finish(*args):
        rows = original(*args)
        with sqlite3.connect(tmp_path / "report.db") as writer:
            writer.execute("INSERT INTO acquisition_runs VALUES ('later', 'asr', 'failed', 101, 118)")
        return rows
    monkeypatch.setattr(service, "performance_attempts", concurrent_finish)
    report = service.asr_performance_report(connection, start=100, end=120)
    assert report["acquisition_run_outcomes"] == {"complete": 1}
    assert not connection.in_transaction
    assert connection.execute("SELECT COUNT(*) FROM acquisition_runs").fetchone()[0] == 2
    connection.close()


def test_report_preserves_callers_uncommitted_transaction_and_limit_failure(tmp_path):
    connection = _database(tmp_path)
    connection.execute("INSERT INTO workflow_attempts VALUES ('failure', 'j1', 113, 115, 'failed', '{}')")
    assert connection.in_transaction
    with pytest.raises(ValueError, match="limit exceeded"):
        asr_performance_report(connection, start=100, end=120, max_attempts=1)
    assert connection.in_transaction
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["attempt_outcomes"]["failed"] == 1
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM workflow_attempts").fetchone()[0] == 1
    connection.execute("INSERT INTO workflow_attempts VALUES ('failure', 'j1', 113, 115, 'failed', '{}')")
    connection.commit()
    with pytest.raises(ValueError, match="limit exceeded"):
        asr_performance_report(connection, start=100, end=120, max_attempts=1)
    assert not connection.in_transaction


@pytest.mark.parametrize("body", ['{"schema_version":2}', '[]', '{"schema_version":1,"audio":null}', 'invalid-json'])
def test_unsupported_or_malformed_evidence_is_unknown_without_traceback(tmp_path, body):
    connection = _database(tmp_path)
    connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (body,))
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["audio_s_per_wall_s"] is None
    assert totals["unknown_success_audio_attempts"] == 1


def test_projection_preserves_unknown_schema_and_pass_shapes(tmp_path):
    connection = _database(tmp_path)
    evidence = {"schema_version": True, "audio": {"sha256": "b" * 64, "duration_ms": 10000},
                "diagnostics": {"passes": [{}, "broken-pass"]}}
    connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(evidence),))
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["audio_s_per_wall_s"] is None
    assert totals["evidence_counts"]["invalid_or_unsupported"] == 1
    evidence["schema_version"] = 1
    connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(evidence),))
    totals = asr_performance_report(connection, start=100, end=120)["totals"]
    assert totals["pass_counts"] == {"single": 0, "multiple": 0, "unknown": 1}
    evidence["audio"]["duration_ms"] = 10 ** 400
    connection.execute("UPDATE transcript_asr_evidence SET evidence_json=?", (json.dumps(evidence),))
    assert asr_performance_report(connection, start=100, end=120)["totals"]["audio_s_per_wall_s"] is None


def test_real_workflow_asr_evidence_flows_into_readonly_report(tmp_path, monkeypatch, capsys):
    import hashlib
    import time
    from bili_asr.cli import main
    from bili_asr.storage import AsrProfile, AsrPolicy, JobKind, WorkflowRepository, open_database
    from bili_asr.workflow_runtime import ArchiveWorkflowHandlers
    from test_workflow_control_plane import _seed_part
    from test_asr_qwen import _runner
    connection = open_database(tmp_path)
    try:
        part = _seed_part(connection)
        repository = WorkflowRepository(connection)
        profile = repository.register_profile(AsrProfile("perf", "model", device="cpu"))
        repository.plan(part_ids=[part], policy=AsrPolicy.ALL, profile_id=profile)
        audio = tmp_path / "audio" / "fixture.m4a"
        audio.parent.mkdir()
        audio.write_bytes(b"verified fixture with fake models")
        digest = hashlib.sha256(audio.read_bytes()).hexdigest()
        for kind in (JobKind.SUBTITLE, JobKind.AUDIO):
            dependency = repository.claim("test", kinds=(kind,))
            repository.finish(dependency.job_id, worker_id="test",
                result={"storage_key": "audio/fixture.m4a", "sha256": digest, "duration_ms": 3000})
        # This fixture's aligner must describe each one-second decoded chunk.
        # The generic runner double uses 0.4 s per character and overshoots a
        # short chunk; such evidence correctly fails structural admission.
        monkeypatch.setattr("test_asr_qwen._units", lambda text: [
            {"text": char, "start_time": index / len(text), "end_time": (index + 1) / len(text)}
            for index, char in enumerate(text)])
        runner, _ = _runner(monkeypatch, text="test", chunk_seconds=1)
        handlers = ArchiveWorkflowHandlers(connection, repository, archive_root=tmp_path, sessdata=None,
            runner_factory=lambda config: runner, asr_prefetch=True)
        started = int(time.time()) - 1
        job = repository.claim("test", kinds=(JobKind.ASR,))
        try:
            result = handlers.local_asr(job)
            repository.finish(job.job_id, worker_id="test", result=result)
        finally:
            handlers.close()
    finally:
        connection.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert main(["workflow", "asr-performance", "--archive-root", str(tmp_path),
                 "--start", str(started), "--end", str(int(time.time()) + 1)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["unique_success_audio_s"] == 3
    assert report["totals"]["prefetch_observed_totals"]["consumed"] == 2
    assert report["totals"]["pass_counts"]["single"] == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
