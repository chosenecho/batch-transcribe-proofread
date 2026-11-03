"""Public snapshot parser, dispatch, and failure contracts."""

from __future__ import annotations

import json

import pytest

from bili_asr import cli


def test_snapshot_requires_explicit_file_or_output_and_restore_target():
    parser = cli.build_parser()
    for arguments in (["snapshot"], ["snapshot", "save"], ["snapshot", "check"],
                      ["snapshot", "restore", "--file", "backup.zip"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(arguments)
        assert exc.value.code == 1


def test_snapshot_help_available_without_media_dependencies(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["snapshot", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "save" in output and "check" in output and "restore" in output


def test_snapshot_save_passes_resolved_artifact_root(tmp_path, monkeypatch, capsys):
    from bili_asr.services import archive_snapshot

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("BILI_ARTIFACT_ROOT", str(artifacts))
    calls = []

    def save(root, out, *, artifact_root, storage_targets):
        calls.append((root, out, artifact_root, storage_targets))
        return {"snapshot_id": "saved"}

    monkeypatch.setattr(archive_snapshot, "save_snapshot", save)
    assert cli.main(["snapshot", "save", "--archive-root", str(tmp_path),
                     "--out", str(tmp_path / "backup.zip")]) == 0
    assert calls == [(tmp_path, tmp_path / "backup.zip", artifacts, {})]
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == "saved"


def test_snapshot_check_is_independent_of_device_artifact_configuration(tmp_path, monkeypatch, capsys):
    from bili_asr.services import archive_snapshot

    monkeypatch.setenv("BILI_ARTIFACT_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(archive_snapshot, "check_snapshot", lambda path: {"checked": True})
    assert cli.main(["snapshot", "check", "--file", str(tmp_path / "backup.zip")]) == 0
    assert json.loads(capsys.readouterr().out) == {"checked": True}


def test_snapshot_missing_input_returns_clear_error(tmp_path, capsys):
    assert cli.main(["snapshot", "check", "--file", str(tmp_path / "missing.zip")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "snapshot check:" in captured.err
