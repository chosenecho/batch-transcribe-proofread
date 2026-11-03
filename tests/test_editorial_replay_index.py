"""Issue #413: scoped replay, exact derived DDL and large-call lookup evidence."""
# ruff: noqa: F811
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import tracemalloc

import pytest

from bili_asr.deepseek import request_body
from bili_asr.editorial import EditorialConfig, canonical
from bili_asr.storage.editorial import EditorialRepository
from bili_asr.storage.editorial_replay import (
    ACCEPTED,
    FINGERPRINT,
    ReplayEvidenceError,
    completed_record,
    install_indexes,
    integrity_report,
    objects,
    require_indexes,
)
from bili_asr.storage.schema_contract import SchemaContractError
from bili_asr.storage.snapshots import (
    SnapshotDatabaseError,
    create_database_snapshot,
    validate_snapshot_database,
)
from bili_asr.storage.workflow import JobKind, WorkflowRepository
from tests.test_ai_editorial import (  # noqa: F401
    FakeClient,
    database,
    envelope,
    no_changes,
    runtime,
)


def seed(database):
    workflow = WorkflowRepository(database)
    repository = EditorialRepository(database)
    prepared = repository.prepare(1, None, EditorialConfig())
    workflow.request_editorial(video_part_id=1, input_id=prepared["input_id"])
    job = workflow.claim("replay-fixture", kinds=(JobKind.PROOFREAD,))
    chunk = prepared["chunks"][0]
    request = request_body(chunk, EditorialConfig())
    call_id = repository.begin_call(job, prepared["input_id"], chunk["chunk_id"], request)
    repository.finish_call(call_id, envelope(no_changes(chunk)))
    return repository, prepared["input_id"], chunk["chunk_id"], call_id, request


def test_large_scoped_lookup_uses_search_without_reading_unrelated_responses(database, capsys):
    repository, input_id, chunk_id, call_id, request = seed(database)
    row = database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    # 12,000 unrelated large responses. One malformed request has a searchable,
    # different scope and is located only by the explicit integrity diagnostic.
    response = canonical({"unrelated": "x" * 4096})
    with database:
        database.executemany("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)", (
            (f"unrelated-{index}", row["attempt_id"], input_id, f"unrelated-chunk-{index}",
             "malformed" if index == 0 else canonical({"request_fingerprint": "x" * 64, "request": {"model": "fixture"}}),
             response, None, 1, 2) for index in range(12_000)))
    before = database.execute("SELECT request_json,response_json FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    install_indexes(database)
    assert require_indexes(database)
    plan = [row[3] for row in database.execute(
        f"EXPLAIN QUERY PLAN SELECT call_id FROM editorial_model_calls WHERE {ACCEPTED} AND {FINGERPRINT}=? ORDER BY finished_at DESC,rowid DESC LIMIT 1",
        (repository.request_fingerprint(input_id, chunk_id, request),))]
    assert any("SEARCH" in item and "editorial_replay_fingerprint_v1" in item for item in plan), plan
    scope_plan = [row[3] for row in database.execute(
        f"EXPLAIN QUERY PLAN SELECT call_id FROM editorial_model_calls WHERE {ACCEPTED} AND input_id=? AND chunk_id=? ORDER BY finished_at DESC,rowid DESC",
        (input_id, chunk_id))]
    assert any("SEARCH" in item and "editorial_replay_scope_v1" in item for item in scope_plan), scope_plan
    statements = []
    database.set_trace_callback(statements.append)
    tracemalloc.start()
    started = time.perf_counter()
    found = repository.completed_call_record(repository.request_fingerprint(input_id, chunk_id, request), input_id=input_id, chunk_id=chunk_id)
    miss = repository.completed_call_record("f" * 64, input_id=input_id, chunk_id="absent")
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    database.set_trace_callback(None)
    assert found["call_id"] == call_id and miss is None
    response_selects = [q for q in statements if q.startswith("SELECT response_json")]
    assert len(response_selects) == 1 and call_id in response_selects[0]
    assert peak < 4_000_000
    with capsys.disabled():
        print(json.dumps({"unrelated_calls": 12_000, "unrelated_response_bytes": 12_000 * len(response.encode()),
                          "response_selects": len(response_selects), "query_count": len(statements),
                          "peak_python_bytes": peak, "elapsed_s": elapsed, "lookup_plan": plan}))
    assert integrity_report(database)["invalid_json_call_ids"] == ["unrelated-0"]
    assert tuple(before) == tuple(database.execute("SELECT request_json,response_json FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone())


def test_exact_legacy_fallback_and_duplicate_attempts_preserve_latest_semantics(database):
    repository, input_id, chunk_id, call_id, request = seed(database)
    with database:
        database.execute("UPDATE editorial_model_calls SET request_json=? WHERE call_id=?", (canonical(request), call_id))
    fingerprint = repository.request_fingerprint(input_id, chunk_id, request)
    assert completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)["call_id"] == call_id
    install_indexes(database)
    assert completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)["call_id"] == call_id
    row = database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    with database:
        database.execute("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)",
            ("newer", row["attempt_id"], input_id, chunk_id, row["request_json"], row["response_json"], None, 1, row["finished_at"] + 1))
    assert completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)["call_id"] == "newer"


def test_old_library_uses_scoped_bounded_reads_without_a_global_json_scan(database):
    repository, input_id, chunk_id, call_id, request = seed(database)
    row = database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    with database:
        database.execute("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)", (
            "other-broken", row["attempt_id"], input_id, "other-chunk", "broken-json", "broken-json", None, 1, 2))
    statements = []
    database.set_trace_callback(statements.append)
    fingerprint = repository.request_fingerprint(input_id, chunk_id, request)
    assert completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)["call_id"] == call_id
    database.set_trace_callback(None)
    request_reads = [query for query in statements if query.startswith("SELECT rowid")]
    assert len(request_reads) == 1 and "input_id=" in request_reads[0] and "chunk_id=" in request_reads[0]
    assert not any("json_extract" in query for query in statements)
    with database:
        database.executemany("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)", (
            (f"legacy-{i}", row["attempt_id"], input_id, "exhausted", row["request_json"], row["response_json"], None, 1, 2)
            for i in range(257)))
    with pytest.raises(ReplayEvidenceError) as error:
        completed_record(database, fingerprint, input_id=input_id, chunk_id="exhausted")
    assert error.value.safe_details["reason_code"] == "legacy_replay_scope_exhausted"


def test_newer_legacy_attempt_is_preferred_to_an_older_indexed_match(database):
    repository, input_id, chunk_id, call_id, request = seed(database)
    row = database.execute("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,)).fetchone()
    with database:
        database.execute("INSERT INTO editorial_model_calls VALUES (?,?,?,?,?,?,?,?,?)",
            ("newer-legacy", row["attempt_id"], input_id, chunk_id, canonical(request), row["response_json"], None,
             row["started_at"], row["finished_at"] + 1))
    install_indexes(database)
    fingerprint = repository.request_fingerprint(input_id, chunk_id, request)
    assert completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)["call_id"] == "newer-legacy"


def test_matched_bad_evidence_fails_with_call_id_but_unrelated_damage_does_not(database):
    repository, input_id, chunk_id, call_id, request = seed(database)
    fingerprint = repository.request_fingerprint(input_id, chunk_id, request)
    install_indexes(database)
    with database:
        database.execute("UPDATE editorial_model_calls SET response_json='bad-json' WHERE call_id=?", (call_id,))
    assert completed_record(database, "f" * 64, input_id=input_id, chunk_id="other") is None
    with pytest.raises(ReplayEvidenceError) as error:
        completed_record(database, fingerprint, input_id=input_id, chunk_id=chunk_id)
    assert error.value.safe_details == {"stage": "response_replay", "reason_code": "response_json_invalid", "call_id": call_id}


def test_replay_damage_is_queryable_on_the_failed_workflow_without_a_new_provider_send(database, tmp_path):
    _, _, _, call_id, _ = seed(database)
    workflow = WorkflowRepository(database)
    job_id = database.execute("SELECT a.job_id FROM workflow_attempts a JOIN editorial_model_calls c USING(attempt_id) WHERE c.call_id=?",
                              (call_id,)).fetchone()[0]
    workflow.fail(job_id, worker_id="replay-fixture", error_code="fixture-before-replay")
    workflow.requeue_failed(job_ids=[job_id])
    with database:
        database.execute("UPDATE editorial_model_calls SET response_json='bad-json' WHERE call_id=?", (call_id,))
    client = FakeClient()
    _, _, _, executor = runtime(database, tmp_path, client)
    assert executor.run().failed == 1 and client.requests == []
    row = database.execute("SELECT error_code,result_json FROM workflow_attempts WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
    assert row["error_code"] == "editorial_replay_evidence_invalid"
    assert json.loads(row["result_json"])["diagnostic"] == {
        "stage": "response_replay", "reason_code": "response_json_invalid", "call_id": call_id}
    assert database.execute("SELECT COUNT(*) FROM editorial_model_calls").fetchone()[0] == 1


def test_index_installation_rolls_back_and_explicit_partial_recovery_checks_every_ddl(database):
    seen = 0
    def interrupt():
        nonlocal seen
        seen += 1
        if seen == 2:
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        install_indexes(database, checkpoint=interrupt)
    assert not require_indexes(database)
    first = next(iter(objects().values()))
    with database:
        database.execute(first[1])
    with pytest.raises(SchemaContractError, match="incomplete"):
        require_indexes(database)
    with pytest.raises(SchemaContractError, match="incomplete"):
        install_indexes(database)
    assert install_indexes(database, recover=True)["installed"]
    name = next(iter(objects()))
    with database:
        database.execute(f"DROP INDEX {name}")
        database.execute(f"CREATE INDEX {name} ON editorial_model_calls(call_id)")
    with pytest.raises(SchemaContractError, match="altered"):
        install_indexes(database, recover=True)


def test_snapshot_retains_indexes_but_content_contract_fingerprint_is_stable(database, tmp_path):
    source = tmp_path / "archive.db"
    original_contract = validate_snapshot_database(source)
    install_indexes(database)
    assert validate_snapshot_database(source) == original_contract
    target = tmp_path / "snapshot.db"
    assert create_database_snapshot(source, target) == original_contract
    with sqlite3.connect(target) as snapshot:
        assert require_indexes(snapshot)
    name = next(iter(objects()))
    with database:
        database.execute(f"DROP INDEX {name}")
    with pytest.raises(SnapshotDatabaseError, match="incomplete"):
        validate_snapshot_database(source)


def test_killed_index_installation_leaves_no_half_group_and_can_be_restarted(database, tmp_path):
    ready = tmp_path / "first-index-installed"
    script = """
import sqlite3, sys
from pathlib import Path
from bili_asr.storage.editorial_replay import install_indexes
seen = 0
def checkpoint():
    global seen
    seen += 1
    if seen == 2:
        Path(sys.argv[2]).write_text('transaction still open', encoding='utf-8')
        sys.stdin.read()
with sqlite3.connect(sys.argv[1]) as connection:
    install_indexes(connection, checkpoint=checkpoint)
"""
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, str(tmp_path / "archive.db"), str(ready)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), process.communicate(timeout=2)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert not require_indexes(database)
    assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert install_indexes(database)["installed"] and require_indexes(database)


def test_upgrade_target_indexes_survive_full_snapshot_check_and_restore(tmp_path):
    from bili_asr.contracts.registry import UNIVERSAL_V2
    from bili_asr.services.archive_snapshot import (
        check_snapshot,
        restore_snapshot,
        save_snapshot,
    )
    from bili_asr.services.archive_upgrade import apply_upgrade, plan_upgrade
    from bili_asr.storage.database import connect_database
    from tests.fixtures.frozen_migration_archive import frozen_archive
    source, target = tmp_path / "frozen", tmp_path / "target"
    frozen_archive(source)
    before = (source / "archive.db").read_bytes()
    plan = plan_upgrade(source, target, target_contracts=(UNIVERSAL_V2,), no_external_control_state=True)
    assert apply_upgrade(plan)["valid"]
    fingerprint = validate_snapshot_database(target / "archive.db")
    with connect_database(target / "archive.db") as connection:
        assert install_indexes(connection)["installed"]
    assert validate_snapshot_database(target / "archive.db") == fingerprint
    bundle = tmp_path / "snapshot.zip"
    save_snapshot(target, bundle)
    assert check_snapshot(bundle)["valid"]
    restored = tmp_path / "restored"
    restore_snapshot(bundle, restored)
    with sqlite3.connect(restored / "archive.db") as connection:
        assert require_indexes(connection)
    assert (source / "archive.db").read_bytes() == before
