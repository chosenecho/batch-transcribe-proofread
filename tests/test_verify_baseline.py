import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.verify_baseline import (
    FIXTURE_SCHEMA,
    SNAPSHOT_SCHEMA,
    PrerequisiteError,
    advisory_applies,
    audit,
    load_snapshot,
    main,
    offline_install_commands,
    redact,
    record_command,
    safe_env,
    validate_fixture,
    copy_checkout_docs,
)


def write_fixture(path: Path, *, required_distributions: list[str] | None = None) -> Path:
    path.mkdir()
    wheel = path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"not a real wheel")
    manifest = {
        "schema": FIXTURE_SCHEMA,
        "project": "bili-asr",
        "required_distributions": required_distributions or ["demo"],
        "artifacts": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}],
    }
    (path / "fixture-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_record_command_classifies_build_backend_bootstrap_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.verify_baseline as verifier

    class FailedProcess:
        returncode = 17
        stdout = "pip output with https://example.test/?token=secret"
        stderr = "backend detail"

    monkeypatch.setattr(verifier.subprocess, "run", lambda *args, **kwargs: FailedProcess())
    result: dict[str, object] = {"commands": []}
    with pytest.raises(PrerequisiteError, match=r"offline_dependency_closure_unavailable: declared build backend bootstrap failed \(exit 17\)") as exc_info:
        record_command(result, "bootstrap_declared_build_requirements", ["python", "-m", "pip"], tmp_path)
    message = str(exc_info.value)
    assert "fixture must contain compatible declared build-system wheels" in message
    assert "pip output" not in message
    assert "secret" not in message
    assert result["commands"] == [{"name": "bootstrap_declared_build_requirements", "returncode": 17}]


def test_preflight_classifies_build_backend_bootstrap_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.prepare_offline_baseline_fixture as fixture_builder

    class FailedProcess:
        returncode = 23
        stdout = "raw pip output"
        stderr = "missing wheel"

    def fake_run(command, **kwargs):
        if "-m" in command and "venv" in command:
            return type("Created", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return FailedProcess()

    monkeypatch.setattr(fixture_builder.subprocess, "run", fake_run)
    monkeypatch.setattr(fixture_builder, "offline_install_commands", lambda python, fixture: (["bootstrap"], ["install"]))
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[build-system]\nrequires=[]\n", encoding="utf-8")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    with pytest.raises(fixture_builder.FixturePrerequisiteError, match=r"offline_dependency_closure_unavailable: declared build backend bootstrap failed \(exit 23\)") as exc_info:
        fixture_builder.preflight_fixture(fixture)
    message = str(exc_info.value)
    assert "fixture must contain compatible declared build-system wheels" in message
    assert "raw pip output" not in message
def test_offline_install_bootstraps_declared_build_requirements_before_project(tmp_path: Path):
    bootstrap, project_install = offline_install_commands(Path("/tmp/venv/bin/python"), tmp_path / "fixture")
    assert bootstrap[-1] == "setuptools>=77"
    assert "--no-build-isolation" not in bootstrap
    assert project_install[-2:] == ["--no-build-isolation", ".[dev]"]
    assert bootstrap[:4] == project_install[:4]


def test_snapshot_schema_and_digest_are_validated(tmp_path: Path):
    path = tmp_path / "advisories.json"
    path.write_text(json.dumps({"schema": SNAPSHOT_SCHEMA, "advisories": []}), encoding="utf-8")
    data, digest = load_snapshot(path)
    assert data["schema"] == SNAPSHOT_SCHEMA
    assert len(digest) == 16


def test_arbitrary_snapshot_is_rejected(tmp_path: Path):
    path = tmp_path / "not-a-snapshot.json"
    path.write_text(json.dumps({"name": "anything"}), encoding="utf-8")
    with pytest.raises(PrerequisiteError, match="schema"):
        load_snapshot(path)


def test_duplicate_advisory_ids_are_rejected(tmp_path: Path):
    path = tmp_path / "advisories.json"
    path.write_text(
        json.dumps(
            {
                "schema": SNAPSHOT_SCHEMA,
                "advisories": [
                    {"id": "TEST-1", "name": "demo", "specifier": ">=1"},
                    {"id": "TEST-1", "name": "other", "specifier": ">=1"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PrerequisiteError, match="duplicate advisory id TEST-1"):
        load_snapshot(path)


def test_advisory_in_range_version_matches():
    assert advisory_applies({"id": "TEST-1", "name": "demo", "specifier": ">=1,<2"}, "1.5")


def test_advisory_out_of_range_version_does_not_match():
    assert not advisory_applies({"id": "TEST-1", "name": "demo", "specifier": ">=1,<2"}, "2.0")


def test_unsupported_advisory_specifier_is_named_prerequisite():
    with pytest.raises(PrerequisiteError, match="unsupported specifier for TEST-1"):
        advisory_applies({"id": "TEST-1", "name": "demo", "specifier": "not a specifier"}, "1.0")


def test_audit_fails_for_in_range_advisory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("scripts.verify_baseline.installed_packages", lambda _python: {"demo": "1.5"})
    result: dict[str, object] = {"commands": []}
    snapshot = {"schema": SNAPSHOT_SCHEMA, "advisories": [{"id": "TEST-1", "name": "demo", "specifier": ">=1,<2"}]}
    with pytest.raises(RuntimeError, match="findings"):
        audit(Path(sys.executable), snapshot, "digest", result)
    assert result["audit"]["finding_ids"] == ["TEST-1"]


def test_audit_passes_for_out_of_range_advisory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("scripts.verify_baseline.installed_packages", lambda _python: {"demo": "2.0"})
    result: dict[str, object] = {"commands": []}
    snapshot = {"schema": SNAPSHOT_SCHEMA, "advisories": [{"id": "TEST-1", "name": "demo", "specifier": ">=1,<2"}]}
    audit(Path(sys.executable), snapshot, "digest", result)
    assert result["audit"]["status"] == "passed"


def test_fixture_manifest_rejects_modified_artifact(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    (fixture / "demo-1.0-py3-none-any.whl").write_bytes(b"modified")
    with pytest.raises(PrerequisiteError, match="artifact content"):
        validate_fixture(fixture)


def test_fixture_manifest_rejects_duplicate_required_distributions(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture", required_distributions=["Demo", "demo"])
    with pytest.raises(PrerequisiteError, match="required_distributions contains duplicates"):
        validate_fixture(fixture)


def test_fixture_manifest_rejects_extra_wheel(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture")
    (fixture / "extra-1.0-py3-none-any.whl").write_bytes(b"extra")
    with pytest.raises(PrerequisiteError, match="exactly match"):
        validate_fixture(fixture)


def test_fixture_manifest_binds_required_distributions_to_wheels(tmp_path: Path):
    fixture = write_fixture(tmp_path / "fixture", required_distributions=["other"])
    with pytest.raises(PrerequisiteError, match="exactly match"):
        validate_fixture(fixture)


def test_script_path_is_platform_correct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scripts.verify_baseline as verifier
    monkeypatch.setattr(verifier.os, "name", "nt")
    assert verifier.script_path(tmp_path, "bili-asr").name == "bili-asr.exe"


def test_copy_checkout_docs_copies_synthetic_checkout_docs(tmp_path: Path):
    source = tmp_path / "checkout"
    destination = tmp_path / "staged"
    (source / "docs" / "nested").mkdir(parents=True)
    (source / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    (source / "docs" / "nested" / "notes.txt").write_text("notes", encoding="utf-8")

    copy_checkout_docs(source, destination)

    assert (destination / "docs" / "guide.md").read_text(encoding="utf-8") == "guide"
    assert (destination / "docs" / "nested" / "notes.txt").read_text(encoding="utf-8") == "notes"


def test_copy_checkout_docs_rejects_out_of_root_symlink_without_copying(tmp_path: Path):
    source = tmp_path / "checkout"
    destination = tmp_path / "staged"
    external = tmp_path / "external.txt"
    (source / "docs").mkdir(parents=True)
    external.write_text("secret external content", encoding="utf-8")
    (source / "docs" / "external.txt").symlink_to(external)

    with pytest.raises(PrerequisiteError, match=r"docs_invalid: symlink is not allowed in docs"):
        copy_checkout_docs(source, destination)

    assert not destination.exists()
    assert external.read_text(encoding="utf-8") == "secret external content"


def test_staged_test_tree_copies_checkout_inputs(tmp_path: Path):
    from scripts.verify_baseline import staged_test_tree
    staged = staged_test_tree(tmp_path)
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / "pyproject.toml").is_file()
    assert (tmp_path / "uv.lock").is_file()
    assert (tmp_path / "scripts" / "verify_baseline.py").is_file()
    assert (tmp_path / "scripts" / "forensic_log.py").is_file()
    assert (staged / "test_cli_pilot.py").is_file()
    assert (staged / "test_installed_baseline.py").is_file()
    assert (staged / "test_cli_help.py").is_file()
    assert not (staged / "test_installed_cli.py").exists()
    assert not (staged / "test_verify_baseline.py").exists()


def test_network_deny_guard_blocks_all_socket_connect_variants(monkeypatch: pytest.MonkeyPatch):
    import socket
    from scripts.verify_baseline import install_network_deny_guard

    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    install_network_deny_guard()
    with pytest.raises(RuntimeError, match="network access denied"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    with pytest.raises(RuntimeError, match="network access denied"):
        socket.socket().connect(("127.0.0.1", 9))
    with pytest.raises(RuntimeError, match="network access denied"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    with pytest.raises(RuntimeError, match="network access denied"):
        socket.socket().connect(("127.0.0.1", 9))
def test_safe_env_removes_session_and_proxy_variables(monkeypatch: pytest.MonkeyPatch):
    for key in ("PYTHONPATH", "BILI_SESSDATA", "SESSDATA", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "secret")
    env = safe_env()
    assert not {"PYTHONPATH", "BILI_SESSDATA", "SESSDATA", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} & env.keys()
    assert env["PIP_NO_INDEX"] == "1"


def test_redact_removes_bearer_json_header_and_url_secrets():
    diagnostic = 'Authorization: Bearer abc.def\n{"access_token":"secret"}\nhttps://example.test/?signature=secret'
    redacted = redact(diagnostic)
    assert "abc.def" not in redacted
    assert '"secret"' not in redacted
    assert "example.test" not in redacted
    assert "[redacted]" in redacted
    assert "[redacted-url]" in redacted


def test_invalid_arguments_write_named_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("scripts.verify_baseline.DEFAULT_RESULT", tmp_path / "verification-results" / "baseline.json")
    assert main(["--pip-audit", "unused"]) == 2
    payload = json.loads((tmp_path / "verification-results" / "baseline.json").read_text(encoding="utf-8"))
    assert payload["status"] == "prerequisite_failed"
    assert payload["error"] == "invalid_arguments: use --help for supported options"
