"""Read actual inactive backups without changing or importing active archives."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
import requests

from bili_asr.cli.main import main
from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS, ArtifactPolicy
from bili_asr.services import maintenance_state_reader as reader
from bili_asr.services.archive_migration import migrate_archive
from bili_asr.services.maintenance_task_state import save_task_state
from bili_asr.storage.archive_contracts import bootstrap_contract
from bili_asr.storage.upgrade_evidence import readonly
from tests.fixtures.frozen_migration_archive import frozen_archive


@pytest.fixture(params=["bilibili-v1", "universal-v2"])
def state_bundle(tmp_path, request):
    source = tmp_path / "source"
    frozen_archive(source)
    if request.param == "universal-v2":
        target = tmp_path / "universal"
        migrate_archive(source, target)
        source = target
    frozen = ' {"error":"invalid_response", "reason":"original\\nfailure"} '
    model_request = ' {"model": "reader-model", "messages": []} '
    with sqlite3.connect(source / "archive.db") as db:
        db.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,policy_key,dedupe_key,payload_json,status,priority,available_at,attempt_count,last_error_code,created_at,updated_at) "
                   "VALUES('reader-failed','proofread',1,'test','reader-failed',' {\"video_part_id\":1} ','failed',0,1,2,'invalid_response',1,40)")
        db.execute("INSERT INTO workflow_attempts VALUES('z-original','reader-failed','old-worker',20,21,'failed','invalid_response',?)", (frozen,))
        db.execute("INSERT INTO workflow_attempts VALUES('a-retry','reader-failed','new-worker',10,11,'failed','transport',NULL)")
        input_id = db.execute("SELECT input_id FROM editorial_inputs WHERE video_part_id=1 LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO editorial_job_inputs VALUES('reader-failed',?)", (input_id,))
        db.execute("INSERT INTO editorial_model_calls VALUES('reader-call','z-original',?,'original-chunk',?,NULL,'invalid_response',20,21)", (input_id, model_request))
        db.execute("UPDATE workflow_jobs SET status='running',lease_owner='vanished-process',lease_expires_at=999,attempt_count=3 WHERE job_id=(SELECT job_id FROM workflow_jobs WHERE kind='subtitle' LIMIT 1)")
        mid = db.execute("SELECT mid FROM bilibili_users LIMIT 1").fetchone()[0]
        bvid = db.execute("SELECT bvid FROM video_parts WHERE video_part_id=1").fetchone()[0]
        db.execute("INSERT INTO ingestion_runs VALUES('reader-collection',?,'bilibili-api-python','frozen-reader-version',1,1,40,42,'failed')", (mid,))
        db.execute("INSERT INTO acquisition_runs VALUES('reader-acquisition','subtitle','bvid',?,1,0,40,42,'failed')", (bvid,))
        db.execute("INSERT INTO acquisition_attempts VALUES('reader-acquisition',1,'failed','subtitle_body_unavailable',NULL,40,42,0,0)")
    controls = tmp_path / "controls"
    controls.mkdir()
    control = controls / "retry.json"
    control.write_text(' {"format":"retry-control/v1","schema_version":1,"retry_holds":{"reader-failed":{"reason":"publication-contract","paused":true}},"retry_ledger":{"reader-failed":{"retries":2,"last_retry_at":40,"max_retries":2}}} ', encoding="utf-8")
    package = tmp_path / "state.zip"
    save_task_state(source, package, external_components=[{"kind": "retry-control/v1", "path": str(control.resolve()),
                                                         "lock_path": str((controls / "controller.lock").resolve())}])
    return source, package, control, frozen, model_request


def _rewrite(package, target, mutate):
    with zipfile.ZipFile(package) as origin, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as output:
        for info in origin.infolist():
            payload = origin.read(info)
            if info.filename == "task-state.json":
                manifest = json.loads(payload)
                mutate(manifest)
                from bili_asr.services.maintenance_package_core import digest
                manifest["content_id"] = digest({k: v for k, v in manifest.items() if k not in {"content_id", "created_at"}})
                payload = json.dumps(manifest).encode()
            output.writestr(info, payload)


def test_original_failure_attempt_insertion_order_frozen_json_and_control_bytes(state_bundle, monkeypatch, tmp_path):
    source, package, control, frozen, model_request = state_bundle
    original = (source / "archive.db").read_bytes()
    def no_network(*args, **kwargs):
        raise AssertionError("offline reader attempted network")
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    from bili_asr.archive_session import ArchiveSession
    monkeypatch.setattr(ArchiveSession, "__init__", no_network)
    result = reader.read_task_state(package, query=reader.StateQuery(job_ids=("reader-failed",)), work_directory=tmp_path)
    assert result["inactive_archive"] and not result["media_self_contained"]
    assert result["restore_policy"] == "read-only-evidence-never-start-workers/v1"
    attempts = result["histories"]["workflow_attempts"]["records"]
    assert [row["values"]["attempt_id"] for row in attempts] == ["z-original", "a-retry"]
    assert attempts[0]["values"]["result_json"] == frozen
    assert attempts[0]["frozen_json_sha256"]["result_json"] == hashlib.sha256(frozen.encode()).hexdigest()
    assert attempts[1]["values"]["result_json"] is None
    calls = result["histories"]["editorial_model_calls"]["records"]
    assert calls[0]["values"]["request_json"] == model_request
    assert base64.b64decode(result["control_records"][0]["base64"]) == control.read_bytes()
    assert (source / "archive.db").read_bytes() == original
    assert not any(path.name.startswith("task-state-reader-") for path in tmp_path.iterdir())
    again = reader.read_task_state(package, query=reader.StateQuery(job_ids=("reader-failed",)))
    assert again == result


def test_filters_intersect_bounded_pagination_and_running_lease_preserved(state_bundle):
    source, package, *_ = state_bundle
    with closing(readonly(source / "archive.db")) as db:
        bvid = db.execute("SELECT bvid FROM video_parts WHERE video_part_id=1").fetchone()[0]
        running_id = db.execute("SELECT job_id FROM workflow_jobs WHERE status='running' LIMIT 1").fetchone()[0]
        model = db.execute("SELECT f.model_name FROM workflow_jobs j JOIN workflow_asr_profiles f USING(profile_id) LIMIT 1").fetchone()[0]
    query = reader.StateQuery(job_ids=("reader-failed",), videos=(bvid,), part_ids=(1,), stages=("proofread",),
                              statuses=("failed",), models=("reader-model",), updated_from=40, updated_to=40, history_limit=1)
    result = reader.read_task_state(package, query=query)
    assert [row["values"]["job_id"] for row in result["jobs"]] == ["reader-failed"]
    assert result["histories"]["workflow_attempts"]["truncated"]
    assert len(result["histories"]["workflow_attempts"]["records"]) == 1
    assert reader.read_task_state(package, query=reader.StateQuery(models=(model,)))["jobs"]
    assert reader.read_task_state(package, query=reader.StateQuery(job_ids=("' OR 1=1--",)))["jobs"] == []
    assert reader.read_task_state(package, query=reader.StateQuery(updated_to=0))["jobs"] == []
    first = reader.read_task_state(package, query=reader.StateQuery(limit=1))
    second = reader.read_task_state(package, query=reader.StateQuery(limit=1, offset=first["pagination"]["next_offset"]))
    assert first["jobs"][0]["values"]["job_id"] != second["jobs"][0]["values"]["job_id"]
    running = reader.read_task_state(package, query=reader.StateQuery(job_ids=(running_id,)))["jobs"][0]["values"]
    assert running["status"] == "running" and running["lease_owner"] == "vanished-process" and running["attempt_count"] == 3
    with closing(readonly(source / "archive.db")) as db:
        assert db.execute("SELECT status,lease_owner,attempt_count FROM workflow_jobs WHERE job_id=?", (running_id,)).fetchone() == ("running", "vanished-process", 3)


def test_cli_dispatch_has_no_active_database_policy_and_evidence_bytes(state_bundle, capsys):
    _, package, control, *_ = state_bundle
    spec = COMMANDS["task-state"]
    assert spec.database is None and spec.artifacts is ArtifactPolicy.NONE
    args = build_parser().parse_args(["task-state", "read", "--snapshot", str(package), "--job", "reader-failed"])
    assert args.job == ["reader-failed"]
    assert main(["task-state", "read", "--snapshot", str(package), "--job", "reader-failed", "--status", "failed", "--stage", "proofread", "--model", "reader-model", "--part", "1", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["values"]["job_id"] == "reader-failed"
    assert main(["task-state", "evidence", "--snapshot", str(package), "--path", "control-state/component-0.json"]) == 0
    assert base64.b64decode(json.loads(capsys.readouterr().out)["evidence"]["base64"]) == control.read_bytes()
    assert main(["task-state", "verify", "--snapshot", str(package)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["task-state", "read", "--snapshot", str(package), "--limit", "0"]) == 1
    assert "pagination" in capsys.readouterr().err


@pytest.mark.parametrize("change", [lambda value: value.update(format="archive-task-state-backup/v999"),
                                    lambda value: value.update(runtime_contract="unknown-v7"),
                                    lambda value: value["observation"].update(protocol="inferred/v99"),
                                    lambda value: value.update(database_contract="sha256:" + "f" * 64)])
def test_unknown_versions_and_corruption_fail_closed_before_record_output(state_bundle, tmp_path, change):
    _, package, *_ = state_bundle
    bad = tmp_path / "unknown.zip"
    _rewrite(package, bad, change)
    with pytest.raises((ValueError, RuntimeError)):
        reader.read_task_state(bad)
    assert not any(path.name.startswith("task-state-reader-") for path in tmp_path.iterdir())


def test_corrupt_zip_missing_evidence_and_scratch_failure_are_reported(state_bundle, tmp_path, monkeypatch, capsys):
    _, package, *_ = state_bundle
    bad = tmp_path / "corrupt.zip"
    bad.write_bytes(b"not a zip")
    assert main(["task-state", "read", "--snapshot", str(bad)]) == 1
    assert not capsys.readouterr().out
    for key in ("../escape", "/absolute", "archive.db", ""):
        with pytest.raises(ValueError):
            reader.read_state_evidence(package, key)
    with pytest.raises(ValueError, match="ordinary directory"):
        reader.read_task_state(package, work_directory=tmp_path / "missing")
    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(reader.tempfile, "TemporaryDirectory", no_space)
    assert main(["task-state", "read", "--snapshot", str(package)]) == 1
    assert "No space left" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs", [{"job_ids": ["wrong-container"]}, {"videos": ("",)}, {"models": ("x" * 1025,)},
                                   {"stages": tuple("x" for _ in range(101))}, {"part_ids": (True,)}, {"part_ids": [1]},
                                   {"offset": -1}, {"limit": True}, {"history_limit": 1001}, {"updated_from": -1},
                                   {"updated_to": True}, {"updated_from": 2, "updated_to": 1}])
def test_invalid_queries_fail_before_opening_any_package(kwargs):
    with pytest.raises(ValueError):
        reader.read_task_state(Path("must-not-be-opened.zip"), query=reader.StateQuery(**kwargs))


def test_bounded_report_refuses_large_history_and_evidence(state_bundle, monkeypatch):
    _, package, *_ = state_bundle
    monkeypatch.setattr(reader, "MAX_REPORT_BYTES", 2048)
    with pytest.raises((ValueError, sqlite3.DataError), match="budget|too big"):
        reader.read_task_state(package)
    monkeypatch.setattr(reader, "MAX_REPORT_BYTES", 1)
    with pytest.raises(ValueError, match="byte budget"):
        reader.read_state_evidence(package, "control-state/component-0.json")


def test_blob_identity_is_lossless_and_json_strings_are_not_normalized():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT ? AS bytes, ? AS result_json, ? AS integer_value", (b"\x00\xff", ' {"a": 1} ', 1)).fetchone()
        result = reader._record(row)
    assert base64.b64decode(result["values"]["bytes"]["base64"]) == b"\x00\xff"
    assert result["values"]["result_json"] == ' {"a": 1} '
    assert result["row_sha256"] == hashlib.sha256(reader._canonical(result["values"])).hexdigest()


def test_collection_and_acquisition_rows_read_without_workflow_binding(state_bundle, capsys):
    source, package, *_ = state_bundle
    query = reader.StateQuery(statuses=("failed",), updated_from=42, updated_to=42)
    result = reader.read_task_records(package, "ingestion_runs", query=query)
    assert [row["values"]["run_id"] for row in result["records"]] == ["reader-collection"]
    assert result["records"][0]["values"]["source_version"] == "frozen-reader-version"
    with closing(readonly(source / "archive.db")) as db:
        bvid = db.execute("SELECT bvid FROM video_parts WHERE video_part_id=1").fetchone()[0]
    result = reader.read_task_records(package, "acquisition_runs", query=reader.StateQuery(stages=("subtitle",),
                                      videos=(bvid,), statuses=("failed",), updated_from=42, updated_to=42))
    assert result["records"][0]["values"]["run_id"] == "reader-acquisition"
    assert main(["task-state", "records", "--snapshot", str(package), "--table", "acquisition_attempts", "--part", "1", "--status", "failed", "--updated-from", "42", "--updated-to", "42"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["values"]["error_code"] == "subtitle_body_unavailable"


@pytest.mark.parametrize("table,kwargs", [("workflow_jobs", {"job_ids": ("reader-failed",), "stages": ("proofread",)}),
                                         ("workflow_jobs", {"models": ("reader-model",)}),
                                         ("workflow_attempts", {"job_ids": ("reader-failed",)}),
                                         ("editorial_inputs", {"job_ids": ("reader-failed",), "part_ids": (1,)}),
                                         ("editorial_model_calls", {"job_ids": ("reader-failed",), "part_ids": (1,), "models": ("reader-model",)}),
                                         ("transcript_segments", {"part_ids": (1,)}),
                                         ("document_artifacts", {"part_ids": (1,)}),
                                         ("publication_edition_reviews", {"part_ids": (1,)}),
                                         ("publication_events", {"statuses": ("published",)}),
                                         ("workflow_asr_profiles", {"models": ("qwen",)})])
def test_record_relation_adapters_preserve_source_rows(state_bundle, table, kwargs):
    source, package, *_ = state_bundle
    if table == "workflow_asr_profiles":
        with closing(readonly(source / "archive.db")) as db:
            kwargs = {"models": (db.execute("SELECT model_name FROM workflow_asr_profiles LIMIT 1").fetchone()[0],)}
    result = reader.read_task_records(package, table, query=reader.StateQuery(**kwargs))
    assert result["records"]
    with closing(readonly(source / "archive.db")) as db:
        db.row_factory = sqlite3.Row
        for record in result["records"]:
            row = db.execute('SELECT rowid AS archive_rowid,* FROM "' + table + '" WHERE rowid=?', (record["values"]["archive_rowid"],)).fetchone()
            assert reader._record(row) == record


@pytest.mark.parametrize("kwargs,message", [({"job_ids": ("reader-failed",)}, "job filter"), ({"part_ids": (1,)}, "part filter"),
                                           ({"stages": ("subtitle",)}, "stage filter"), ({"videos": ("BVexample",)}, "video filter"),
                                           ({"models": ("model",)}, "model filter")])
def test_table_filters_are_not_silently_ignored(state_bundle, kwargs, message):
    _, package, *_ = state_bundle
    with pytest.raises(ValueError, match=message):
        reader.read_task_records(package, "ingestion_runs", query=reader.StateQuery(**kwargs))


def test_unknown_record_table_and_undefined_status_time_are_refused(state_bundle):
    _, package, *_ = state_bundle
    with pytest.raises(ValueError, match="unsupported.*table"):
        reader.read_task_records(package, 'workflow_jobs";DROP TABLE workflow_jobs--')
    for query, expected in ((reader.StateQuery(statuses=("failed",)), "status filter"),
                            (reader.StateQuery(updated_from=1), "time filter")):
        with pytest.raises(ValueError, match=expected):
            reader.read_task_records(package, "editorial_job_inputs", query=query)


def test_records_pagination_retains_attempt_order_and_final_offset(state_bundle):
    _, package, *_ = state_bundle
    first = reader.read_task_records(package, "workflow_attempts", query=reader.StateQuery(job_ids=("reader-failed",), limit=1))
    assert first["records"][0]["values"]["attempt_id"] == "z-original"
    assert first["truncated"] and first["pagination"]["next_offset"] == 1
    second = reader.read_task_records(package, "workflow_attempts", query=reader.StateQuery(job_ids=("reader-failed",), limit=1, offset=1))
    assert second["records"][0]["values"]["attempt_id"] == "a-retry"
    assert second["pagination"]["next_offset"] is None


def test_metadata_only_archive_has_readable_failed_collection_with_zero_workflow_jobs(tmp_path):
    source = tmp_path / "no-jobs"
    source.mkdir()
    with sqlite3.connect(source / "archive.db") as db:
        bootstrap_contract(db, "bilibili-v1")
        db.execute("INSERT INTO bilibili_users VALUES(1,'original creator',1,1)")
        db.execute("INSERT INTO ingestion_runs VALUES('only-collection',1,'bilibili-api-python','original version',1,1,40,42,'failed')")
    package = tmp_path / "no-jobs.zip"
    save_task_state(source, package, no_external_control_state=True)
    report = reader.read_task_state(package)
    assert report["jobs"] == []
    assert report["histories"]["ingestion_runs"]["records"][0]["values"]["run_id"] == "only-collection"
    records = reader.read_task_records(package, "ingestion_runs", query=reader.StateQuery(statuses=("failed",), updated_from=42))
    assert records["records"][0]["values"]["run_id"] == "only-collection"


def test_output_budget_counts_base64_and_report_envelope():
    from bili_asr.services.maintenance_state_reader import _bounded_report
    with pytest.raises(ValueError, match="byte budget"):
        _bounded_report({"report": "x" * reader.MAX_REPORT_BYTES})


def test_universal_neutral_video_adapter_and_asr_model_id(state_bundle, tmp_path):
    source, package, *_ = state_bundle
    with closing(readonly(source / "archive.db")) as db:
        model = db.execute("SELECT model_name FROM asr_models LIMIT 1").fetchone()[0]
    assert reader.read_task_records(package, "transcripts", query=reader.StateQuery(models=(model,)))["records"]
    with closing(readonly(source / "archive.db")) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_videos'").fetchone():
            return
    with sqlite3.connect(source / "archive.db") as db:
        db.execute("INSERT INTO source_videos VALUES(100,'youtube','reader-neutral-video',NULL,'original title',1,'en','https://example.invalid/original',1,1,1)")
        db.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,processing_status,created_at,updated_at,source_video_id) VALUES(100,NULL,0,NULL,'original part',1000,'discovered',1,1,100)")
        db.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,dedupe_key,payload_json,status,available_at,created_at,updated_at) VALUES('neutral-job','subtitle',100,'neutral-job','{\"schema_version\":1,\"video_part_id\":100}','queued',1,1,1)")
    neutral = tmp_path / "neutral.zip"
    save_task_state(source, neutral, no_external_control_state=True)
    query = reader.StateQuery(videos=("youtube:reader-neutral-video",))
    assert reader.read_task_state(neutral, query=query)["jobs"][0]["values"]["job_id"] == "neutral-job"
    assert reader.read_task_records(neutral, "video_parts", query=query)["records"][0]["values"]["video_part_id"] == 100
    assert reader.read_task_records(neutral, "source_videos", query=query)["records"][0]["values"]["external_id"] == "reader-neutral-video"
