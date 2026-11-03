"""Public artifact commands preserve source state and freeze reviewed output."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from bili_asr import cli
from bili_asr.services.artifact_catalog_upgrade import authority_fingerprints
from bili_asr.services.artifact_inventory_service import validate_offload_plan
from bili_asr.storage.artifact_catalog import require_artifact_catalog
from bili_asr.storage.database import connect_database, initialize_schema
from tests.fixtures.frozen_migration_archive import frozen_archive


@pytest.fixture
def cli_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("BILI_ARTIFACT_ROOT", raising=False)
    root = tmp_path / "source"
    root.mkdir()
    database = root / "archive.db"
    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        connection.execute("INSERT INTO bilibili_users VALUES (1,'creator',1,1)")
        connection.execute("INSERT INTO videos VALUES ('BVCLI',1,1,'video',1,1,1)")
        connection.execute("INSERT INTO video_parts VALUES (1,'BVCLI',0,1,'part',1000,'discovered',1,1)")
        connection.execute("INSERT INTO audio_objects VALUES (1,?,5,'m4a',1000,'audio/BVCLI.p0.m4a',1)",
                           (hashlib.sha256(b"sound").hexdigest(),))
        connection.execute("INSERT INTO part_audio_objects VALUES (1,1,1,'test')")
    (root / "audio").mkdir()
    (root / "audio/BVCLI.p0.m4a").write_bytes(b"sound")
    return root


def call(root, action, *arguments):
    return cli.main(["artifacts", action, "--archive-root", str(root), *map(str, arguments)])


def test_inventory_defaults_to_quick_and_never_changes_authority(cli_archive, capsys):
    database = cli_archive / "archive.db"
    before = database.read_bytes()
    with connect_database(database, readonly=True) as connection:
        authority = authority_fingerprints(connection)
    assert call(cli_archive, "inventory") == 0
    first = json.loads(capsys.readouterr().out)
    assert first["integrity_depth"] == "quick"
    assert not first["external_holds_verified"]
    assert first["summary"]["files_hashed"] == 0
    assert "external_hold_unverified" in first["copies"][0]["blockers"]
    assert call(cli_archive, "inventory") == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert database.read_bytes() == before
    with connect_database(database, readonly=True) as connection:
        assert authority_fingerprints(connection) == authority
        assert not require_artifact_catalog(connection)


def test_deep_filtered_plan_is_frozen_with_target_identity(cli_archive, tmp_path, capsys):
    output = tmp_path / "reports" / "plan.json"
    assert call(cli_archive, "plan", "--target-id", "disk:archive", "--no-external-holds", "--kind", "audio",
                "--platform", "bilibili", "--creator-id", "1", "--video-id", "BVCLI", "--part-id", "1",
                "--version", "audio:1", "--out", output) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    validate_offload_plan(report)
    assert len(report["items"]) == 1
    assert report["target_id"] == "disk:archive"
    assert report["items"][0]["sha256"] == hashlib.sha256(b"sound").hexdigest()
    assert (cli_archive / "audio/BVCLI.p0.m4a").read_bytes() == b"sound"


@pytest.mark.parametrize("inside", ["archive", "artifacts"])
def test_report_must_be_outside_both_source_roots(cli_archive, tmp_path, capsys, inside):
    artifacts = tmp_path / "products"
    artifacts.mkdir()
    root = cli_archive if inside == "archive" else artifacts
    output = root / "report.json"
    assert call(cli_archive, "inventory", "--artifact-root", artifacts, "--out", output) == 1
    captured = capsys.readouterr()
    assert "outside" in captured.err and captured.out == ""
    assert not output.exists()


def test_report_never_overwrites_existing_file(cli_archive, tmp_path, capsys):
    output = tmp_path / "existing.json"
    output.write_text("keep", encoding="utf-8")
    assert call(cli_archive, "inventory", "--out", output) == 1
    assert "already exists" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "keep"


def test_missing_database_does_not_bootstrap_archive(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BILI_ARTIFACT_ROOT", raising=False)
    missing = tmp_path / "no-archive"
    assert call(missing, "inventory") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "artifacts inventory:" in captured.err
    assert not missing.exists()
    assert not (missing / "archive.db").exists()


@pytest.mark.parametrize("document", [
    [], {}, {"version": True, "holds": {}}, {"version": 2, "holds": {}},
    {"version": 1, "holds": []}, {"version": 1, "holds": {"part:1": []}},
    {"version": 1, "holds": {"part:1": [""]}}, {"version": 1, "holds": {"part:1": "reason"}},
    {"version": 1, "holds": {}, "unexpected": True}, {"version": 1, "holds": {"": ["reason"]}},
])
def test_holds_file_requires_exact_versioned_shape(cli_archive, tmp_path, capsys, document):
    holds = tmp_path / "holds.json"
    holds.write_text(json.dumps(document), encoding="utf-8")
    assert call(cli_archive, "inventory", "--holds-file", holds) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "artifacts inventory:" in captured.err


def test_holds_file_preserves_retention_reason_and_empty_mapping_is_verified(cli_archive, tmp_path, capsys):
    holds = tmp_path / "holds.json"
    holds.write_text(json.dumps({"version": 1, "holds": {"part:1": ["production-investigation"]}}), encoding="utf-8")
    assert call(cli_archive, "plan", "--target-id", "disk", "--holds-file", holds) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["items"] == []
    assert "external_hold:part:1:production-investigation" in report["held"][0]["reasons"]
    holds.write_text(json.dumps({"version": 1, "holds": {}}), encoding="utf-8")
    assert call(cli_archive, "plan", "--target-id", "disk", "--holds-file", holds) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1


@pytest.mark.parametrize("action", ["inventory", "plan"])
def test_unknown_external_holds_block_candidates(cli_archive, capsys, action):
    extra = ["--target-id", "disk"] if action == "plan" else ["--deep"]
    assert call(cli_archive, action, *extra) == 0
    report = json.loads(capsys.readouterr().out)
    if action == "plan":
        assert not report["items"]
        assert "external_hold_unverified" in report["held"][0]["reasons"]
    else:
        assert report["integrity_depth"] == "deep"
        assert not report["copies"][0]["candidate"]


def test_copy_preview_is_non_destructive_and_does_not_claim_release_capacity(cli_archive, capsys):
    assert call(cli_archive, "plan", "--target-id", "disk", "--purpose", "copy", "--kind", "audio") == 0
    report = json.loads(capsys.readouterr().out)
    validate_offload_plan(report)
    assert report["schema"] == "artifact-copy-plan-v1" and len(report["items"]) == 1
    assert report["external_holds_verified"] is False
    assert report["estimate"]["reclaimable_content_bytes"] == 0
    assert (cli_archive / "audio/BVCLI.p0.m4a").read_bytes() == b"sound"


@pytest.mark.parametrize("universal", [False, True])
def test_upgrade_cli_preserves_source_contract_and_supports_both_archive_kinds(tmp_path, monkeypatch, capsys, universal):
    from bili_asr.services.archive_migration import migrate_archive

    monkeypatch.delenv("BILI_ARTIFACT_ROOT", raising=False)
    original = tmp_path / "original"
    frozen_archive(original)
    source = tmp_path / "universal" if universal else original
    if universal:
        migrate_archive(original, source)
    target = tmp_path / "upgraded"
    before = (source / "archive.db").read_bytes()
    assert call(source, "upgrade", "--target-root", target, "--dry-run") == 0
    dry = json.loads(capsys.readouterr().out)
    assert not dry["installed"] and not target.exists()
    assert call(source, "upgrade", "--target-root", target) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["installed"]
    assert (source / "archive.db").read_bytes() == before
    with connect_database(target / "archive.db", readonly=True) as connection:
        assert require_artifact_catalog(connection, required=True)
    assert call(target, "inventory", "--deep", "--no-external-holds", "--kind", "audio") == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["database_contract"] == ("universal-v2" if universal else "bilibili-v1")
    assert inventory["summary"]["files_hashed"] > 0


@pytest.fixture
def prepared_cli(cli_archive, tmp_path, capsys):
    archive = tmp_path / "active"
    assert call(cli_archive, "upgrade", "--target-root", archive) == 0
    capsys.readouterr()
    target = tmp_path / "cold"
    target.mkdir()
    assert call(archive, "bind-target", "--target-root", target, "--target-id", "cold") == 0
    capsys.readouterr()
    plan_file = tmp_path / "audio-plan.json"
    assert call(archive, "plan", "--target-id", "cold", "--kind", "audio", "--no-external-holds", "--out", plan_file) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["items"]) == 1
    return archive, target, plan_file, plan


def _authority(root):
    connection = connect_database(root / "archive.db", readonly=True)
    try:
        return [item for item in authority_fingerprints(connection) if not item["table"].startswith("artifact_")]
    finally:
        connection.close()


def _transfer(prepared, *arguments):
    archive, target, plan_file, _ = prepared
    return call(archive, "transfer", "--target-root", target, "--plan", plan_file,
                "--no-external-holds", *arguments)


def test_real_cli_copy_offload_check_restore_and_idempotence_preserve_production_facts(prepared_cli, capsys):
    archive, target, plan_file, plan = prepared_cli
    item = plan["items"][0]
    audio_path = archive / item["path"]
    before = _authority(archive)
    # The transfer default is copy; bytes remain readable locally.
    assert _transfer(prepared_cli) == 0
    copied = json.loads(capsys.readouterr().out)
    assert copied["operation"]["kind"] == "copy"
    assert copied["operation"]["state"] == "complete"
    assert audio_path.read_bytes() == b"sound"
    assert _transfer(prepared_cli, "--mode", "offload") == 0
    offloaded = json.loads(capsys.readouterr().out)
    assert offloaded["operation"]["state"] == "complete"
    assert offloaded["released_copies"] == 1
    assert not audio_path.exists()
    package = target / offloaded["packages"][0]["relative_key"]
    assert call(archive, "check", "--package", package) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["objects"][0]["sha256"] == item["sha256"]
    assert _transfer(prepared_cli, "--mode", "offload") == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["operation"]["transfer_id"] == offloaded["operation"]["transfer_id"]
    assert repeated["released_bytes_this_run"] == 0
    assert call(archive, "reconcile", "--target-root", target, "--plan", plan_file, "--no-external-holds") == 0
    assert json.loads(capsys.readouterr().out)["operation"]["state"] == "complete"
    assert call(archive, "restore", "--target-root", target, "--target-id", "cold", "--object-id", item["sha256"],
                "--storage-key", item["path"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["restored_bytes"] == item["size"]
    assert hashlib.sha256(audio_path.read_bytes()).hexdigest() == item["sha256"]
    assert call(archive, "restore", "--target-root", target, "--target-id", "cold", "--object-id", item["sha256"],
                "--storage-key", item["path"]) == 0
    assert json.loads(capsys.readouterr().out)["reused_local"]
    assert _authority(archive) == before


def test_cli_refuses_targets_nested_in_sources(prepared_cli, capsys):
    archive, _, plan_file, _ = prepared_cli
    nested = archive / "cold"
    nested.mkdir()
    assert call(archive, "bind-target", "--target-root", nested, "--target-id", "nested") == 1
    assert "must not contain" in capsys.readouterr().err
    assert call(archive, "transfer", "--target-root", nested, "--plan", plan_file, "--no-external-holds") == 1
    assert "must not contain" in capsys.readouterr().err
    assert (archive / "audio/BVCLI.p0.m4a").read_bytes() == b"sound"


@pytest.mark.parametrize("content", ["{}", "[]", "{invalid json", '{"schema":"unknown-offload-plan-v99"}'])
def test_cli_malformed_or_unknown_plan_is_a_bounded_exit1(prepared_cli, tmp_path, capsys, content):
    archive, target, _, _ = prepared_cli
    before = _authority(archive)
    malformed = tmp_path / "malformed-plan.json"
    malformed.write_text(content, encoding="utf-8")
    assert call(archive, "transfer", "--target-root", target, "--plan", malformed, "--no-external-holds") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("artifacts transfer:")
    assert "Traceback" not in captured.err and len(captured.err) < 1000
    assert _authority(archive) == before
    assert (archive / "audio/BVCLI.p0.m4a").read_bytes() == b"sound"


def test_cli_transfer_rechecks_external_holds_against_frozen_plan(prepared_cli, tmp_path, capsys):
    archive, target, plan_file, _ = prepared_cli
    holds = tmp_path / "new-holds.json"
    holds.write_text(json.dumps({"version": 1, "holds": {"part:1": ["new-investigation"]}}), encoding="utf-8")
    assert call(archive, "transfer", "--target-root", target, "--plan", plan_file, "--mode", "offload", "--holds-file", holds) == 1
    assert "retention guard" in capsys.readouterr().err
    assert (archive / "audio/BVCLI.p0.m4a").read_bytes() == b"sound"


@pytest.mark.parametrize("input_kind", ["plan", "holds"])
def test_json_excessive_nesting_is_bounded_diagnostic(prepared_cli, tmp_path, capsys, input_kind):
    archive, target, plan_file, _ = prepared_cli
    malformed = tmp_path / "nested.json"
    malformed.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    if input_kind == "plan":
        result = call(archive, "transfer", "--target-root", target, "--plan", malformed, "--no-external-holds")
    else:
        result = call(archive, "transfer", "--target-root", target, "--plan", plan_file, "--holds-file", malformed)
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("artifacts transfer:")
    assert "Traceback" not in captured.err and len(captured.err) < 1000
