"""Malformed portable controls never erase holds, budgets or source identity."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest

from bili_asr.services import retry_control as controls
from bili_asr.storage.database import initialize_schema
from tests.test_artifact_inventory_service import job


@pytest.fixture
def control_source(tmp_path):
    database = tmp_path / "archive.db"
    with closing(sqlite3.connect(database)) as connection:
        initialize_schema(connection)
        connection.execute("INSERT INTO bilibili_users VALUES (1,'creator',1,1)")
        connection.execute("INSERT INTO videos VALUES ('BVTEST',1,1,'video',1,1,1)")
        connection.execute("INSERT INTO video_parts VALUES (1,'BVTEST',0,1,'part',1000,'discovered',1,1)")
        connection.commit()
        job(connection, "held-job", kind="proofread", status="failed")
        path = tmp_path / "control.json"
        source = {"retry_holds": {"held-job": "preserve investigation"},
                  "retry_ledger": {"held-job": {"retries": 3, "last_retry_at": 10,
                                                 "max_retries": 3, "next_retry_at": 40}}}
        path.write_text(json.dumps(source, indent=3) + "\n", encoding="utf-8")
        document = controls.capture_control(path, connection, database_sha256="a" * 64, captured_at=30)
        yield connection, path, source, document


@pytest.mark.parametrize("holds,ledger,reason", [
    (None, {}, "identities"),
    ({"bad/job": "reason"}, {}, "identities"),
    ({"held-job": "   "}, {}, "hold reason"),
    ({"held-job": "https://secret.invalid/token"}, {}, "hold reason"),
    ({"held-job": {"reason": "keep", "password": "secret"}}, {}, "hold fields"),
    ({}, {"held-job": {"last_retry_at": 10}}, "budget ledger"),
    ({}, {"held-job": {"retries": True, "last_retry_at": 10}}, "budget ledger"),
    ({}, {"held-job": {"retries": 1, "last_retry_at": 10, "unknown": 0}}, "budget ledger"),
    ({}, {"held-job": {"retries": 1, "last_retry_at": 10, "dispatch_outcome": "refunded"}}, "budget ledger"),
])
def test_rejected_projection_preserves_source_file_and_failed_job(control_source, holds, ledger, reason):
    connection, path, _source, _document = control_source
    path.write_text(json.dumps({"retry_holds": holds, "retry_ledger": ledger}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match=reason):
        controls.capture_control(path, connection, database_sha256="a" * 64, captured_at=30)
    assert path.read_bytes() == before
    assert connection.execute("SELECT status,attempt_count FROM workflow_jobs").fetchone() == ("failed", 0)


@pytest.mark.parametrize("source,reason", [
    ([], "must be an object"),
    ({"retry_holds": {}}, "complete spent-budget ledger"),
    ({"format": "retry-control/v9", "retry_holds": {}, "retry_ledger": {}}, "source version"),
    ({"schema_version": 1, "retry_holds": {}, "retry_ledger": {}}, "source version"),
    ({"clock_domain": "monotonic", "retry_holds": {}, "retry_ledger": {}}, "clock domain"),
])
def test_unknown_source_versions_cannot_be_projected_as_current(control_source, source, reason):
    connection, path, _old, _document = control_source
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        controls.capture_control(path, connection, database_sha256="a" * 64, captured_at=30)


@pytest.mark.parametrize("version", [1, 2])
def test_recapture_registered_source_retains_raw_origin_and_exhausted_budget(control_source, version):
    connection, path, source, document = control_source
    if version == 1:
        source = {"format": "retry-control/v1", "schema_version": 1, **source}
    else:
        source = document
    raw = (json.dumps(source, indent=4) + "\n").encode("utf-8")
    path.write_bytes(raw)
    captured = controls.capture_control(path, connection, database_sha256="a" * 64, captured_at=31)
    assert captured["source"] == {"format": f"retry-control/v{version}",
                                  "sha256": hashlib.sha256(raw).hexdigest()}
    assert captured["retry_holds"] == source["retry_holds"]
    assert captured["retry_ledger"] == source["retry_ledger"]
    assert captured["retry_ledger"]["held-job"]["retries"] == 3
    controls.validate_control(captured, connection=connection, database_sha256="a" * 64, now=31)


@pytest.mark.parametrize("change,reason", [
    (lambda value: value.update(format="retry-control/v9"), "version or fields"),
    (lambda value: value.update(unregistered=True), "version or fields"),
    (lambda value: value["source"].update(sha256="not-a-sha"), "original retry control digest"),
    (lambda value: value["source"].update(format="retry-control/v9"), "original retry control digest"),
    (lambda value: value["archive_scope"].update(database_sha256="bad"), "archive scope"),
    (lambda value: value["archive_scope"].update(jobs=[]), "archive scope"),
    (lambda value: value["archive_scope"]["jobs"][0].update(identity_sha256="bad"), "job scope identity"),
    (lambda value: value["archive_scope"]["jobs"].append(deepcopy(value["archive_scope"]["jobs"][0])), "job scope identity"),
    (lambda value: value["archive_scope"]["jobs"][0].update(job_id="another-job"), "outside its archive scope"),
])
def test_resealed_malformed_envelope_still_fails_semantic_validation(control_source, change, reason):
    _connection, _path, _source, document = control_source
    altered = deepcopy(document)
    change(altered)
    altered["content_sha256"] = controls.digest({key: value for key, value in altered.items() if key != "content_sha256"})
    with pytest.raises(ValueError, match=reason):
        controls.validate_control(altered)


def test_same_rows_do_not_make_another_database_digest_the_original_source(control_source):
    connection, _path, _source, document = control_source
    with pytest.raises(ValueError, match="source database digest mismatch"):
        controls.validate_control(document, connection=connection, database_sha256="b" * 64)
    controls.validate_control(document, connection=connection, database_sha256="a" * 64)


def test_empty_archive_cannot_supply_a_portable_retry_identity(tmp_path):
    with closing(sqlite3.connect(tmp_path / "empty.db")) as connection:
        initialize_schema(connection)
        with pytest.raises(ValueError, match="without workflow identities"):
            controls.archive_scope(connection, "a" * 64)


def test_oversized_raw_control_is_refused_before_json_loading(control_source):
    connection, path, _source, _document = control_source
    with path.open("wb") as stream:
        stream.truncate(controls.MAX_BYTES + 1)
    with pytest.raises(ValueError, match="size limit"):
        controls.capture_control(path, connection, database_sha256="a" * 64, captured_at=30)
    assert path.stat().st_size == controls.MAX_BYTES + 1


def test_many_individually_valid_hold_reasons_still_obey_total_byte_limit():
    holds = {f"job-{index}": "r" * 2048 for index in range(4200)}
    with pytest.raises(ValueError, match="size limit"):
        controls.seal_control({"retry_holds": holds, "retry_ledger": {}})
