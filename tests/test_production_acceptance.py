"""Production acceptance records are separate, bounded and write-once."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile
import pytest

from bili_asr.file_ops import json_bytes
from bili_asr.contracts.json_schema import ContractValidationError, validate_json
from bili_asr.cli.parser import build_parser
from bili_asr.cli.production_acceptance import _cmd_production_acceptance
from bili_asr.production_acceptance import (
    CHECK_IDS,
    ProductionAcceptanceError,
    record_acceptance,
    show_record_file,
    verify_record_file,
)
from bili_asr.release_identity import create_manifest
from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr import production_acceptance as acceptance
from bili_asr import release_identity

_REAL_DATABASE_CONTRACT = acceptance._database_contract
_RUNTIME_BUILD_ID = None


def _release(tmp_path: Path, *, extra_members: tuple[str, ...] = ()) -> tuple[Path, str]:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    build_manifest = create_manifest(package, "a" * 40, "b" * 64)
    wheel = tmp_path / "bili_asr-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("bili_asr/_build_identity.json", json.dumps(build_manifest))
        archive.writestr("bili_asr/module.py", (package / "module.py").read_bytes())
        for member in extra_members:
            archive.writestr(member, b"unregistered payload")
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    global _RUNTIME_BUILD_ID
    _RUNTIME_BUILD_ID = build_manifest["build_id"]
    manifest = tmp_path / "release.json"
    manifest.write_bytes(json_bytes({
        "schema_version": 1,
        "build_id": build_manifest["build_id"],
        "source_commit": "a" * 40,
        "lock_sha256": "b" * 64,
        "wheel": wheel.name,
        "wheel_sha256": wheel_digest,
        "production_acceptance": "pending",
    }))
    return manifest, build_manifest["build_id"]


def _archive(tmp_path: Path) -> Path:
    archive = tmp_path / "archive"
    archive.mkdir()
    return archive


@pytest.fixture(autouse=True)
def _pinned_database_identity(monkeypatch):
    # The acceptance writer consumes the existing read-only DB-contract API.
    # Keep these tests focused on record behavior; the Windows base checkout's
    # legacy snapshot fingerprint test is separately covered by its own suite.
    monkeypatch.setattr("bili_asr.production_acceptance._database_contract",
                        lambda _root: {"runtime_contract": "bilibili-v1", "fingerprint": "sha256:" + "f" * 64})

    def running_build_status(*, expected_build_id=None):
        matches = (_RUNTIME_BUILD_ID is not None
                   and (expected_build_id is None or expected_build_id == _RUNTIME_BUILD_ID))
        return {"state": "verified" if matches else "unexpected_build", "build_id": _RUNTIME_BUILD_ID}

    monkeypatch.setattr(release_identity, "build_status", running_build_status)


def _input(path: Path, *, status: str = "not_run", bad_field: str | None = None,
           expected_roles: list[str] | None = None, actor_status: str = "none",
           external_check_status: str | None = None) -> Path:
    checks = []
    for check_id in CHECK_IDS:
        check_status = external_check_status if check_id == "external-actor-scope" and external_check_status else status
        check = {"id": check_id, "status": check_status}
        if check_status != "not_run":
            check["evidence"] = {"id": f"evidence-{check_id}", "sha256": "c" * 64}
        checks.append(check)
    value = {"format": "bili-asr-production-acceptance-input", "schema_version": 1,
             "environment": {"id": "prod-asr-1", "platform": "linux-x86_64"},
             "operator_id": "operator-1",
             "scope": {
                 "expected_worker_roles": ["asr"] if expected_roles is None else expected_roles,
                 "worker_inventory_complete": True,
                 "worker_inventory_evidence": {"id": "worker-inventory", "sha256": "a" * 64},
                 "external_actor_inventory": [
                    {"kind": kind, "status": actor_status,
                      "evidence": {"id": f"scope-{kind}", "sha256": "b" * 64}}
                     for kind in ("scheduler", "backup_helper", "worker_supervisor", "gpu_runner", "other")
                 ],
             },
             "checks": checks}
    if bad_field:
        value[bad_field] = "/private/path-or-secret"
    path.write_bytes(json_bytes(value))
    return path


def _worker(archive: Path, build_id: str, *, heartbeat: str | None = None,
            other_build: str | None = None, phase: str = "ready") -> Path:
    directory = archive.parent / f".{archive.name}.worker-health"
    directory.mkdir(exist_ok=True)
    heartbeat = heartbeat or datetime.now(timezone.utc).isoformat()
    worker = {"schema_version": 1, "instance_id": "d" * 32, "pid": 1234, "role": "asr",
              "started_at": heartbeat, "heartbeat_at": heartbeat, "phase": phase,
              "build": {"build_id": other_build or build_id, "state": "verified"},
              "dependencies": {"unused": "not copied"}, "profile_digests": ["e" * 64],
              "gpu_acceptance": "not_recorded"}
    (directory / "1234-instance.json").write_bytes(json_bytes(worker))
    return directory


def test_operator_input_schema_is_separate_and_requires_each_check_once(tmp_path):
    operator_input = _input(tmp_path / "input.json")
    value = json.loads(operator_input.read_text(encoding="utf-8"))
    validate_json("production-acceptance-input/v1", value)

    value["checks"][1]["id"] = value["checks"][0]["id"]
    with pytest.raises(ContractValidationError):
        validate_json("production-acceptance-input/v1", value)


def test_record_keeps_unrun_checks_pending_and_verify_show_are_read_only(tmp_path):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "records" / "acceptance.json"

    result = record_acceptance(input_path=operator_input, release_manifest=release,
                               archive_root=archive, worker_health_dir=None,
                               output_path=output)

    record_bytes = output.read_bytes()
    record = show_record_file(output)
    assert result["status"]["production"] == "pending"
    assert "output" not in result
    assert all(check["status"] == "not_run" for check in record["checks"])
    assert record["status"] == {
        "source_delivered": "pending", "installed": "pending",
        "loaded": "pending", "production": "pending",
    }
    report = verify_record_file(output)
    assert report["verification"] == "self_consistent_only"
    assert report["status"] == record["status"]
    assert output.read_bytes() == record_bytes


def test_record_is_write_once_and_refuses_overwrite(tmp_path):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "records" / "same.json"
    args = {"input_path": operator_input, "release_manifest": release,
            "archive_root": archive, "worker_health_dir": None, "output_path": output}
    record_acceptance(**args)
    before = output.read_bytes()
    with pytest.raises(ProductionAcceptanceError, match="already exists"):
        record_acceptance(**args)
    assert output.read_bytes() == before


def test_all_checks_and_explicit_operator_confirmation_are_required(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass")
    output = tmp_path / "record.json"

    pending = record_acceptance(input_path=operator_input, release_manifest=release,
                                archive_root=archive, worker_health_dir=workers,
                                output_path=output, confirm_production=False)
    assert pending["status"]["loaded"] == "verified"
    assert pending["status"]["production"] == "pending"


def test_confirmation_with_complete_current_matching_evidence_can_record_acceptance(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass")

    result = record_acceptance(input_path=operator_input, release_manifest=release,
                               archive_root=archive, worker_health_dir=workers,
                               output_path=tmp_path / "record.json", confirm_production=True)

    assert result["status"]["production"] == "production_verified"
    record = show_record_file(tmp_path / "record.json")
    assert record["profile_digests"] == ["e" * 64]
    assert record["workers"][0]["pid"] == 1234
    assert record["workers"][0]["instance_id"] == "d" * 32


def test_included_external_actors_can_be_verified_with_complete_scope(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass", actor_status="included")

    result = record_acceptance(input_path=operator_input, release_manifest=release,
                               archive_root=archive, worker_health_dir=workers,
                               output_path=tmp_path / "record.json", confirm_production=True)

    assert result["status"]["production"] == "production_verified"


def test_included_external_actor_with_unrun_scope_check_stays_pending(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass", actor_status="included",
                            external_check_status="not_run")

    result = record_acceptance(input_path=operator_input, release_manifest=release,
                               archive_root=archive, worker_health_dir=workers,
                               output_path=tmp_path / "record.json", confirm_production=True)

    assert result["status"]["loaded"] == "verified"
    assert result["status"]["production"] == "pending"


def test_unrun_external_actor_cannot_be_claimed_as_fully_checked(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass", actor_status="not_run")

    with pytest.raises(ProductionAcceptanceError, match="external-actor-scope"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                          archive_root=archive, worker_health_dir=workers,
                          output_path=tmp_path / "record.json", confirm_production=True)
    assert not (tmp_path / "record.json").exists()


def test_empty_expected_worker_roles_are_rejected(tmp_path):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json", expected_roles=[])

    with pytest.raises(ProductionAcceptanceError, match="input schema"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                          archive_root=archive, worker_health_dir=None,
                          output_path=tmp_path / "record.json")
    assert not (tmp_path / "record.json").exists()


def test_empty_worker_inventory_cannot_be_marked_loaded_or_production_verified(tmp_path):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json", status="pass")

    with pytest.raises(ProductionAcceptanceError, match="worker-loaded"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                          archive_root=archive, worker_health_dir=None,
                          output_path=tmp_path / "record.json", confirm_production=True)
    assert not (tmp_path / "record.json").exists()


def test_cli_record_output_does_not_echo_private_output_directory(tmp_path, capsys):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "private" / "acceptance.json"

    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])
    exit_code = _cmd_production_acceptance(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err
    result = json.loads(captured.out)
    assert set(result) == {"record_id", "status"}


def test_second_directory_sync_failure_sanitizes_stderr_and_record_can_be_verified(
        tmp_path, monkeypatch, capsys):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "private" / "acceptance.json"
    calls = 0

    def fail_second_directory_sync(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(f"directory sync failed for {path}")

    monkeypatch.setattr(acceptance, "fsync_directory", fail_second_directory_sync)
    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])

    assert _cmd_production_acceptance(args) == 1

    captured = capsys.readouterr()
    assert calls == 2
    assert str(tmp_path) not in captured.err
    assert "may already have been published" in captured.err
    assert "verify before retrying" in captured.err
    assert output.is_file()
    assert verify_record_file(output)["verification"] == "self_consistent_only"


def test_cli_symlink_failure_stderr_does_not_echo_output_path(tmp_path, capsys):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    target = tmp_path / "target.json"
    target.write_text("sentinel", encoding="utf-8")
    output = tmp_path / "private" / "acceptance.json"
    output.parent.mkdir()
    try:
        output.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])

    assert _cmd_production_acceptance(args) == 1

    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.err
    assert "cannot safely prepare acceptance output" in captured.err
    assert target.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("path_kind,expected_error", [
    ("manifest", "release manifest path cannot be checked safely"),
    ("wheel", "release wheel path cannot be checked safely"),
])
def test_release_path_check_failure_stderr_does_not_echo_path(
        tmp_path, monkeypatch, capsys, path_kind, expected_error):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "private" / "acceptance.json"
    real_checked_path = acceptance.checked_path
    checked_target = (release if path_kind == "manifest" else
                      release.parent / json.loads(release.read_text(encoding="utf-8"))["wheel"])

    def fail_release_path(path):
        if path == checked_target:
            raise ValueError(f"private path failure: {path}")
        return real_checked_path(path)

    monkeypatch.setattr(acceptance, "checked_path", fail_release_path)
    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])

    assert _cmd_production_acceptance(args) == 1

    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.err
    assert expected_error in captured.err
    assert not output.exists()


@pytest.mark.parametrize("member", [
    "bili_asr/evil.so", "bili_asr/evil.pyc", "bili_asr/evil.pth",
    "evil.so", "evil.pyc", "evil.pth", "extra_package/module.py",
])
def test_release_identity_rejects_unregistered_wheel_members(tmp_path, member):
    release, _ = _release(tmp_path, extra_members=(member,))

    with pytest.raises(ProductionAcceptanceError, match="release wheel contents are invalid"):
        acceptance._release_identity(release)


def test_cli_output_directory_failure_stderr_does_not_echo_path(tmp_path, monkeypatch, capsys):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "private-directory" / "nested" / "acceptance.json"
    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])
    real_mkdir = Path.mkdir

    def fail_target_parent(path, *args, **kwargs):
        if path == output.parent:
            raise PermissionError(f"permission denied: {path}")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target_parent)

    assert _cmd_production_acceptance(args) == 1

    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.err
    assert "cannot safely prepare acceptance output" in captured.err
    assert not output.exists()


def test_temporary_path_check_failure_stderr_does_not_echo_random_path(
        tmp_path, monkeypatch, capsys):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "private" / "acceptance.json"
    real_checked_path = acceptance.checked_path

    def fail_temporary_path(path):
        if path.name.startswith(".acceptance-"):
            raise ValueError(f"private temporary path failure: {path}")
        return real_checked_path(path)

    monkeypatch.setattr(acceptance, "checked_path", fail_temporary_path)
    args = build_parser().parse_args([
        "production-acceptance", "record", "--input", str(operator_input),
        "--release-manifest", str(release), "--archive-root", str(archive),
        "--output", str(output),
    ])

    assert _cmd_production_acceptance(args) == 1

    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.err
    assert "cannot safely publish acceptance record" in captured.err
    assert not output.exists()


def test_output_is_restricted_to_owner_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permission bits are not available on this platform")
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "record.json"

    record_acceptance(input_path=operator_input, release_manifest=release,
                      archive_root=archive, worker_health_dir=None, output_path=output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_oversized_record_is_rejected_before_publication(tmp_path, monkeypatch):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    output = tmp_path / "record.json"
    monkeypatch.setattr(acceptance, "_MAX_RECORD", 128)

    with pytest.raises(ProductionAcceptanceError, match="size limit"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                          archive_root=archive, worker_health_dir=None, output_path=output)
    assert not output.exists()


def test_explicit_release_build_check_overrides_optional_environment_expectation(tmp_path, monkeypatch):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass")
    received = {}

    def running_build_status(*, expected_build_id=None):
        received["expected_build_id"] = expected_build_id
        return {"state": "verified", "build_id": build_id}

    monkeypatch.setenv("BILI_ASR_EXPECTED_BUILD_ID", "f" * 64)
    monkeypatch.setattr(release_identity, "build_status", running_build_status)

    record_acceptance(input_path=operator_input, release_manifest=release,
                      archive_root=archive, worker_health_dir=workers,
                      output_path=tmp_path / "record.json")

    assert received["expected_build_id"] == build_id


def test_mismatched_running_package_is_rejected_without_record(tmp_path, monkeypatch):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    monkeypatch.setattr(release_identity, "build_status",
                        lambda *, expected_build_id=None: {"state": "verified", "build_id": "f" * 64})

    with pytest.raises(ProductionAcceptanceError, match="running package build identity"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                          archive_root=archive, worker_health_dir=None,
                          output_path=tmp_path / "record.json")
    assert not (tmp_path / "record.json").exists()


def test_stale_or_wrong_build_worker_cannot_be_marked_loaded(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id, other_build="f" * 64)
    operator_input = _input(tmp_path / "input.json", status="pass")

    with pytest.raises(ProductionAcceptanceError, match="worker-loaded"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                           archive_root=archive, worker_health_dir=workers,
                           output_path=tmp_path / "record.json", confirm_production=True)
    assert not (tmp_path / "record.json").exists()


def test_worker_inventory_must_match_declared_roles_exactly(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    operator_input = _input(tmp_path / "input.json", status="pass", expected_roles=["asr", "cpu"])

    with pytest.raises(ProductionAcceptanceError, match="worker-loaded"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                           archive_root=archive, worker_health_dir=workers,
                           output_path=tmp_path / "record.json", confirm_production=True)


def test_unexpected_observed_worker_role_is_rejected(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    original = json.loads(next(workers.glob("*.json")).read_text(encoding="utf-8"))
    original["pid"] = 5678
    original["instance_id"] = "9" * 32
    original["role"] = "cpu"
    (workers / "5678-extra.json").write_bytes(json_bytes(original))
    operator_input = _input(tmp_path / "input.json", status="pass", expected_roles=["asr"])

    with pytest.raises(ProductionAcceptanceError, match="worker-loaded"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                           archive_root=archive, worker_health_dir=workers,
                           output_path=tmp_path / "record.json", confirm_production=True)


@pytest.mark.parametrize("field", ["host_path", "credential", "transcript_text"])
def test_operator_input_rejects_paths_credentials_and_content_fields(tmp_path, field):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json", bad_field=field)

    with pytest.raises(ProductionAcceptanceError, match="unsupported fields"):
        record_acceptance(input_path=operator_input, release_manifest=release,
                           archive_root=archive, worker_health_dir=None,
                           output_path=tmp_path / "record.json")
    assert not (tmp_path / "record.json").exists()


def test_worker_input_is_allowlisted_and_does_not_copy_health_payload(tmp_path):
    release, build_id = _release(tmp_path)
    archive = _archive(tmp_path)
    workers = _worker(archive, build_id)
    health_path = next(workers.glob("*.json"))
    health = json.loads(health_path.read_text())
    health["secret"] = "must-not-be-copied"
    health["model_path"] = "must-not-be-copied"
    health_path.write_bytes(json_bytes(health))
    operator_input = _input(tmp_path / "input.json")

    record_acceptance(input_path=operator_input, release_manifest=release,
                      archive_root=archive, worker_health_dir=workers,
                      output_path=tmp_path / "record.json")
    content = (tmp_path / "record.json").read_text(encoding="utf-8")
    assert "must-not-be-copied" not in content
    assert str(tmp_path) not in content


def test_output_symlink_is_refused_without_following_it(tmp_path):
    release, _ = _release(tmp_path)
    archive = _archive(tmp_path)
    operator_input = _input(tmp_path / "input.json")
    target = tmp_path / "target.json"
    target.write_text("sentinel")
    link = tmp_path / "acceptance.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ProductionAcceptanceError):
        record_acceptance(input_path=operator_input, release_manifest=release,
                           archive_root=archive, worker_health_dir=None,
                           output_path=link)
    assert target.read_text() == "sentinel"


def test_database_identity_is_read_with_readonly_archive_session(tmp_path, monkeypatch):
    seen = {}

    class Connection:
        def execute(self, _query):
            return []

    class Session:
        def __init__(self, archive, *, mode):
            seen["archive"] = archive
            seen["mode"] = mode
            self.connection = Connection()
            self.database_path = archive / "archive.db"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr("bili_asr.archive_session.ArchiveSession", Session)
    monkeypatch.setattr("bili_asr.storage.archive_contracts.runtime_contract",
                        lambda _connection: "bilibili-v1")
    monkeypatch.setattr("bili_asr.storage.extension_contracts.require_import_extension", lambda _c: False)
    monkeypatch.setattr("bili_asr.storage.extension_contracts.require_supplement_extension", lambda _c: False)
    monkeypatch.setattr("bili_asr.storage.extension_contracts.require_artifact_catalog", lambda _c: False)
    monkeypatch.setattr("bili_asr.storage.extension_contracts.require_artifact_online", lambda _c: False)
    monkeypatch.setattr("bili_asr.storage.observation_rows.require_observations", lambda _c: False)
    monkeypatch.setattr("bili_asr.contracts.fingerprints.database_fingerprint",
                        lambda *args: "sha256:" + "f" * 64)

    result = _REAL_DATABASE_CONTRACT(tmp_path / "archive")

    assert seen["mode"] is ArchiveAccessMode.READ
    assert result == {"runtime_contract": "bilibili-v1", "fingerprint": "sha256:" + "f" * 64}
