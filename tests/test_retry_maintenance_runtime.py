"""Run the installed maintenance scheduler against real workflow persistence."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from bili_asr.deepseek import DeepSeekTransportError
from bili_asr.editorial import EditorialConfig
from bili_asr.services.retry_maintenance import health
from bili_asr.services.retry_maintenance_io import MaintenanceBusyError, maintenance_lock, read_json, write_json
from bili_asr.storage import open_database
from bili_asr.workflow_models import JobKind
from tests.test_ai_editorial import database, envelope, no_changes, runtime
from tests.test_retry_maintenance_scheduler import RUNNER, wait_for


pytestmark = pytest.mark.skipif(os.name == "nt", reason="container scheduling requires POSIX")


def control_files(root, job_id):
    directory = root / "ops"
    directory.mkdir()
    files = {name: directory / (name + ".json") for name in ("policy", "state", "health")}
    files["lock"] = directory / "maintenance.lock"
    write_json(files["policy"], {
        "schema_version": 1, "job_ids": [job_id], "part_ids": [], "kinds": ["proofread"],
        "retryable_errors": {"proofread": ["deepseek_transport_failed"]},
        "max_retries": 2, "delays_seconds": [0, 600], "interval_seconds": 1,
    })
    write_json(files["state"], {"retry_holds": {}, "retry_ledger": {}, "scope_note": "acceptance only"})
    return files


@contextmanager
def scheduled(root, files):
    command = [sys.executable, str(RUNNER), "--bin", str(Path(sys.executable).with_name("bili-asr")),
               "--archive-root", str(root)]
    for name, path in files.items():
        command.extend(["--" + name, str(path)])
    with (root / "scheduler.log").open("a") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            assert process.returncode == 0, (root / "scheduler.log").read_text()


def two_chunks(connection):
    with connection:
        connection.execute("INSERT INTO transcript_segments VALUES (1,2,2000,2900,'第三段文字')")
        connection.execute("INSERT INTO transcript_segments VALUES (1,3,3000,3900,'第四段文字')")
        # This fixture substitutes a four-second decoder. Its original two-cue
        # attestation must describe the expanded inference before it is frozen.
        connection.execute("UPDATE transcript_coverage_attestations SET decoded_s=3.9,produced_s=3.9,"
                           "coverage=1,coverage_short=0 WHERE transcript_id=1")
        row = connection.execute("SELECT evidence_json FROM transcript_asr_evidence WHERE transcript_id=1").fetchone()
        evidence = json.loads(row[0])
        evidence["audio"]["duration_ms"] = 3900
        connection.execute("UPDATE transcript_asr_evidence SET evidence_json=? WHERE transcript_id=1", (json.dumps(evidence),))


class CheckpointClient:
    def __init__(self, failure=None):
        self.failure = failure
        self.requests = []

    def complete(self, request, config):
        self.requests.append(deepcopy(request))
        chunk = json.loads(request["messages"][1]["content"])
        value = no_changes(chunk)
        if len(self.requests) == 2:
            if self.failure == "transport":
                raise DeepSeekTransportError("offline transport failure", error_code="deepseek_transport_failed")
            if self.failure == "reference":
                value["paragraphs"][0]["issues"] = [{"note": "核对", "candidate": "",
                    "evidence_refs": [chunk["editable_segments"][1]["segment_id"]]}]
        return envelope(value)


def test_scheduled_transport_recovery_preserves_checkpoint_and_dependency(database, tmp_path):
    two_chunks(database)
    client = CheckpointClient("transport")
    workflow, editorial, _, executor = runtime(database, tmp_path, client)
    prepared = editorial.prepare(1, None, EditorialConfig(max_chunk_segments=2, context_segments=0))
    proof, render, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    with database:
        old_render, _ = workflow._ensure_job(kind=JobKind.RENDER_DOCUMENT, video_part_id=1,
            profile_id=None, policy_key=None,
            payload={"proofread_job_id": proof, "template_version": "ai-draft-v1"},
            dedupe_key=f"historical-render:{proof}:ai-draft-v1")
    workflow.cancel(job_ids=[old_render])
    assert executor.run().failed == 1 and len(client.requests) == 2
    assert len(prepared["chunks"]) == 2
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0
    assert workflow.explain_job(render)["attempt_count"] == 0
    assert workflow.claim("blocked-render", kinds=[JobKind.RENDER_DOCUMENT]) is None
    prior_calls = [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls ORDER BY rowid")]
    files = control_files(tmp_path, proof)
    with scheduled(tmp_path, files) as process:
        try:
            wait_for(lambda: workflow.explain_job(proof)["status"] == "queued"
                     and files["health"].exists()
                     and read_json(files["health"]).get("last_succeeded_at") is not None, process)
        except AssertionError as exc:
            raise AssertionError((tmp_path / "scheduler.log").read_text()) from exc
        assert read_json(files["state"])["retry_ledger"][proof]["retries"] == 1
    # Restart both scheduler and worker, keeping only durable database/control state.
    prior_success = read_json(files["health"])["last_succeeded_at"]
    with scheduled(tmp_path, files) as process:
        wait_for(lambda: read_json(files["health"]).get("last_succeeded_at", 0) > prior_success, process)
        with open_database(tmp_path) as connection:
            resumed_client = CheckpointClient()
            resumed_workflow, resumed_editorial, _, resumed_executor = runtime(connection, tmp_path, resumed_client)
            summary = resumed_executor.run()
            assert summary.succeeded == 2 and summary.failed == 0
            assert len(resumed_client.requests) == 1
            assert json.loads(resumed_client.requests[0]["messages"][1]["content"])["chunk_id"] == prepared["chunks"][1]["chunk_id"]
            assert resumed_editorial.revision_for_job(proof)
            assert resumed_workflow.explain_job(old_render)["status"] == "cancelled"
            assert resumed_workflow.explain_job(old_render)["attempt_count"] == 0
    assert workflow.explain_job(proof)["attempt_count"] == 2
    assert workflow.explain_job(render)["status"] == "succeeded"
    assert read_json(files["state"])["retry_ledger"][proof]["retries"] == 1
    assert read_json(files["state"])["scope_note"] == "acceptance only"
    assert prior_calls == [tuple(row) for row in database.execute("SELECT * FROM editorial_model_calls ORDER BY rowid LIMIT 2")]
    assert len(editorial.physical_calls(prepared["input_id"], prepared["chunks"][0]["chunk_id"])) == 1


def test_bad_reference_remains_failed_with_original_response_and_blocked_render(database, tmp_path):
    two_chunks(database)
    client = CheckpointClient("reference")
    workflow, editorial, _, executor = runtime(database, tmp_path, client)
    legacy_config = EditorialConfig.from_snapshot({"max_chunk_segments": 2, "context_segments": 0})
    prepared = editorial.prepare(1, None, legacy_config)
    proof, render, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    assert executor.run().failed == 1
    assert workflow.explain_job(proof)["error_code"] == "RevisionValidationError"
    assert database.execute("SELECT COUNT(*) FROM editorial_revisions").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_chunk_results").fetchone()[0] == 1
    failed_call = database.execute("SELECT * FROM editorial_model_calls WHERE error_code='RevisionValidationError'").fetchone()
    assert failed_call["response_json"] is not None
    files = control_files(tmp_path, proof)
    with scheduled(tmp_path, files) as process:
        wait_for(lambda: 'error_not_allowlisted' in (tmp_path / "scheduler.log").read_text(), process)
    assert workflow.explain_job(proof)["status"] == "failed"
    assert workflow.explain_job(proof)["attempt_count"] == 1
    assert workflow.explain_job(render)["attempt_count"] == 0
    assert read_json(files["state"])["retry_ledger"] == {}
    assert tuple(failed_call) == tuple(database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (failed_call["call_id"],)).fetchone())
    assert 'error_not_allowlisted' in (tmp_path / "scheduler.log").read_text()


def test_scheduled_preflight_failure_lock_contention_and_stop_are_distinguishable(database, tmp_path):
    workflow, editorial, _, _ = runtime(database, tmp_path, CheckpointClient())
    prepared = editorial.prepare(1, None, EditorialConfig())
    proof, _, _, _ = workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    files = control_files(tmp_path, proof)
    worker_health = tmp_path / "workers.json"
    write_json(worker_health, {"status": "alive"})
    write_json(files["state"], {"retry_ledger": {}})
    with scheduled(tmp_path, files) as process:
        wait_for(lambda: files["health"].exists() and read_json(files["health"]).get("status") == "failed", process)
        report = health(health_path=files["health"], stale_after_seconds=1, worker_health_path=worker_health)
        assert report["maintenance"]["health_status"] == "failed"
        assert report["workers"]["status"] == "alive"
        assert report["maintenance"]["error_code"] == "ValueError"
        write_json(files["state"], {"retry_holds": {}, "retry_ledger": {}})
        wait_for(lambda: read_json(files["health"]).get("status") == "succeeded", process)
        # A published success can overlap the next scheduler pass. Acquire the
        # test's lock between passes instead of assuming the child is idle.
        with ExitStack() as held_lock:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "scheduler exited before lock contention check"
                try:
                    held_lock.enter_context(maintenance_lock(files["lock"]))
                    break
                except MaintenanceBusyError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("could not acquire maintenance test lock within 10 seconds") from None
                    time.sleep(0.01)
            success = read_json(files["health"])["last_succeeded_at"]
            wait_for(lambda: read_json(files["health"]).get("lock_skip_count", 0) >= 2, process)
            report = health(health_path=files["health"], stale_after_seconds=1, worker_health_path=worker_health)
            assert report["maintenance"]["last_succeeded_at"] == success
            assert report["maintenance"]["health_status"] == "stale"
            assert report["maintenance"]["last_lock_skipped_at"] >= success
        wait_for(lambda: read_json(files["health"])["last_succeeded_at"] > success, process)
    success = read_json(files["health"])["last_succeeded_at"]
    time.sleep(2.1)
    assert read_json(files["health"])["last_succeeded_at"] == success
    assert health(health_path=files["health"], stale_after_seconds=1,
                  worker_health_path=worker_health)["maintenance"]["health_status"] == "stale"
