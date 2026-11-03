"""Offline maintenance accepts frozen histories and refuses partial trust."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import shutil
from pathlib import Path
import sqlite3
import zipfile

import pytest

from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS
from bili_asr.services import maintenance_package as package
from bili_asr.services.archive_migration import initialize_archive, migrate_archive
from bili_asr.storage.archive_contracts import bootstrap_contract
from bili_asr.storage.maintenance_selection import select_database, table_ledger
from bili_asr.storage.upgrade_evidence import readonly
from tests.fixtures.frozen_migration_archive import frozen_archive


def _bytes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _install(tmp_path, exported, name="target"):
    target = tmp_path / name
    plan = package.plan_import(exported, target)
    assert not target.exists()
    path = tmp_path / (name + "-plan.json")
    package.write_plan(plan, path)
    return target, plan, path, package.apply_import(path)


def test_full_frozen_history_exact_source_bytes_and_idempotent_receipt(tmp_path):
    source = tmp_path / "source"
    expected = frozen_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,policy_key,dedupe_key,payload_json,status,priority,available_at,attempt_count,created_at,updated_at) VALUES('full-failed','subtitle',1,'test','full-failed','{\"video_part_id\":1}','failed',0,1,1,1,1)")
        connection.execute("INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,outcome,error_code,result_json) VALUES('full-attempt','full-failed','worker',1,2,'failed','diagnostic',NULL)")
    before = _bytes(source)
    exported = tmp_path / "full.zip"
    first = package.export_package(source, exported)
    assert package.inspect_package(exported)["payload_verified"] is False
    assert package.verify_package(exported)["valid"]
    target, plan, plan_path, applied = _install(tmp_path, exported)
    assert not applied["reused"]
    assert package.apply_import(plan_path)["reused"]
    assert package.check_import(target, plan_id=plan["plan_id"])["installation_baseline_matches"]
    assert _bytes(source) == before
    assert (source / "archive.db").read_bytes() == (target / "archive.db").read_bytes()
    assert table_ledger(source / "archive.db") == table_ledger(target / "archive.db")
    for key, sha256 in expected["files"].items():
        assert hashlib.sha256((target / key).read_bytes()).hexdigest() == sha256
    with closing(readonly(target / "archive.db")) as connection:
        assert {row[0] for row in connection.execute("SELECT status FROM publication_releases")} == {"published", "superseded", "withdrawn"}
        assert "cancelled" in {row[0] for row in connection.execute("SELECT status FROM workflow_jobs")}
        assert "failed" in {row[0] for row in connection.execute("SELECT outcome FROM workflow_attempts")}
    second = tmp_path / "again.zip"
    assert package.export_package(source, second)["content_id"] == first["content_id"]
    foreign = tmp_path / "foreign"
    shutil.copytree(target, foreign)
    with pytest.raises(ValueError, match="target path"):
        package.check_import(foreign, plan_id=plan["plan_id"])
    (target / "subtitles/BVpreserve.p0.raw.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="baseline changed"):
        package.apply_import(plan_path)


def test_selected_real_frozen_rows_exclude_sibling_artifacts_and_preserve_histories(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,policy_key,dedupe_key,payload_json,status,priority,available_at,attempt_count,created_at,updated_at) VALUES('null-failed','subtitle',1,'test','null-failed','{\"video_part_id\":1}','failed',0,1,1,1,1)")
        connection.execute("INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,outcome,error_code,result_json) VALUES('null-attempt','null-failed','worker',1,2,'failed','diagnostic',NULL)")
    before = _bytes(source)
    exported = tmp_path / "selected.zip"
    result = package.export_package(source, exported, part_ids=[1])
    assert result["selection"]["resolved_part_ids"] == [1]
    target, _, _, _ = _install(tmp_path, exported)
    assert _bytes(source) == before
    with closing(readonly(source / "archive.db")) as origin, closing(readonly(target / "archive.db")) as installed:
        assert list(installed.execute("SELECT video_part_id FROM video_parts")) == [(1,)]
        for table in ("transcripts", "acquisition_attempts", "workflow_jobs", "workflow_publications", "publication_releases", "publication_events"):
            assert list(installed.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")) == list(origin.execute(f"SELECT rowid,* FROM {table} WHERE video_part_id=1 ORDER BY rowid"))
        assert installed.execute("SELECT result_json FROM workflow_attempts WHERE attempt_id='null-attempt'").fetchone() == (None,)
        assert not list(installed.execute("PRAGMA foreign_key_check"))
    with zipfile.ZipFile(exported) as bundle:
        assert "subtitles/BVpreserve.p0.raw.json" in bundle.namelist()
        assert not any(key.startswith(("documents/part-2/", "publications/part-2/")) for key in bundle.namelist())


def test_json_and_fk_dependencies_expand_whole_other_part_history(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        job, raw = connection.execute("SELECT job_id,payload_json FROM workflow_jobs WHERE video_part_id=1 AND kind='asr' LIMIT 1").fetchone()
        other = connection.execute("SELECT transcript_id FROM transcripts WHERE video_part_id=2 LIMIT 1").fetchone()[0]
        payload = json.loads(raw)
        payload["reference_transcript_id"] = other
        connection.execute("UPDATE workflow_jobs SET payload_json=? WHERE job_id=?", (json.dumps(payload), job))
    exported = tmp_path / "expanded.zip"
    result = package.export_package(source, exported, part_ids=[1])
    assert result["selection"]["requested_part_ids"] == [1]
    assert result["selection"]["resolved_part_ids"] == [1, 2]
    assert package.verify_package(exported)["valid"]


def test_attempt_result_identity_and_artifact_dependencies(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    extra = source / "documents" / "detached" / "evidence.json"
    extra.parent.mkdir()
    extra.write_bytes(b'{"frozen":true}')
    with sqlite3.connect(source / "archive.db") as connection:
        other = connection.execute("SELECT transcript_id FROM transcripts WHERE video_part_id=2 LIMIT 1").fetchone()[0]
        result = {"requested_transcript_id": other, "artifacts": {"evidence": "documents/detached/evidence.json"}}
        connection.execute("INSERT INTO workflow_jobs(job_id,kind,video_part_id,policy_key,dedupe_key,payload_json,status,priority,available_at,attempt_count,created_at,updated_at) VALUES('result-failed','subtitle',1,'test','result-failed','{\"video_part_id\":1}','failed',0,1,1,1,1)")
        connection.execute("INSERT INTO workflow_attempts(attempt_id,job_id,worker_id,started_at,finished_at,outcome,error_code,result_json) VALUES('result-attempt','result-failed','worker',1,2,'failed','diagnostic',?)", (json.dumps(result),))
    exported = tmp_path / "selected.zip"
    assert package.export_package(source, exported, part_ids=[1])["selection"]["resolved_part_ids"] == [1, 2]
    with zipfile.ZipFile(exported) as bundle:
        assert bundle.read("documents/detached/evidence.json") == extra.read_bytes()
    assert package.verify_package(exported)["valid"]


def test_asr_evidence_typed_reference_and_audio_dependencies(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    extra = source / "audio" / "detached-evidence.m4a"
    extra.write_bytes(b"frozen-audio-evidence")
    with sqlite3.connect(source / "archive.db") as connection:
        other = connection.execute("SELECT transcript_id FROM transcripts WHERE video_part_id=2 LIMIT 1").fetchone()[0]
        profile = connection.execute("SELECT profile_id FROM workflow_asr_profiles LIMIT 1").fetchone()[0]
        evidence = {"schema_version": 1, "reference_transcript_id": other, "profile_id": profile,
                    "audio": {"storage_key": "audio/detached-evidence.m4a", "sha256": hashlib.sha256(extra.read_bytes()).hexdigest()}}
        connection.execute("UPDATE transcript_asr_evidence SET evidence_json=? WHERE run_id='asr-version-1'", (json.dumps(evidence),))
    exported = tmp_path / "evidence.zip"
    assert package.export_package(source, exported, part_ids=[1])["selection"]["resolved_part_ids"] == [1, 2]
    assert package.verify_package(exported)["valid"]
    with zipfile.ZipFile(exported) as bundle:
        assert bundle.read("audio/detached-evidence.m4a") == extra.read_bytes()


def test_native_universal_selection_and_extended_full_only(tmp_path):
    source = tmp_path / "native"
    initialize_archive(source)
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("INSERT INTO bilibili_users VALUES(1,'creator',1,1)")
        connection.execute("INSERT INTO videos VALUES('BVnative',1,1,'title',1,1,1)")
        connection.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,processing_status,created_at,updated_at) VALUES(7,'BVnative',0,1,'part',1000,'discovered',1,1)")
        connection.execute("INSERT INTO source_metadata_observations VALUES('bilibili','BVnative','details','desc','present',1,1,NULL,'\"description\"')")
    assert package.export_package(source, tmp_path / "native.zip", part_ids=[7])["valid"]
    assert package.verify_package(tmp_path / "native.zip")["valid"]
    legacy = tmp_path / "legacy"
    frozen_archive(legacy)
    extended = tmp_path / "extended"
    migrate_archive(legacy, extended)
    with pytest.raises(ValueError, match="native schemas only|whole-archive"):
        package.export_package(extended, tmp_path / "unsupported.zip", part_ids=[1])
    assert not (tmp_path / "unsupported.zip").exists()
    package.export_package(extended, tmp_path / "extended.zip")
    target, _, _, _ = _install(tmp_path, tmp_path / "extended.zip", "extended-target")
    assert (target / "archive.db").read_bytes() == (extended / "archive.db").read_bytes()


@pytest.mark.parametrize("mutation", ["database", "extra", "duplicate", "traversal", "manifest", "compressed"])
def test_package_corruption_refused_before_target_write(tmp_path, mutation):
    source = tmp_path / "source"
    frozen_archive(source)
    good = tmp_path / "good.zip"
    package.export_package(source, good)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(good) as origin, zipfile.ZipFile(bad, "w") as output:
        for member in origin.infolist():
            data = origin.read(member)
            if mutation == "database" and member.filename == "archive.db":
                data = data[:-1] + bytes([data[-1] ^ 1])
            if mutation == "manifest" and member.filename == "maintenance.json":
                data = data.replace(b'"profile":"private-history"', b'"profile":"public"')
            if mutation == "compressed":
                member.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(member, data)
        if mutation in {"extra", "duplicate", "traversal"}:
            output.writestr({"extra": "documents/extra.json", "duplicate": "archive.db", "traversal": "documents/../escape"}[mutation], b"hostile")
    target = tmp_path / "target"
    with pytest.raises((ValueError, KeyError)):
        package.plan_import(bad, target)
    assert not target.exists()


def test_aliases_active_rows_and_package_drift_are_refused(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    before = (source / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="fresh database"):
        select_database(source / "archive.db", source / "archive.db", part_ids=[1], bvids=[])
    assert (source / "archive.db").read_bytes() == before
    exported = tmp_path / "full.zip"
    package.export_package(source, exported)
    target = tmp_path / "target"
    plan = package.plan_import(exported, target)
    path = tmp_path / "plan.json"
    package.write_plan(plan, path)
    with exported.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="changed since"):
        package.apply_import(path)
    assert not target.exists()
    with sqlite3.connect(source / "archive.db") as connection:
        connection.execute("UPDATE workflow_jobs SET status='running',lease_owner='stopped',lease_expires_at=1 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
    with pytest.raises(ValueError, match="unfinished execution"):
        package.export_package(source, tmp_path / "active.zip")
    assert not (tmp_path / "active.zip").exists()


def test_import_staging_failure_can_retry_without_partial_target(tmp_path, monkeypatch):
    source = tmp_path / "source"
    frozen_archive(source)
    exported = tmp_path / "full.zip"
    package.export_package(source, exported)
    target = tmp_path / "target"
    plan_path = tmp_path / "plan.json"
    package.write_plan(package.plan_import(exported, target), plan_path)
    original = package._extract
    def interrupted(package_path, stage, **kwargs):
        result = original(package_path, stage, **kwargs)
        if kwargs.get("artifacts", True):
            raise OSError("injected staging interruption")
        return result
    monkeypatch.setattr(package, "_extract", interrupted)
    with pytest.raises(OSError, match="interruption"):
        package.apply_import(plan_path)
    assert not target.exists()
    monkeypatch.setattr(package, "_extract", original)
    assert package.apply_import(plan_path)["valid"]


def test_offline_cli_registration_needs_no_archive_database(tmp_path, capsys):
    from bili_asr.cli.maintenance_package import _cmd_maintenance_package
    args = build_parser().parse_args(["maintenance", "verify", "--package", str(tmp_path / "missing.zip")])
    assert COMMANDS[args.command].database_for(args) is None
    assert _cmd_maintenance_package(args) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "maintenance-validation-failed"
