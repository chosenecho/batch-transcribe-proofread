"""Read-only replay readiness evidence; verifies this process, never a running worker by implication."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import tracemalloc

from bili_asr.storage import editorial_replay
from bili_asr.storage.database import connect_database


def verify(database: Path):
    with closing(connect_database(database.resolve(strict=True), readonly=True)) as connection:
        connection.execute("BEGIN")
        installed = editorial_replay.require_indexes(connection)
        integrity = editorial_replay.integrity_report(connection)
        count = connection.execute("SELECT count(*) FROM editorial_model_calls").fetchone()[0]
        fingerprint = editorial_replay.FINGERPRINT
        accepted = editorial_replay.ACCEPTED
        sql = (f"SELECT rowid,finished_at,call_id,input_id,chunk_id,request_json FROM editorial_model_calls "
               f"WHERE {accepted} AND {fingerprint}=? ORDER BY finished_at DESC,rowid DESC LIMIT 1")
        query_plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, ("0" * 64,))]
        scoped_plan = [row[3] for row in connection.execute(
            f"EXPLAIN QUERY PLAN SELECT rowid,finished_at,call_id,input_id,chunk_id,request_json "
            f"FROM editorial_model_calls WHERE {accepted} AND input_id=? AND chunk_id=? "
            f"AND {fingerprint} IS NULL ORDER BY finished_at DESC,rowid DESC LIMIT ?",
            ("absent-input", "absent-chunk", 257))]
        probes = []
        if installed:
            sample = connection.execute(f"SELECT {fingerprint},input_id,chunk_id FROM editorial_model_calls "
                                        f"WHERE {accepted} AND {fingerprint} IS NOT NULL LIMIT 1").fetchone()
            for label, fp, input_id, chunk_id in (
                    [("hit", *sample)] if sample else []) + [
                    ("miss", "0" * 64, "absent-input", "absent-chunk")]:
                tracemalloc.start()
                try:
                    started = time.perf_counter()
                    record = editorial_replay.completed_record(connection, fp, input_id=input_id, chunk_id=chunk_id)
                    seconds = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                probes.append({"kind": label, "found": record is not None, "elapsed_seconds": seconds,
                               "python_peak_bytes": peak})
        loaded_path = Path(editorial_replay.__file__)
        report = {"format": "replay-deployment-verification/v1", "captured_at": datetime.now(timezone.utc).isoformat(),
                  "process": {"pid": os.getpid(), "scope": "this-verifier-process-only",
                              "loaded_module_sha256": hashlib.sha256(loaded_path.read_bytes()).hexdigest()},
                  "sqlite": sqlite3.sqlite_version, "call_rows": count, "indexes_complete": installed,
                  "fingerprint_expression": fingerprint, "query_plan": query_plan,
                  "legacy_scope_plan": scoped_plan, "integrity": integrity, "probes": probes,
                  "ready_for_worker_acceptance": installed and not integrity["invalid_json_call_ids"]
                  and not integrity["truncated"] and any("SEARCH" in row for row in query_plan)
                  and any("SEARCH" in row for row in scoped_plan),
                  "worker_loaded_version_verified": False, "paid_requests_sent": 0}
        connection.rollback()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.database)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"replay deployment verification failed: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_worker_acceptance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
