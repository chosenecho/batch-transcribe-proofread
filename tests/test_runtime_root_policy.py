"""Product-owned runtime directories have one explicit maintenance policy."""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bili_asr.archive_maintenance import archive_access
from bili_asr.file_ops import EXPORT_LOCK_MARKER, exclusive_lock
from bili_asr.services import archive_snapshot
from bili_asr.services import maintenance_task_state as task_state
from bili_asr.services import runtime_root_policy as policy
from bili_asr.services.archive_snapshot import (
    SnapshotError,
    check_snapshot,
    save_snapshot,
)
from bili_asr.services.maintenance_package_core import export_package, inspect_package
from bili_asr.services.maintenance_task_state import save_task_state
from bili_asr.services.migration_preflight import (
    MigrationPreflightError,
    migration_preflight,
)
from tests.test_archive_snapshot import _seed_archive
from tests.test_task_state_backup import rewrite


def _idle_runtime(root: Path) -> None:
    (root / ".artifact-runtime").mkdir()
    (root / ".asr-input-reservations").mkdir()


def test_empty_runtime_directories_are_explicitly_excluded_by_all_maintenance_exports(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    _idle_runtime(root)

    preflight = migration_preflight(root)
    assert {item["path"] for item in preflight["excluded"]} == {
        ".artifact-runtime", ".asr-input-reservations"
    }

    package_path = tmp_path / "maintenance.zip"
    export_package(root, package_path)
    package_manifest = inspect_package(package_path)["manifest"]
    assert package_manifest["source"]["runtime_policy"] == "archive-runtime-v1"
    assert {item["path"] for item in package_manifest["source"]["excluded"]} == {
        ".artifact-runtime", ".asr-input-reservations"
    }

    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    assert check_snapshot(snapshot)["valid"]

    state = save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)
    assert state["manifest"]["runtime_policy"] == "archive-runtime-v1"
    assert {item["path"] for item in state["manifest"]["runtime_exclusions"]} == {
        ".artifact-runtime", ".asr-input-reservations"
    }


def test_stopped_fence_is_preserved_as_exclusion_but_active_fence_blocks(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    runtime = root / ".artifact-runtime"
    runtime.mkdir()
    requested = runtime / ("a" * 64)
    with archive_access(requested, exclusive=True):
        pass
    report = migration_preflight(root)
    fences = [item for item in report["excluded"] if item["category"] == "runtime-fence"]
    assert fences and fences[0]["state"] == "stopped-runtime-fence"

    with archive_access(requested, exclusive=False), pytest.raises(MigrationPreflightError, match="active runtime fence"):
        migration_preflight(root)
    with archive_access(requested, exclusive=False), pytest.raises(SnapshotError, match="active runtime fence"):
        save_snapshot(root, tmp_path / "blocked.zip")


@pytest.mark.parametrize("kind", ["unknown", "reservation", "malformed-reservation", "symlink"])
def test_runtime_directories_fail_closed_for_unknown_or_live_state(tmp_path, kind):
    root = tmp_path / "source"
    _seed_archive(root)
    if kind == "unknown":
        directory = root / ".artifact-runtime"
        directory.mkdir()
        (directory / "unexpected").write_bytes(b"x")
    elif kind in {"reservation", "malformed-reservation"}:
        directory = root / ".asr-input-reservations"
        directory.mkdir()
        value = '{"schema_version":1,"byte_size":1}' if kind == "reservation" else '{"schema_version":true,"byte_size":1}'
        (directory / ("a" * 32 + ".json")).write_text(value, encoding="utf-8")
    else:
        target = tmp_path / "target"
        target.mkdir()
        try:
            (root / ".artifact-runtime").symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    with pytest.raises(MigrationPreflightError):
        migration_preflight(root)


def test_nonempty_fence_cannot_be_silently_excluded(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    directory = root / ".artifact-runtime"
    directory.mkdir()
    fence = directory / ("." + "a" * 64 + ".archive-maintenance.lock")
    fence.write_bytes(b"unexpected retained evidence")
    for save, output in ((export_package, "package.zip"), (save_snapshot, "snapshot.zip")):
        with pytest.raises(ValueError, match="empty regular file"):
            save(root, tmp_path / output)
    with pytest.raises(ValueError, match="empty regular file"):
        save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)
    assert fence.read_bytes() == b"unexpected retained evidence"


def test_unknown_source_root_remains_refused_with_known_runtime_directories(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    _idle_runtime(root)
    (root / "unknown-private-state.json").write_bytes(b"must not be silently omitted")
    with pytest.raises(MigrationPreflightError, match="unsupported source root"):
        migration_preflight(root)
    with pytest.raises(ValueError, match="unsupported source root"):
        export_package(root, tmp_path / "package.zip")


@pytest.mark.parametrize("save", ["snapshot", "task-state"])
def test_unknown_source_root_is_rejected_by_snapshot_and_task_state(tmp_path, save):
    root = tmp_path / "source"
    _seed_archive(root)
    (root / "private-state.json").write_text("{}", encoding="utf-8")
    if save == "snapshot":
        with pytest.raises(SnapshotError, match="unsupported source root"):
            save_snapshot(root, tmp_path / "snapshot.zip")
    else:
        with pytest.raises(ValueError, match="unsupported source root"):
            save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)


@pytest.mark.parametrize("exclusive", [False, True])
def test_same_thread_runtime_fence_probe_is_not_reentrant(tmp_path, exclusive):
    root = tmp_path / "source"
    _seed_archive(root)
    runtime = root / ".artifact-runtime"
    runtime.mkdir()
    requested = runtime / ("b" * 64)
    with archive_access(requested, exclusive=exclusive), pytest.raises(MigrationPreflightError, match="active runtime fence"):
        migration_preflight(root)


@pytest.mark.parametrize("nested", ["products", "nested/products"])
def test_snapshot_and_task_state_accept_explicit_nested_artifact_roots(tmp_path, nested):
    root = tmp_path / "source"
    products = root / nested
    _seed_archive(root, products)
    (root / ".env").write_text("private-test-credential", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "worker.log").write_text("operational", encoding="utf-8")
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot, artifact_root=products)
    assert check_snapshot(snapshot)["valid"]
    saved = save_task_state(root, tmp_path / "state.zip", artifact_root=products, no_external_control_state=True)
    assert saved["valid"]


@pytest.mark.parametrize("save", ["snapshot", "task-state"])
def test_nested_artifact_root_does_not_hide_unknown_siblings(tmp_path, save):
    root = tmp_path / "source"
    products = root / "nested/products"
    _seed_archive(root, products)
    (products.parent / "private-state.json").write_text("{}", encoding="utf-8")
    if save == "snapshot":
        with pytest.raises(SnapshotError, match="unsupported source root"):
            save_snapshot(root, tmp_path / "snapshot.zip", artifact_root=products)
    else:
        with pytest.raises(ValueError, match="unsupported source root"):
            save_task_state(root, tmp_path / "state.zip", artifact_root=products, no_external_control_state=True)


@pytest.mark.parametrize("media_policy", ["local-required", "verified-references"])
def test_old_task_state_manifest_without_runtime_fields_remains_readable(tmp_path, media_policy):
    root = tmp_path / "source"
    _seed_archive(root)
    original = tmp_path / "state.zip"
    save_task_state(root, original, no_external_control_state=True, media_policy=media_policy)

    def old_manifest(value):
        value.pop("runtime_policy")
        value.pop("runtime_exclusions")

    old = rewrite(original, tmp_path / "old.zip", old_manifest)
    manifest = task_state.verify_task_state(old)["manifest"]
    assert "runtime_policy" not in manifest and "runtime_exclusions" not in manifest


@pytest.mark.parametrize("field", ["runtime_policy", "runtime_exclusions"])
def test_runtime_manifest_fields_must_appear_as_a_pair(tmp_path, field):
    root = tmp_path / "source"
    _seed_archive(root)
    original = tmp_path / "state.zip"
    save_task_state(root, original, no_external_control_state=True)
    incomplete = rewrite(original, tmp_path / "incomplete.zip", lambda value: value.pop(field))
    with pytest.raises(ValueError):
        task_state.verify_task_state(incomplete)


@pytest.mark.parametrize("change", [
    lambda entries: entries[0].update(path="../unknown"),
    lambda entries: entries[0].update(state="stopped-runtime-fence"),
    lambda entries: entries[0].update(count=True),
    lambda entries: entries.append(dict(entries[0])),
    lambda entries: entries[0].pop("state"),
])
def test_runtime_manifest_rejects_rehashed_invalid_exclusions(tmp_path, change):
    root = tmp_path / "source"
    _seed_archive(root)
    _idle_runtime(root)
    original = tmp_path / "state.zip"
    save_task_state(root, original, no_external_control_state=True)
    changed = rewrite(original, tmp_path / "changed.zip", lambda value: change(value["runtime_exclusions"]))
    with pytest.raises(ValueError):
        task_state.verify_task_state(changed)


def test_task_state_rechecks_runtime_after_root_access_acquisition(tmp_path, monkeypatch):
    root = tmp_path / "source"
    _seed_archive(root)
    _idle_runtime(root)
    original = task_state.inspect_runtime_roots
    calls = 0

    def raced_runtime(roots):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / ".asr-input-reservations" / ("a" * 32 + ".json")).write_text(
                '{"schema_version":1,"byte_size":1}', encoding="utf-8")
        return original(roots)

    monkeypatch.setattr(task_state, "inspect_runtime_roots", raced_runtime)
    with pytest.raises(ValueError, match="active ASR input reservation"):
        save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)
    assert calls == 2
    assert not (tmp_path / "state.zip").exists()


def test_stopped_preserved_import_lock_is_recorded_by_every_backup_and_retained(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    with exclusive_lock(root / "preserved-body-import"):
        pass
    lock = root / policy.PRESERVED_IMPORT_LOCK
    before = lock.stat()
    expected = {"path": lock.name, "category": "runtime-export-lock", "state": "stopped-export-lock", "count": 1}
    assert any(all(item[key] == expected[key] for key in expected) for item in migration_preflight(root)["excluded"])
    package = tmp_path / "package.zip"
    export_package(root, package)
    assert any(all(item[key] == expected[key] for key in expected)
               for item in inspect_package(package)["manifest"]["source"]["excluded"])
    snapshot = tmp_path / "snapshot.zip"
    save_snapshot(root, snapshot)
    assert check_snapshot(snapshot)["valid"]
    state = save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)
    assert {**expected, "base": str(root)} in state["manifest"]["runtime_exclusions"]
    after = lock.stat()
    assert lock.read_bytes() == EXPORT_LOCK_MARKER
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_same_thread_preserved_import_lock_is_active_even_on_posix(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    with exclusive_lock(root / "preserved-body-import"), pytest.raises(MigrationPreflightError, match="active or damaged"):
        migration_preflight(root)


@pytest.mark.parametrize("kind", ["bad-marker", "extra-byte", "hardlink", "directory", "symlink"])
def test_preserved_import_lock_refuses_unmanaged_or_linked_bytes(tmp_path, kind):
    root = tmp_path / "source"
    root.mkdir()
    lock = root / policy.PRESERVED_IMPORT_LOCK
    if kind == "directory":
        lock.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(EXPORT_LOCK_MARKER)
        try:
            lock.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        lock.write_bytes(b"private evidence" if kind == "bad-marker" else EXPORT_LOCK_MARKER + (b"x" if kind == "extra-byte" else b""))
        if kind == "hardlink":
            os.link(lock, tmp_path / "other-link")
    with pytest.raises(policy.RuntimeRootPolicyError, match="active or damaged"):
        policy.inspect_runtime_root(root)
    assert lock.exists()


@pytest.mark.parametrize("entry", [".other.export.lock", ".preserved-body-import-journal.json"])
@pytest.mark.parametrize("operation", ["preflight", "snapshot", "state"])
def test_export_lock_exception_does_not_whitelist_other_locks_or_unfinished_imports(tmp_path, entry, operation):
    root = tmp_path / "source"
    _seed_archive(root)
    (root / entry).write_bytes(EXPORT_LOCK_MARKER)
    with pytest.raises(ValueError, match="unsupported source root"):
        if operation == "preflight":
            migration_preflight(root)
        elif operation == "snapshot":
            save_snapshot(root, tmp_path / "snapshot.zip")
        else:
            save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)


@pytest.mark.parametrize("kind", ["directory", "hardlink", "symlink", "io-failure", "changed-after-lock"])
def test_runtime_fence_probe_refuses_unsafe_inode_or_failed_observation(tmp_path, monkeypatch, kind):
    runtime = tmp_path / ".artifact-runtime"
    runtime.mkdir()
    fence = runtime / ("." + "a" * 64 + ".archive-maintenance.lock")
    if kind == "directory":
        fence.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"")
        try:
            fence.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        fence.write_bytes(b"")
        if kind == "hardlink":
            os.link(fence, tmp_path / "other-link")
        elif kind in {"io-failure", "changed-after-lock"}:
            @contextmanager
            def racing_access(*args, **kwargs):
                if kind == "io-failure":
                    raise OSError("lock observation unavailable")
                fence.write_bytes(b"new evidence")
                yield
            monkeypatch.setattr(policy, "archive_access", racing_access)
    with pytest.raises(policy.RuntimeRootPolicyError):
        policy.inspect_runtime_root(tmp_path)


@pytest.mark.parametrize("kind", ["unknown", "oversized", "invalid-utf8", "invalid-json", "directory", "symlink"])
def test_reservation_receipts_never_discard_malformed_or_unreadable_state(tmp_path, kind):
    directory = tmp_path / policy.ASR_INPUT_RESERVATIONS
    directory.mkdir()
    receipt = directory / ("a" * 32 + ".json")
    if kind == "unknown":
        (directory / "private.json").write_bytes(b"{}")
    elif kind == "directory":
        receipt.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"{}")
        try:
            receipt.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        receipt.write_bytes({"oversized": b" " * (64 * 1024 + 1), "invalid-utf8": b"\xff", "invalid-json": b"{"}[kind])
    with pytest.raises(policy.RuntimeRootPolicyError):
        policy.inspect_runtime_root(tmp_path)
    assert any(directory.iterdir())


def test_runtime_roots_are_deduplicated_and_missing_roots_have_no_exclusion(tmp_path):
    assert policy.inspect_runtime_root(tmp_path / "missing") == []
    (tmp_path / policy.ARTIFACT_RUNTIME).mkdir()
    assert len(policy.inspect_runtime_roots([tmp_path, tmp_path])) == 1


def test_runtime_directory_cannot_be_an_ordinary_file(tmp_path):
    (tmp_path / policy.ARTIFACT_RUNTIME).write_bytes(b"evidence")
    with pytest.raises(policy.RuntimeRootPolicyError, match="not a directory"):
        policy.inspect_runtime_root(tmp_path)


def test_task_state_rejects_runtime_changes_during_evidence_capture(tmp_path, monkeypatch):
    root = tmp_path / "source"
    _seed_archive(root)
    original = task_state._copy_file
    def changed_capture(*args, **kwargs):
        original(*args, **kwargs)
        (root / policy.ARTIFACT_RUNTIME).mkdir(exist_ok=True)
    monkeypatch.setattr(task_state, "_copy_file", changed_capture)
    with pytest.raises(ValueError, match="runtime exclusions changed"):
        save_task_state(root, tmp_path / "state.zip", no_external_control_state=True)
    assert not (tmp_path / "state.zip").exists()


def test_snapshot_translates_a_failed_runtime_probe_to_snapshot_error(tmp_path, monkeypatch):
    root = tmp_path / "source"
    _seed_archive(root)
    def unavailable(*args):
        raise policy.RuntimeRootPolicyError("runtime probe unavailable")
    monkeypatch.setattr(archive_snapshot, "inspect_runtime_roots", unavailable)
    with pytest.raises(SnapshotError, match="runtime probe unavailable"):
        save_snapshot(root, tmp_path / "snapshot.zip")


@pytest.mark.parametrize("entry", [None, {},
    {"path": "../private-state", "category": "runtime-export-lock", "state": "stopped-export-lock", "count": 1},
    {"path": policy.PRESERVED_IMPORT_LOCK, "category": "runtime-export-lock", "state": "empty-runtime-directory", "count": 1},
    {"path": policy.PRESERVED_IMPORT_LOCK, "category": "runtime-export-lock", "state": "stopped-export-lock", "count": 1, "base": ""},
])
def test_runtime_manifest_exclusions_have_strict_structure(entry):
    with pytest.raises(policy.RuntimeRootPolicyError):
        policy.validate_runtime_exclusions(entry if entry is None else [entry])


@pytest.mark.skipif(os.name != "nt", reason="Windows junction policy")
@pytest.mark.parametrize("location", ["root", "runtime-directory", "fence", "reservation"])
def test_runtime_root_policy_refuses_real_windows_junctions(tmp_path, location):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "source"
    if location == "root":
        linked = root
    else:
        root.mkdir()
        if location == "runtime-directory":
            linked = root / policy.ARTIFACT_RUNTIME
        elif location == "fence":
            (root / policy.ARTIFACT_RUNTIME).mkdir()
            linked = root / policy.ARTIFACT_RUNTIME / ("." + "a" * 64 + ".archive-maintenance.lock")
        else:
            (root / policy.ASR_INPUT_RESERVATIONS).mkdir()
            linked = root / policy.ASR_INPUT_RESERVATIONS / ("a" * 32 + ".json")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(target)], check=True, capture_output=True)
    try:
        with pytest.raises(policy.RuntimeRootPolicyError, match="junction"):
            policy.inspect_runtime_root(root)
    finally:
        linked.rmdir()
