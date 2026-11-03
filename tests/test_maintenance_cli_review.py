"""Exercise offline maintenance entry points with real retained archive evidence."""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS
from tests.fixtures.frozen_migration_archive import frozen_archive
from tests.test_publication_export import manuscript, publish


def invoke(capsys, *arguments):
    args = build_parser().parse_args([str(value) for value in arguments])
    spec = COMMANDS[args.command]
    result = getattr(importlib.import_module(spec.handler_module), spec.handler)(args)
    streams = capsys.readouterr()
    return result, json.loads(streams.out) if streams.out else None, streams.err


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    frozen_archive(root)
    return root


def test_cli_full_export_inspect_verify_and_import_receipt(source, tmp_path, capsys):
    output = tmp_path / "full.zip"
    assert invoke(capsys, "maintenance", "export", "--source-root", source, "--output", output)[0] == 0
    code, inspected, _ = invoke(capsys, "maintenance", "inspect", "--package", output)
    assert code == 0 and inspected["manifest"]["files"]
    assert invoke(capsys, "maintenance", "verify", "--package", output)[1]["valid"]
    target, plan_path = tmp_path / "target", tmp_path / "plan.json"
    code, plan, _ = invoke(capsys, "maintenance", "import-plan", "--package", output,
                           "--target-root", target, "--output", plan_path)
    assert code == 0 and not target.exists()
    assert json.loads(plan_path.read_bytes())["plan_id"] == plan["plan_id"]
    assert invoke(capsys, "maintenance", "import-apply", "--plan", plan_path)[1]["valid"]
    assert invoke(capsys, "maintenance", "import-check", "--target-root", target,
                  "--plan-id", plan["plan_id"])[1]["valid"]
    with sqlite3.connect(target / "archive.db") as db:
        assert db.execute("SELECT count(*) FROM transcripts").fetchone()[0] > 0


def test_cli_scoped_export_and_import_keep_only_selected_metadata(source, tmp_path, capsys):
    output = tmp_path / "scoped.zip"
    code, _, _ = invoke(capsys, "maintenance", "export", "--source-root", source,
                        "--output", output, "--profile", "database-only", "--dataset", "metadata",
                        "--dataset", "tags", "--part-id", 1)
    assert code == 0
    _, report, _ = invoke(capsys, "maintenance", "inspect", "--package", output)
    assert report["manifest"]["selection"]["resolved_part_ids"] == [1]
    target, plan = tmp_path / "target", tmp_path / "plan.json"
    assert invoke(capsys, "maintenance", "import-plan", "--package", output,
                  "--target-root", target, "--output", plan, "--profile", "database-only",
                  "--dataset", "metadata", "--part-id", 1)[0] == 0
    assert invoke(capsys, "maintenance", "import-apply", "--plan", plan)[0] == 0
    with sqlite3.connect(target / "archive.db") as db:
        assert db.execute("SELECT count(*) FROM video_parts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0


def test_cli_delta_chain_materialization_and_safe_retention(source, tmp_path, capsys):
    repository = tmp_path / "repository"
    repository.mkdir()
    base, delta = repository / "base.zip", repository / "delta.zip"
    assert invoke(capsys, "maintenance", "export", "--source-root", source, "--output", base)[0] == 0
    (source / "documents" / "extra.txt").write_bytes(b"retained new evidence")
    code, saved, _ = invoke(capsys, "maintenance", "delta-save", "--source-root", source,
                           "--output", delta, "--baseline", base, "--compression", "deflate")
    assert code == 0 and saved["changed_files"] == 1
    assert len(invoke(capsys, "maintenance", "chain-verify", "--package", delta)[1]["chain"]) == 2
    full = tmp_path / "materialized.zip"
    assert invoke(capsys, "maintenance", "chain-materialize", "--package", delta, "--output", full)[0] == 0
    assert invoke(capsys, "maintenance", "verify", "--package", full)[1]["valid"]
    plan = tmp_path / "retention.json"
    code, report, _ = invoke(capsys, "maintenance", "retention-plan", "--repository", repository,
                            "--output", plan, "--keep-count", 1, "--keep-days", 0)
    assert code == 0 and "base.zip" in report["protected_dependencies"]
    assert invoke(capsys, "maintenance", "retention-apply", "--plan", plan)[1]["deleted"] == []
    assert base.exists() and delta.exists()


def test_cli_checkpoint_and_backup_policy_run_status_and_failure(source, tmp_path, capsys):
    assert invoke(capsys, "maintenance", "checkpoint", "--source-root", source)[1]["valid"]
    repository, policy_path = tmp_path / "backups", tmp_path / "policy.json"
    code, configured, _ = invoke(capsys, "maintenance", "backup-policy", "--source-root", source,
                                 "--repository", repository, "--output", policy_path,
                                 "--mode", "full", "--keep-count", 2,
                                 "--keep-days", 0, "--interval-seconds", 3600, "--alert-after", 1)
    assert code == 0 and json.loads(policy_path.read_bytes())["policy_id"] == configured["policy_id"]
    code, receipt, _ = invoke(capsys, "maintenance", "backup-run", "--policy", policy_path,
                             "--trigger", "event", "--event-id", "published-release-1")
    assert code == 0 and receipt["restoration_verified"]
    _, duplicate, _ = invoke(capsys, "maintenance", "backup-run", "--policy", policy_path,
                             "--trigger", "event", "--event-id", "published-release-1")
    assert duplicate["skipped"] == "event_already_backed_up"
    _, status, _ = invoke(capsys, "maintenance", "backup-status", "--repository", repository,
                          "--policy-id", configured["policy_id"])
    assert status["runs"][0]["receipt_id"] == receipt["receipt_id"]
    with sqlite3.connect(source / "archive.db") as db:
        db.execute("UPDATE workflow_jobs SET status='running',lease_owner='active',lease_expires_at=99999999999 WHERE job_id=(SELECT job_id FROM workflow_jobs LIMIT 1)")
    code, failed, _ = invoke(capsys, "maintenance", "backup-run", "--policy", policy_path)
    assert code == 1 and failed["valid"] is False
    assert failed["notification"] == "backup_failure_alert"


def test_cli_full_backup_refuses_unsupported_compression_before_writing_policy(source, tmp_path, capsys):
    output = tmp_path / "policy.json"
    code, report, _ = invoke(capsys, "maintenance", "backup-policy", "--source-root", source,
                            "--repository", tmp_path / "backups", "--output", output,
                            "--mode", "full", "--compression", "deflate")
    assert code == 1 and "stored containers" in report["error"]["message"]
    assert not output.exists()


@pytest.mark.parametrize("output_kind", ["missing-parent", "existing-file"])
def test_cli_report_output_errors_preserve_existing_bytes(source, tmp_path, capsys, output_kind):
    output = tmp_path / "missing" / "policy.json" if output_kind == "missing-parent" else tmp_path / "existing.json"
    if output_kind == "existing-file":
        output.write_bytes(b"original evidence")
    code, report, _ = invoke(capsys, "maintenance", "backup-policy", "--source-root", source,
                            "--repository", tmp_path / "backups", "--output", output)
    assert code == 1 and report["error"]["code"] == "maintenance-validation-failed"
    if output_kind == "existing-file":
        assert output.read_bytes() == b"original evidence"
    else:
        assert not output.parent.exists()


def test_cli_corrupt_package_and_unknown_action_return_structured_errors(tmp_path, capsys):
    from bili_asr.cli.maintenance_package import _cmd_maintenance_package
    package = tmp_path / "bad.zip"
    package.write_bytes(b"invalid zip")
    code, report, _ = invoke(capsys, "maintenance", "verify", "--package", package)
    assert code == 1 and report["operation"] == "maintenance-verify"
    args = build_parser().parse_args(["maintenance", "verify", "--package", str(package)])
    args.maintenance_action = "unregistered-action"
    assert _cmd_maintenance_package(args) == 1
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "unsupported maintenance action"


@pytest.mark.usefixtures("enable_content_publication_for_tests")
def test_cli_public_profile_keeps_approved_release_and_rejects_selection(manuscript, tmp_path, capsys):
    connection, root, _, edition = manuscript
    publish(connection, root, edition)
    output = tmp_path / "public"
    code, report, _ = invoke(capsys, "maintenance", "export", "--source-root", root,
                            "--output", output, "--profile", "publication")
    assert code == 0 and report["published_articles"] == 1
    assert not list(output.rglob("*.db"))
    rejected = tmp_path / "selected-public"
    code, report, _ = invoke(capsys, "maintenance", "export", "--source-root", root,
                            "--output", rejected, "--profile", "publication", "--part-id", 1)
    assert code == 1 and "current approved releases" in report["error"]["message"]
    assert not rejected.exists()


def test_snapshot_cli_baseline_saves_real_delta_and_rejects_remote_bindings(source, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BILI_ARTIFACT_ROOT", raising=False)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    assert invoke(capsys, "snapshot", "save", "--archive-root", source, "--out", base)[0] == 0
    (source / "documents" / "new.txt").write_bytes(b"new fact")
    code, report, _ = invoke(capsys, "snapshot", "save", "--archive-root", source,
                            "--out", delta, "--baseline", base, "--compression", "deflate")
    assert code == 0 and report["changed_files"] >= 1
    assert invoke(capsys, "maintenance", "chain-verify", "--package", delta)[1]["valid"]
    materialized = tmp_path / "materialized.zip"
    assert invoke(capsys, "maintenance", "chain-materialize", "--package", delta,
                  "--output", materialized)[0] == 0
    _, inspected, _ = invoke(capsys, "maintenance", "inspect", "--package", materialized)
    assert any(item["path"] == "documents/new.txt" for item in inspected["manifest"]["files"])
    rejected = tmp_path / "invalid-delta.zip"
    code, _, error = invoke(capsys, "snapshot", "save", "--archive-root", source,
                           "--out", rejected, "--baseline", base, "--storage-target", "remote=/missing")
    assert code == 1 and "materialized local objects" in error
    assert not rejected.exists()


def test_upgrade_cli_audit_reports_new_work_without_allowing_baseline_reuse(source, tmp_path, capsys):
    from bili_asr.contracts.registry import IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY, UNIVERSAL_V2
    from bili_asr.services.archive_upgrade import apply_upgrade, plan_upgrade
    target = tmp_path / "target"
    plan = plan_upgrade(source, target, target_contracts=(UNIVERSAL_V2, IMPORT_EXTENSION, SOURCE_SUPPLEMENT_POLICY),
                        no_external_control_state=True)
    apply_upgrade(plan)
    code, verified, _ = invoke(capsys, "archive", "upgrade-check", "--target-root", target,
                              "--plan-id", plan["plan_id"])
    assert code == 0 and verified["valid"]
    (target / "documents" / "new-work.txt").write_bytes(b"retained work")
    code, audited, _ = invoke(capsys, "archive", "upgrade-check", "--target-root", target,
                             "--plan-id", plan["plan_id"], "--audit")
    assert code == 0 and audited["added_artifacts"] == ["documents/new-work.txt"]
    assert audited["baseline_reuse_allowed"] is False
    code, _, error = invoke(capsys, "archive", "upgrade-check", "--target-root", target,
                           "--plan-id", plan["plan_id"])
    assert code == 1 and "file set changed" in error
    assert (target / "documents" / "new-work.txt").read_bytes() == b"retained work"
