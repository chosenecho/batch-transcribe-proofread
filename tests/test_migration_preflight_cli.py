"""Public legacy preflight behavior and explicit source-root selection."""

from __future__ import annotations

import json

import pytest

from bili_asr import cli
from tests.fixtures.migration_archive import build_migration_archive


def test_preflight_reports_real_legacy_source_without_conversion(tmp_path, capsys, monkeypatch):
    fixture = build_migration_archive(tmp_path / "source")
    before = (fixture.archive_root / "archive.db").read_bytes()
    monkeypatch.setenv("BILI_ARTIFACT_ROOT", str(tmp_path / "unrelated-missing-root"))
    assert cli.main(["archive", "migration-preflight", "--source-root", str(fixture.archive_root)]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert report["valid"] and report["mode"] == "stopped-checkpointed"
    assert not report["conversion_performed"] and not report["target_created"]
    assert report["totals"]["file_count"] == len(fixture.files)
    assert report["source"]["source_revision"] == "9b289570494b5e8f7cc564a7eaa5b2eb2c28c3ad"
    assert (fixture.archive_root / "archive.db").read_bytes() == before


def test_preflight_requires_explicit_existing_source_and_returns_no_report_on_failure(tmp_path, capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["archive", "migration-preflight"])
    capsys.readouterr()
    source = tmp_path / "missing"
    assert cli.main(["archive", "migration-preflight", "--source-root", str(source)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "archive migration-preflight:" in output.err
    assert not source.exists()


def test_preflight_explicit_artifact_root_and_text_summary(tmp_path, capsys):
    artifacts = tmp_path / "artifacts"
    fixture = build_migration_archive(tmp_path / "source", artifact_root=artifacts)
    assert cli.main(["archive", "migration-preflight", "--source-root", str(fixture.archive_root),
                     "--source-artifact-root", str(artifacts), "--format", "text"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "Source contract:" in output.out and "Source fingerprint:" in output.out
    assert "conversion performed: false; target created: false" in output.out
