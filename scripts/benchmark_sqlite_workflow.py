"""Compare DELETE/WAL using real claims, heartbeat fencing and terminal commits; no API calls."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time

from bili_asr.storage.database import connect_database, open_database
from bili_asr.storage.snapshots import create_database_snapshot
from bili_asr.storage.workflow import WorkflowRepository


def _worker(database, worker, delay, representative=False):
    timings = {"claim_ms": [], "heartbeat_ms": [], "commit_ms": [], "api_wait_ms": []}
    claims = []
    errors = []
    with closing(connect_database(database)) as connection:
        repository = WorkflowRepository(connection)
        while True:
            try:
                started = time.perf_counter()
                if representative:
                    candidate = connection.execute("SELECT * FROM workflow_jobs WHERE job_id LIKE 'job-%' AND status='queued' ORDER BY job_id LIMIT 1").fetchone()
                    job = repository.claim(worker, expected_candidate=repository._job_from_row(candidate)) if candidate else None
                else:
                    job = repository.claim(worker)
                timings["claim_ms"].append((time.perf_counter() - started) * 1000)
                if job is None:
                    if representative and candidate is not None:
                        continue  # Another worker won this candidate; keep competing.
                    break
                claims.append(job.job_id)
                started = time.perf_counter()
                time.sleep(delay)
                timings["api_wait_ms"].append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                repository.renew_lease(job, lease_seconds=900)
                timings["heartbeat_ms"].append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                if representative:
                    from bili_asr.storage.editorial import EditorialRepository
                    evidence = EditorialRepository(connection)
                    template = connection.execute("SELECT input_id,chunk_id FROM editorial_model_calls WHERE response_json IS NOT NULL LIMIT 1").fetchone()
                    call = evidence.begin_call(job, template["input_id"], template["chunk_id"],
                        {"synthetic_benchmark": True, "request": "x"*4096})
                    evidence.finish_call(call, {"synthetic_benchmark": True, "response": "x"*16384})
                repository.finish(job.job_id, worker_id=worker, expected_attempt_count=job.attempt_count,
                                  result={"synthetic": True, "response": "x" * 4096})
                timings["commit_ms"].append((time.perf_counter() - started) * 1000)
            except sqlite3.OperationalError as error:
                errors.append({"sqlite_code": getattr(error, "sqlite_errorname", None)})
                break
    return {"timings": timings, "claims": claims, "errors": errors}


def _crash_writer(database):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE crash_probe(value TEXT)")
        connection.execute("INSERT INTO crash_probe VALUES ('committed')")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO crash_probe VALUES ('uncommitted')")
        os._exit(23)


def _summary(values):
    ordered = sorted(values)
    return {"count": len(ordered), "p50": statistics.median(ordered) if ordered else None,
            "p95": ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .95))] if ordered else None,
            "total": sum(ordered)}


def benchmark(directory: Path, *, workers: int, jobs: int, delay: float, mode: str,
              source_database: Path | None = None) -> dict:
    if mode not in {"DELETE", "WAL"} or not 1 <= workers <= 64 or jobs < workers or delay < 0:
        raise ValueError("invalid benchmark mode or workload")
    directory.mkdir()  # Refuse reuse/overwrite and never target a production directory.
    database = directory / "archive.db"
    if source_database is not None:
        create_database_snapshot(source_database, database)
    with closing(open_database(database)) as connection:
        connection.execute(f"PRAGMA journal_mode={mode}")
        if connection.execute("SELECT 1 FROM workflow_jobs WHERE job_id LIKE 'job-%' LIMIT 1").fetchone():
            raise ValueError("benchmark namespace already exists in source")
        connection.executemany(
            "INSERT INTO workflow_jobs(job_id,kind,dedupe_key,payload_json,status,available_at,created_at,updated_at) "
            "VALUES (?,'index',?,?,'queued',0,0,0)",
            [(f"job-{i:08d}", f"benchmark-{i}", json.dumps({"schema_version": 1, "padding": "x" * 4096}))
             for i in range(jobs)])
        connection.commit()
    context = multiprocessing.get_context("spawn")
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        results = list(pool.map(_worker, [str(database)] * workers,
                                [f"worker-{i}" for i in range(workers)], [delay] * workers,
                                [source_database is not None] * workers))
    elapsed = time.perf_counter() - started
    claims = [job for result in results for job in result["claims"]]
    timings = {name: _summary([v for result in results for v in result["timings"][name]])
               for name in results[0]["timings"]}
    with closing(sqlite3.connect(database)) as connection:
        attempts = connection.execute("SELECT count(*) FROM workflow_attempts WHERE outcome='succeeded' AND job_id LIKE 'job-%'").fetchone()[0]
        # Pin a real read snapshot, then commit a writer beyond its WAL end mark.
        with closing(sqlite3.connect(database)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM workflow_jobs").fetchone()
            checkpoint_ms = None
            checkpoint = None
            if mode == "WAL":
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE wal_probe(value INTEGER)")
                connection.execute("INSERT INTO wal_probe VALUES (1)")
                connection.commit()
                connection.execute("PRAGMA busy_timeout=1")
                tick = time.perf_counter()
                checkpoint = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                checkpoint_ms = (time.perf_counter() - tick) * 1000
            backup = directory / "consistent-backup.db"
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            with closing(sqlite3.connect(backup)) as target:
                restored = target.execute("SELECT count(*) FROM workflow_attempts WHERE outcome='succeeded' AND job_id LIKE 'job-%'").fetchone()[0]
                backup_wal_value = target.execute("SELECT value FROM wal_probe").fetchone()[0] if mode == "WAL" else None
            reader.rollback()
        released_checkpoint = list(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) if mode == "WAL" else None
    crash = context.Process(target=_crash_writer, args=(str(database),))
    crash.start()
    crash.join(30)
    if crash.is_alive():
        crash.terminate()
        crash.join()
        raise RuntimeError("crash probe timed out")
    with closing(sqlite3.connect(database)) as connection:
        crash_rows = [row[0] for row in connection.execute("SELECT value FROM crash_probe")]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    # Bounded SQLITE_FULL simulation, not exhaustion of the host filesystem.
    with closing(sqlite3.connect(directory / "disk-pressure.db")) as pressure:
        pressure.execute("PRAGMA max_page_count=4")
        pressure.execute("CREATE TABLE pressure(value BLOB)")
        full = False
        try:
            pressure.execute("INSERT INTO pressure VALUES (zeroblob(1048576))")
            pressure.commit()
        except sqlite3.OperationalError as error:
            full = getattr(error, "sqlite_errorname", None) == "SQLITE_FULL"
            pressure.rollback()
        pressure_ok = pressure.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    errors = [error for result in results for error in result["errors"]]
    return {"format": "sqlite-workflow-benchmark/v1", "mode": mode,
            "workers": workers, "jobs": jobs, "fixture_payload_bytes_per_job": 4096,
            "simulated_api_delay_seconds": delay, "elapsed_including_process_start_seconds": elapsed,
            "throughput_jobs_per_second": len(claims) / elapsed, "timings_ms": timings,
            "errors": errors, "unique_claims": len(set(claims)), "total_claims": len(claims),
            "successful_attempts": attempts, "backup_successful_attempts": restored,
            "long_reader_checkpoint": checkpoint, "long_reader_checkpoint_ms": checkpoint_ms,
            "released_reader_checkpoint": released_checkpoint, "backup_wal_value": backup_wal_value,
            "crash_exit_code": crash.exitcode, "crash_committed_rows": crash_rows, "quick_check": integrity,
            "bounded_sqlite_full_detected": full, "pressure_quick_check_ok": pressure_ok,
            "acceptance_passed": len(claims) == len(set(claims)) == attempts == restored == jobs
            and not errors and crash_rows == ["committed"] and crash.exitcode == 23
            and integrity == "ok" and full and pressure_ok,
            "source_database_bytes": source_database.stat().st_size if source_database else None,
            "representative_history": source_database is not None,
            "limitations": ["Isolated historical copy with synthetic API waits and call payloads." if source_database else "Synthetic local fixture; not a production-scale or hardware-matched dataset.",
                            "Timings include transaction/lock wait and Python work; no lock-only attribution.",
                            "Cold process start is included; use repetitions to assess variance.",
                            "No production journal mode or worker settings were changed."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, nargs="+", default=[10, 20])
    parser.add_argument("--jobs", type=int, default=200)
    parser.add_argument("--api-delay", type=float, default=.02)
    parser.add_argument("--source-database", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10:
        parser.error("repetitions must be between 1 and 10")
    with tempfile.TemporaryDirectory(prefix="bili-sqlite-benchmark-") as temporary:
        reports = []
        for repetition in range(args.repetitions):
            for workers in args.workers:
                for mode in ("DELETE", "WAL"):
                    with tempfile.TemporaryDirectory(dir=temporary) as trial:
                        result = benchmark(Path(trial) / "database", workers=workers,
                            jobs=args.jobs, delay=args.api_delay, mode=mode,
                            source_database=args.source_database)
                        result["repetition"] = repetition + 1
                        reports.append(result)
                    print(f"completed {mode}/{workers} repetition {repetition+1}", flush=True)
    report = {"host": {"platform": platform.platform(), "python": platform.python_version(),
                       "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count()},
              "results": reports, "production_decision": "No switch authorized by synthetic evidence alone."}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0 if all(item["acceptance_passed"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
