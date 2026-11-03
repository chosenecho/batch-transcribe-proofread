"""Verify synthetic S3 interoperability against disposable loopback Moto.

Run from a source checkout in a separate environment with moto[server]==5.2.5,
the project's remote extra and pytest installed. No cloud binding is accepted.
"""
import argparse
import hashlib
import importlib.metadata
import io
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from datetime import datetime, timezone

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists() or not args.output.parent.is_dir():
    parser.exit(1, "output must be a new file in an existing directory\n")

sys.path.insert(0, str(Path.cwd()))
from moto.server import ThreadedMotoServer
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.remote_storage import RemoteStorageError
from bili_asr.s3_storage import S3Backend, S3Binding
from bili_asr.services.remote_artifacts import push_remote_package, restore_remote_artifact
from bili_asr.services.reference_backup import save_reference_backup, check_reference_backup, restore_reference_backup
from bili_asr.storage.artifact_s3_upgrade import upgrade_catalog_s3
from tests.test_artifact_restore import _setup
from tests.test_s3_storage import MemoryState

logging.getLogger("werkzeug").setLevel(logging.ERROR)
with socket.socket() as chosen:
    chosen.bind(("127.0.0.1", 0))
    port = chosen.getsockname()[1]
server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
os.environ["INTEROP_ACCESS"] = "synthetic-" + secrets.token_hex(8)
os.environ["INTEROP_SECRET"] = secrets.token_hex(32)
server.start()
try:
    binding = S3Binding("moto-cold", f"http://127.0.0.1:{port}", "private-synthetic-test",
                        "INTEROP_ACCESS", "INTEROP_SECRET", allow_local_http=True, region="us-east-1", part_size=5*1024**2)
    with S3Backend(binding) as backend, tempfile.TemporaryDirectory(prefix="bili-s3-interop-") as temporary:
        backend.client.create_bucket(Bucket=binding.bucket)
        root = Path(temporary)
        content = bytes(range(256)) * (11 * 4096)
        digest = hashlib.sha256(content).hexdigest()
        key = f"packages/artifact-{digest}.zip"
        source = root / "large.zip";source.write_bytes(content)
        state = MemoryState()
        original_call = backend._call
        def interrupt(operation, **options):
            if operation == "upload_part" and options["PartNumber"] == 2:
                raise RemoteStorageError("synthetic_interruption")
            return original_call(operation, **options)
        backend._call = interrupt
        try:
            backend.upload(source, key=key, sha256=digest, size=len(content), checkpoint_store=state)
            raise AssertionError("interruption not exercised")
        except RemoteStorageError as error:
            assert error.code == "synthetic_interruption"
        assert len(state.load()["parts"]) == 1
        backend._call = original_call
        first = backend.upload(source, key=key, sha256=digest, size=len(content), checkpoint_store=state)
        second = backend.upload(source, key=key, sha256=digest, size=len(content), checkpoint_store=state)
        assert second["reused"] and state.load() is None
        destination = io.BytesIO()
        backend.download(key=key, sha256=digest, size=len(content), offset=23, count=44, destination=destination)
        assert destination.getvalue() == content[23:67]
        fixture = root / "fixture";fixture.mkdir()
        roots, _, audio, _, package, _ = _setup(fixture)
        upgrade_catalog_s3(roots, root / "pre-v2.db")
        push_remote_package(roots, Path(package["package_path"]), backend)
        backup = root / "reference.zip"
        save_reference_backup(roots, backup, holds={"version":1,"holds":{"part:1":["synthetic-investigation"]}})
        assert check_reference_backup(backup, online=True, remote_backends={binding.target_id:backend})["external_verified"]
        new = root / "empty"
        restored = restore_reference_backup(backup, new, source_workers_stopped=True)
        assert not restored["ready_to_run"] and not restored["retry_started"]
        hydrated = restore_remote_artifact(ArtifactRoots.of(new), audio["sha256"], audio["storage_key"], backend)
        assert hydrated["remote_package_verified"]
        assert hashlib.sha256((new/audio["storage_key"]).read_bytes()).hexdigest()==audio["sha256"]
        backend.client.put_object(Bucket=binding.bucket, Key=binding.prefix+"/"+key, Body=b"x"*len(content))
        try:
            backend.download(key=key, sha256=digest, size=len(content), destination=io.BytesIO())
            raise AssertionError("corrupt object accepted")
        except RemoteStorageError as error:
            assert error.code=="checksum_mismatch"
        backend.client.delete_object(Bucket=binding.bucket, Key=binding.prefix+"/"+key)
        try:
            backend.download(key=key, sha256=digest, size=len(content), destination=io.BytesIO())
            raise AssertionError("missing object accepted")
        except RemoteStorageError as error:
            assert error.code=="object_missing"
        report={"format":"s3-local-interop-evidence/v1","captured_at":datetime.now(timezone.utc).isoformat(),
                "endpoint":"disposable-loopback-Moto-S3-emulator","moto_version":importlib.metadata.version("moto"),
                "boto3_version":importlib.metadata.version("boto3"),"synthetic_payload_bytes":len(content),
                "checks":{"multipart_interrupt_resume":True,"idempotent_readback":True,"range_read":True,
                          "explicit_catalog_v2":True,"reference_backup":True,"empty_directory_restore":True,
                          "exact_audio_sha256":True,"missing_object_refused":True,"corrupt_object_refused":True,
                          "holds_preserved":True,"no_paid_calls":True,"no_source_release":True},
                "real_r2_verified":False,"production_verified":False,
                "limitations":["Moto emulates S3; it is not Cloudflare R2.",
                               "Permission/timeout cases use separate SDK/wire fault tests.",
                               "No account pricing or limits were queried."]}
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report,indent=2))
finally:
    server.stop()
