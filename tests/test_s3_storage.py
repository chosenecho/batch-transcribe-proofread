"""SDK-to-HTTP tests use synthetic bytes and a disposable loopback endpoint."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytest.importorskip("boto3")

from bili_asr import cli
from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.remote_storage import RemoteStorageError
from bili_asr.s3_storage import S3Backend, S3Binding, read_s3_binding
from bili_asr.services.artifact_catalog_upgrade import authority_fingerprints
from bili_asr.services.remote_artifacts import abort_remote_upload, push_remote_package, restore_remote_artifact
from bili_asr.services.reference_backup import check_reference_backup, restore_reference_backup, save_reference_backup
from bili_asr.storage.artifact_catalog import ArtifactCatalog
from bili_asr.storage.artifact_s3_upgrade import upgrade_catalog_s3
from bili_asr.storage.database import connect_database
from bili_asr.storage.extension_contracts import artifact_catalog_version, require_artifact_catalog
from bili_asr.storage.schema_contract import SchemaContractError
from bili_asr.storage.s3_upload_state import S3UploadState
from bili_asr.storage.snapshots import validate_snapshot_database
from tests.fixtures.s3_endpoint import s3_endpoint
from tests.test_artifact_restore import _setup


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv("S3_TEST_ACCESS", "synthetic-access")
    monkeypatch.setenv("S3_TEST_SECRET", "SENTINEL-never-in-evidence")
    with s3_endpoint() as (endpoint, state):
        binding = S3Binding("r2-cold", endpoint, "private-test", "S3_TEST_ACCESS", "S3_TEST_SECRET",
                            allow_local_http=True, part_size=5 * 1024**2)
        with S3Backend(binding) as backend:
            yield backend, state


class MemoryState:
    def __init__(self):
        self.state = None
    def load(self):
        return self.state
    def save(self, state):
        self.state = json.loads(json.dumps(state))
    def clear(self):
        self.state = None


def payload(tmp_path, size=1024):
    content = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "input.zip"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return path, content, digest, f"packages/artifact-{digest}.zip"


def test_single_upload_readback_range_zero_and_idempotence(remote, tmp_path):
    backend, state = remote
    path, content, digest, key = payload(tmp_path)
    assert backend.prepare()["available"]
    first = backend.upload(path, key=key, sha256=digest, size=len(content))
    second = backend.upload(path, key=key, sha256=digest, size=len(content))
    assert not first["reused"] and second["reused"]
    assert first["source_released"] is False and first["verification_scope"] == "entire-package-client-readback"
    assert [call[0] for call in state.calls].count("PUT") == 1
    target = io.BytesIO()
    report = backend.download(key=key, sha256=digest, size=len(content), offset=17, count=31, destination=target)
    assert target.getvalue() == content[17:48] and not report["whole_object_verified"]
    assert backend.download(key=key, sha256=digest, size=len(content), offset=12, count=0, destination=io.BytesIO())["size"] == 0
    encoded = json.dumps(first)
    assert "SENTINEL" not in encoded and backend.binding.endpoint not in encoded and backend.binding.bucket not in encoded
    assert backend.binding.endpoint not in repr(backend.binding)


def test_configured_bounded_ranges_verify_entire_object_and_preserve_identity(remote, tmp_path):
    backend, state = remote
    path, content, digest, key = payload(tmp_path, 2 * 1024**2 + 23)
    backend.upload(path, key=key, sha256=digest, size=len(content))
    binding = dataclasses.replace(backend.binding, download_range_bytes=1024**2)
    assert binding.instance_id == backend.binding.instance_id
    with S3Backend(binding) as ranged:
        original_call, requests = ranged._call, []
        def measured(operation, **arguments):
            if operation == "get_object": requests.append(arguments["Range"])
            return original_call(operation, **arguments)
        ranged._call = measured
        target = io.BytesIO()
        receipt = ranged.download(key=key, sha256=digest, size=len(content), destination=target)
        assert target.getvalue() == content and receipt["whole_object_verified"]
        assert requests == ["bytes=0-1048575", "bytes=1048576-2097151", "bytes=2097152-2097174"]
        with pytest.raises(RemoteStorageError, match="checksum_mismatch"):
            ranged.download(key=key, sha256="0"*64, size=len(content), destination=io.BytesIO())


def test_multipart_interruption_resumes_only_verified_parts_and_readbacks(remote, tmp_path):
    backend, state = remote
    path, content, digest, key = payload(tmp_path, 11 * 1024**2)
    receipts = MemoryState()
    state.fail_part = 2
    with pytest.raises(RemoteStorageError, match="permission_denied"):
        backend.upload(path, key=key, sha256=digest, size=len(content), checkpoint_store=receipts)
    assert len(receipts.state["parts"]) == 1 and state.uploads and not state.objects
    first_upload = receipts.state["upload_id"]
    state.fail_part = None
    result = backend.upload(path, key=key, sha256=digest, size=len(content), checkpoint_store=receipts)
    assert not result["source_released"] and receipts.state is None and not state.uploads
    assert state.objects["/private-test/bili-asr/" + key] == content
    assert [call[0] for call in state.calls].count("PUT") == 4  # part 1, failed 2, retried 2, part 3
    assert first_upload == "upload-1"
    assert path.read_bytes() == content


def test_explicit_abort_missing_upload_and_resumed_part_size(remote, tmp_path):
    backend, state = remote
    path, content, digest, key = payload(tmp_path, 6 * 1024**2)
    receipts = MemoryState()
    state.fail_part = 2
    with pytest.raises(RemoteStorageError):
        backend.upload(path, key=key, sha256=digest, size=len(content), checkpoint_store=receipts)
    with S3Backend(dataclasses.replace(backend.binding, part_size=6 * 1024**2)) as other:
        with pytest.raises(RemoteStorageError, match="resume_part_size_mismatch"):
            other.upload(path, key=key, sha256=digest, size=len(content), checkpoint_store=receipts)
    state.uploads.clear()
    with pytest.raises(RemoteStorageError, match="upload_missing"):
        backend.upload(path, key=key, sha256=digest, size=len(content), checkpoint_store=receipts)
    assert receipts.state is None
    state.fail_part = None
    state.uploads["gone"] = {}
    receipts.save({"upload_id": "gone", "part_size": backend.binding.part_size, "parts": []})
    assert backend.abort(key=key, checkpoint_store=receipts)["aborted"]
    assert not state.uploads and receipts.state is None
    assert not backend.abort(key=key, checkpoint_store=receipts)["aborted"]
    receipts.save({"upload_id": "already-gone", "part_size": backend.binding.part_size, "parts": []})
    assert backend.abort(key=key, checkpoint_store=receipts)["aborted"]


@pytest.mark.parametrize("fault,code", [("deny", "permission_denied"), ("corrupt", "checksum_mismatch"), ("short", "size_mismatch"), ("missing", "object_missing")])
def test_missing_tampered_short_or_denied_bytes_never_verify(remote, tmp_path, fault, code):
    backend, state = remote
    path, content, digest, key = payload(tmp_path)
    backend.upload(path, key=key, sha256=digest, size=len(content))
    if fault == "missing":
        state.objects.clear()
    else:
        setattr(state, fault, True)
    with pytest.raises(RemoteStorageError, match=code):
        backend.download(key=key, sha256=digest, size=len(content), destination=io.BytesIO())
    assert path.read_bytes() == content
    if fault != "missing":
        with pytest.raises(RemoteStorageError, match=code):
            backend.upload(path, key=key, sha256=digest, size=len(content))


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://public.example"}, {"endpoint": "https://name:secret@private.example"},
    {"endpoint": "https://private.example/?token=secret"}, {"endpoint": "https://private.example/path"},
    {"endpoint": "http://public.example", "allow_local_http": True}, {"bucket": "../escape"},
    {"bucket": "bucket..name"}, {"target_id": "local"}, {"target_id": "../escape"},
    {"prefix": "../escape"}, {"access_key_env": "inline secret"}, {"private_bucket": False},
    {"part_size": True}, {"part_size": 1}, {"timeout": False}, {"timeout": 301},
    {"region": "region\nsecret"}, {"addressing_style": "invalid"},
])
def test_binding_confinement(remote, changes):
    backend, _ = remote
    with pytest.raises((RemoteStorageError, ValueError)):
        dataclasses.replace(backend.binding, **changes)


def test_binding_environment_rotation_and_cli_probe(remote, tmp_path, capsys, monkeypatch):
    backend, _ = remote
    path = tmp_path / "binding.json"
    document = {**dataclasses.asdict(backend.binding), "backend": "s3-compatible/v1"}
    path.write_text(json.dumps(document))
    assert read_s3_binding(path).instance_id == backend.binding.instance_id
    assert cli.main(["remote", "probe", "--binding", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["instance_id"] == backend.binding.instance_id
    monkeypatch.setenv("S3_TEST_SECRET", "ROTATED-secret")
    with S3Backend(read_s3_binding(path)) as rotated:
        assert rotated.probe()["instance_id"] == report["instance_id"]
    monkeypatch.delenv("S3_TEST_SECRET")
    assert cli.main(["remote", "probe", "--binding", str(path)]) == 1
    assert "credential_missing" in capsys.readouterr().err
    path.write_text(json.dumps({**document, "secret_key": "must-reject"}))
    with pytest.raises(RemoteStorageError, match="invalid_binding"):
        read_s3_binding(path)
    path.write_bytes(b" " * 16385)
    with pytest.raises(RemoteStorageError, match="binding_too_large"):
        read_s3_binding(path)
    disconnected = S3Backend(backend.binding)
    with pytest.raises(RemoteStorageError, match="not_connected"):
        disconnected.probe()


def test_invalid_upload_download_and_partial_destination(remote, tmp_path):
    backend, _ = remote
    path, content, digest, key = payload(tmp_path)
    with pytest.raises(RemoteStorageError, match="content_addressed_key_required"):
        backend.upload(path, key="packages/fixed.zip", sha256=digest, size=len(content))
    with pytest.raises(RemoteStorageError, match="source_changed"):
        backend.upload(path, key=key, sha256=digest, size=len(content) + 1)
    for options in ({"offset": -1}, {"count": True}, {"offset": len(content) + 1}, {"sha256": "not-a-digest"}):
        args = {"key": key, "sha256": digest, "size": len(content), "destination": io.BytesIO(), **options}
        with pytest.raises(RemoteStorageError, match="invalid_download"):
            backend.download(**args)
    with pytest.raises(RemoteStorageError, match="invalid_key"):
        backend.download(key="../escape", sha256=digest, size=len(content), destination=io.BytesIO())
    backend.upload(path, key=key, sha256=digest, size=len(content))
    class ShortDestination(io.BytesIO):
        def write(self, block):
            return super().write(block[:7])
    destination = ShortDestination()
    backend.download(key=key, sha256=digest, size=len(content), destination=destination)
    assert destination.getvalue() == content
    class BrokenDestination:
        def write(self, _):
            return 0
    with pytest.raises(RemoteStorageError, match="destination_write_failed"):
        backend.download(key=key, sha256=digest, size=len(content), destination=BrokenDestination())
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    zero = hashlib.sha256(b"").hexdigest()
    backend.upload(empty, key=f"packages/artifact-{zero}.zip", sha256=zero, size=0)


def test_catalog_v2_migration_preserves_rows_frozen_sql_and_rollback_backup(tmp_path):
    roots, _, _, _, _, _ = _setup(tmp_path)
    backup = tmp_path / "before.db"
    with closing(connect_database(roots.archive_root / "archive.db", readonly=True)) as connection:
        before = {row["table"]: row for row in authority_fingerprints(connection)}
    result = upgrade_catalog_s3(roots, backup)
    assert result["changed"] and result["backup_retained"]
    with closing(connect_database(roots.archive_root / "archive.db", readonly=True)) as connection:
        assert artifact_catalog_version(connection) == 2
        after = {row["table"]: row for row in authority_fingerprints(connection)}
        assert {k: v for k, v in after.items() if k in before and k not in {"artifact_targets", "artifact_storage_contract"}} == {
            k: v for k, v in before.items() if k not in {"artifact_targets", "artifact_storage_contract"}}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(connect_database(backup, readonly=True)) as connection:
        assert artifact_catalog_version(connection) == 1
        assert {row["table"]: row for row in authority_fingerprints(connection)} == before
    assert validate_snapshot_database(backup) == result["backup_contract"]
    assert validate_snapshot_database(roots.archive_root / "archive.db") == result["database_contract"]
    assert upgrade_catalog_s3(roots, tmp_path / "unnecessary.db")["reused"]
    assert not (tmp_path / "unnecessary.db").exists()
    with pytest.raises(ValueError, match="new file"):
        upgrade_catalog_s3(roots, backup)
    with pytest.raises(ValueError, match="outside"):
        upgrade_catalog_s3(roots, roots.archive_root / "bad.db")


def test_s3_catalog_push_reference_backup_and_empty_directory_restore(remote, tmp_path, monkeypatch):
    backend, state = remote
    roots, _, audio, _, package, _ = _setup(tmp_path)
    package_path = Path(package["package_path"])
    with pytest.raises(RemoteStorageError, match="explicit_catalog_v2"):
        push_remote_package(roots, package_path, backend)
    from bili_asr.storage.workflow_batches import install_batches, BatchRepository
    from bili_asr.storage.publication_packages import install_packages
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session:
        install_packages(session.connection)
        install_batches(session.connection)
        job_id = session.connection.execute("SELECT job_id FROM workflow_jobs WHERE status!='running' LIMIT 1").fetchone()[0]
        batches = BatchRepository(session.connection)
        batch = batches.create([job_id], desired_workers=10, actor="fixture", reason="synthetic reviewed scope")
        batches.set_state(batch["batch_id"], paused=True, actor="fixture", reason="preserve pause through cold recovery")
        with session.connection:
            ArtifactCatalog(session.connection).pin_object(audio["sha256"], "synthetic investigation", pin_id="retained-hold")
    upgrade_catalog_s3(roots, tmp_path / "before.db")
    receipt = push_remote_package(roots, package_path, backend)
    assert receipt["source_released"] is False and receipt["object_count"] == 1
    assert push_remote_package(roots, package_path, backend)["reused"]
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session, session.connection:
        catalog = ArtifactCatalog(session.connection)
        assert session.connection.execute("SELECT kind FROM artifact_targets WHERE target_id=?", (backend.binding.target_id,)).fetchone()[0] == "s3"
        assert session.connection.execute("SELECT state FROM artifact_transfers WHERE transfer_id LIKE 's3-%'").fetchone()[0] == "complete"
        assert session.connection.execute("SELECT count(*) FROM artifact_multipart_uploads").fetchone()[0] == 0
        with pytest.raises(ValueError, match="immutable"):
            catalog.register_target(backend.binding.target_id, kind="s3", instance_id="f" * 32)
    reference = tmp_path / "reference.zip"
    holds = {"version": 1, "holds": {"part:1": ["investigation"]}}
    save_reference_backup(roots, reference, holds=holds, target_instances={backend.binding.target_id: backend.binding.instance_id})
    assert check_reference_backup(reference, online=True, remote_backends={backend.binding.target_id: backend})["external_verified"]
    from tests.test_reference_backup import _rewrite
    for name, instances in (("missing", {}), ("changed", {backend.binding.target_id: "0" * 32})):
        forged = tmp_path / (name + "-instance.zip")
        _rewrite(reference, forged, lambda manifest: manifest.update(target_instances=instances))
        with pytest.raises(ValueError, match="retained S3 target instance"):
            check_reference_backup(forged)
    destination = tmp_path / "empty"
    restored = restore_reference_backup(reference, destination, source_workers_stopped=True)
    assert not restored["ready_to_run"] and not restored["retry_started"]
    with closing(connect_database(destination / "archive.db", readonly=True)) as connection:
        before = [row for row in authority_fingerprints(connection) if not row["table"].startswith("artifact_")]
    report = restore_remote_artifact(ArtifactRoots.of(destination), audio["sha256"], audio["storage_key"], backend)
    assert report["remote_package_verified"] and not report["automatic_retry"]
    assert hashlib.sha256((destination / audio["storage_key"]).read_bytes()).hexdigest() == audio["sha256"]
    with closing(connect_database(destination / "archive.db", readonly=True)) as connection:
        assert [row for row in authority_fingerprints(connection) if not row["table"].startswith("artifact_")] == before
        assert connection.execute("SELECT state FROM workflow_batches").fetchone()[0] == "paused"
        assert connection.execute("SELECT released_at FROM artifact_pins WHERE pin_id='retained-hold'").fetchone()[0] is None
    assert json.loads(next((destination / "documents/reference-recovery").rglob("holds.json")).read_bytes()) == holds
    state.corrupt = True
    with pytest.raises(RemoteStorageError, match="checksum_mismatch"):
        restore_remote_artifact(roots, audio["sha256"], audio["storage_key"], backend)
    assert not (roots.archive_root / audio["storage_key"]).exists()


def test_failed_push_retains_ledger_and_abort_requires_exact_instance(remote, tmp_path):
    backend, state = remote
    roots, _, _, _, package, _ = _setup(tmp_path)
    upgrade_catalog_s3(roots, tmp_path / "before.db")
    state.deny = True
    with pytest.raises(RemoteStorageError, match="permission_denied"):
        push_remote_package(roots, Path(package["package_path"]), backend)
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        row = session.connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id LIKE 's3-%'").fetchone()
        transfer_id = row["transfer_id"]
        assert row["state"] == "failed" and row["error_code"] == "permission_denied"
        assert session.connection.execute("SELECT count(*) FROM artifact_replicas WHERE target_id=?", (backend.binding.target_id,)).fetchone()[0] == 0
    state.deny = False
    assert not abort_remote_upload(roots, backend, transfer_id)["source_released"]
    wrong = S3Backend(dataclasses.replace(backend.binding, prefix="other-prefix"))
    with pytest.raises(RemoteStorageError, match="target_instance_mismatch"):
        abort_remote_upload(roots, wrong, transfer_id)
    with pytest.raises(RemoteStorageError, match="resumable_transfer_required"):
        abort_remote_upload(roots, backend, "unknown")


def test_upload_state_roundtrip_and_rejects_corrupt_receipts(tmp_path):
    roots, *_ = _setup(tmp_path)
    upgrade_catalog_s3(roots, tmp_path / "before.db")
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.WRITE) as session:
        with session.connection:
            catalog = ArtifactCatalog(session.connection)
            catalog.register_target("local", kind="local")
            catalog.begin_transfer("resume-test", kind="copy", plan_sha256="a" * 64, target_id="local")
        state = S3UploadState(session.connection, "resume-test")
        assert state.load() is None
        document = {"upload_id": "upload-synthetic", "part_size": 5 * 1024**2,
                    "parts": [{"PartNumber": 1, "ETag": "opaque", "sha256": "b" * 64}]}
        state.save(document)
        assert state.load() == document
        state.save(document)
        with session.connection:
            session.connection.execute("UPDATE artifact_multipart_uploads SET parts_json='[{}]'")
        with pytest.raises(ValueError, match="invalid multipart"):
            state.load()
        state.clear()
        assert state.load() is None
        session.connection.execute("DROP TRIGGER immutable_artifact_remote_instance_update")
        with pytest.raises(SchemaContractError, match="missing or altered"):
            require_artifact_catalog(session.connection)


def test_catalog_upgrade_failure_rolls_back_ddl_and_preserves_verified_backup(tmp_path, monkeypatch):
    from bili_asr.storage import artifact_s3_upgrade as migration
    roots, *_ = _setup(tmp_path)
    old_contract = validate_snapshot_database(roots.archive_root / "archive.db")
    original = migration.migration_statements
    def invalid_foreign_key():
        yield from original()
        yield "INSERT INTO artifact_remote_instances VALUES ('missing-target','" + "a" * 32 + "')"
    monkeypatch.setattr(migration, "migration_statements", invalid_foreign_key)
    backup = tmp_path / "rollback.db"
    with pytest.raises(ValueError, match="foreign-key"):
        migration.upgrade_catalog_s3(roots, backup)
    assert validate_snapshot_database(roots.archive_root / "archive.db") == old_contract
    assert validate_snapshot_database(backup) == old_contract
    with closing(connect_database(roots.archive_root / "archive.db", readonly=True)) as connection:
        assert artifact_catalog_version(connection) == 1
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_remote_instances'").fetchone() is None


def test_s3_cli_upgrade_push_restore_abort_and_reference_instance_check(remote, tmp_path, capsys):
    backend, state = remote
    roots, _, audio, _, package, _ = _setup(tmp_path)
    args = ["--archive-root", str(roots.archive_root)]
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({**dataclasses.asdict(backend.binding), "backend": "s3-compatible/v1"}))
    assert cli.main(["remote", "upgrade-catalog", *args, "--backup", str(tmp_path / "before.db")]) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    state.deny = True
    assert cli.main(["remote", "push", *args, "--binding", str(binding), "--package", package["package_path"]]) == 1
    assert "permission_denied" in capsys.readouterr().err
    with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ) as session:
        transfer_id = session.connection.execute("SELECT transfer_id FROM artifact_transfers WHERE transfer_id LIKE 's3-%'").fetchone()[0]
    state.deny = False
    assert cli.main(["remote", "abort-upload", *args, "--binding", str(binding), "--transfer-id", transfer_id]) == 0
    assert not json.loads(capsys.readouterr().out)["source_released"]
    assert cli.main(["remote", "push", *args, "--binding", str(binding), "--package", package["package_path"]]) == 0
    capsys.readouterr()
    with pytest.raises(RemoteStorageError, match="resumable_transfer"):
        abort_remote_upload(roots, backend, transfer_id)
    assert cli.main(["reference", "save", *args, "--out", str(tmp_path / "reference.zip"), "--remote-binding", str(binding)]) == 0
    capsys.readouterr()
    assert cli.main(["reference", "check", "--file", str(tmp_path / "reference.zip"), "--online", "--remote-binding", str(binding)]) == 0
    assert json.loads(capsys.readouterr().out)["external_verified"]
    assert cli.main(["remote", "restore", *args, "--binding", str(binding), "--object-id", audio["sha256"], "--storage-key", audio["storage_key"]]) == 0
    assert json.loads(capsys.readouterr().out)["remote_package_verified"]
    with pytest.raises(ValueError, match="retained catalog instance"):
        save_reference_backup(roots, tmp_path / "bad-instance.zip", target_instances={backend.binding.target_id: "f" * 32})
    wrong = S3Backend(dataclasses.replace(backend.binding, prefix="other-prefix"))
    with pytest.raises(RemoteStorageError, match="target_instance_mismatch"):
        restore_remote_artifact(roots, audio["sha256"], audio["storage_key"], wrong)


@pytest.mark.parametrize("provider_code,expected", [
    ("InvalidAccessKeyId", "authentication"), ("SignatureDoesNotMatch", "authentication"),
    ("NoSuchBucket", "bucket_missing"), ("RequestTimeout", "timeout"),
    ("403", "permission_denied"), ("404", "object_missing"), ("UnknownProviderError", "s3_operation_failed")])
def test_sdk_errors_are_classified_without_connection_or_secret_text(remote, provider_code, expected):
    from botocore.exceptions import ClientError
    backend, _ = remote
    class Failed:
        def head_bucket(self, **_):
            raise ClientError({"Error": {"Code": provider_code, "Message": "SENTINEL-secret https://private/?token=secret"}}, "HeadBucket")
    original = backend.client
    try:
        backend.client = Failed()
        with pytest.raises(RemoteStorageError, match=expected) as failure:
            backend.probe()
        assert "SENTINEL" not in str(failure.value) and "private" not in str(failure.value)
    finally:
        backend.client = original


def test_sdk_connection_timeout_and_stream_failure_are_safely_classified(remote):
    from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
    backend, _ = remote
    class Failed:
        def head_bucket(self, **_):
            raise EndpointConnectionError(endpoint_url="https://private/?token=SENTINEL")
        def get_object(self, **_):
            return {"ContentLength": 1, "Body": self}
        def read(self, _):
            raise ReadTimeoutError(endpoint_url="https://private/?token=SENTINEL")
        def close(self):
            self.closed = True
    original = backend.client
    try:
        backend.client = Failed()
        with pytest.raises(RemoteStorageError, match="connection_or_timeout"):
            backend.probe()
        with pytest.raises(RemoteStorageError, match="download_interrupted"):
            backend.download(key="packages/synthetic.zip", sha256="a" * 64, size=1, destination=io.BytesIO())
        assert backend.client.closed
    finally:
        backend.client = original


def test_old_maintenance_media_never_mislabels_s3_as_ssh(remote, tmp_path):
    import time
    from bili_asr.services.maintenance_media import ReferenceCapture
    backend, _ = remote
    capture = ReferenceCapture(ArtifactRoots.of(tmp_path), {}, {backend.binding.target_id: backend}, tmp_path, time.monotonic() + 60)
    with pytest.raises(ValueError, match="reference-backup format"):
        capture._package({"target_id": backend.binding.target_id, "relative_key": "packages/synthetic.zip", "package_sha256": "a" * 64})


def test_snapshot_contract_rejects_unknown_catalog_version_and_missing_s3_instance(tmp_path):
    from bili_asr.contracts.fingerprints import database_fingerprint
    with pytest.raises(ValueError, match="unsupported artifact storage contract"):
        database_fingerprint("archive", artifacts=1)
    with sqlite3.connect(":memory:") as invalid:
        invalid.executescript("CREATE TABLE artifact_storage_contract(singleton INTEGER,version INTEGER); INSERT INTO artifact_storage_contract VALUES(1,3);")
        with pytest.raises(SchemaContractError, match="unsupported contract marker"):
            artifact_catalog_version(invalid)
    roots, *_ = _setup(tmp_path)
    upgrade_catalog_s3(roots, tmp_path / "before.db")
    with closing(connect_database(roots.archive_root / "archive.db")) as connection:
        connection.execute("INSERT INTO artifact_targets VALUES ('unbound-s3','s3',0)")
        with pytest.raises(SchemaContractError, match="S3 target instance binding is missing"):
            require_artifact_catalog(connection)
        connection.rollback()


@pytest.mark.parametrize("failure", [None, "missing", "corrupt", "permission", "credential", "space", "instance", "binding-target", "unretained"])
def test_s3_worker_materializes_before_claim_and_faults_do_not_start_attempt(remote, tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from tests.support.artifact_online import archive as online_archive
    from tests.test_artifact_online import _seed_candidates
    from tests.test_artifact_transfer import plan, transfer
    from bili_asr.archive_maintenance import ArchiveBusyError
    from bili_asr.services.artifact_consumer import candidate_artifact_access
    from bili_asr.services.artifact_coordination import object_fence
    from bili_asr.storage.workflow import WorkflowRepository
    from bili_asr.workflow import WorkflowExecutor
    from bili_asr.workflow_models import JobKind
    backend, state = remote
    archive = online_archive.__wrapped__(tmp_path)
    transfer(archive, plan(archive), mode="offload")
    upgrade_catalog_s3(archive.roots, tmp_path / "before.db")
    package = next(archive.target.rglob("*.zip"))
    push_remote_package(archive.roots, package, backend)
    _seed_candidates(archive)
    binding = tmp_path / "worker-s3.json"
    document = {**dataclasses.asdict(backend.binding), "backend": "s3-compatible/v1"}
    if failure == "missing":
        state.objects.clear()
    elif failure == "corrupt":
        state.corrupt = True
    elif failure == "permission":
        state.deny = True
    elif failure == "credential":
        monkeypatch.delenv("S3_TEST_SECRET")
    elif failure == "space":
        monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    elif failure == "instance":
        document["prefix"] = "wrong-instance"
    elif failure == "binding-target":
        document["target_id"] = "other-target"
    elif failure == "unretained":
        document["target_id"] = "other-target"
    binding.write_text(json.dumps(document))
    identity = "other-target" if failure == "unretained" else backend.binding.target_id
    with ArchiveSession(archive.root, mode=ArchiveAccessMode.WRITE) as session:
        repository = WorkflowRepository(session.connection)
        calls = []
        def handler(job):
            calls.append(job.job_id)
            assert ArtifactCatalog(session.connection).pinned(archive.digest)
            assert (archive.root / "audio/input.m4a").read_bytes() == archive.data
            with pytest.raises(ArchiveBusyError), object_fence(archive.roots, archive.digest, exclusive=True):
                pass
            return {}
        executor = WorkflowExecutor(repository, worker_id="s3-worker-test", handlers={JobKind.ASR: handler},
            kinds=(JobKind.ASR,), candidate_access=lambda candidate: candidate_artifact_access(
                session.connection, repository, archive.roots, candidate, storage_targets={identity: binding}))
        report = executor.run()
        assert not report.failed
        if failure is None:
            assert report.succeeded == 1 and calls == ["asr"]
            assert session.connection.execute("SELECT attempt_count FROM workflow_jobs WHERE job_id='asr'").fetchone()[0] == 1
        else:
            assert report.blocked == 1 and not calls
            assert session.connection.execute("SELECT status,attempt_count FROM workflow_jobs WHERE job_id='asr'").fetchone()[:] == ("queued", 0)
            assert session.connection.execute("SELECT COUNT(*) FROM workflow_attempts WHERE job_id='asr'").fetchone()[0] == 0
            before = len(state.calls)
            assert not executor.run().failed
            assert len(state.calls) == before
        assert not ArtifactCatalog(session.connection).pinned(archive.digest)
        assert session.connection.execute("SELECT COUNT(*) FROM artifact_reservations WHERE released_at IS NULL").fetchone()[0] == 0
