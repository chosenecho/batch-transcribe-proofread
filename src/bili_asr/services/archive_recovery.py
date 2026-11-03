"""Read-only recovery readiness derived from archive facts and local runtime."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from bili_asr import __version__
from bili_asr.archive_session import ArchiveSession, ArchiveAccessMode
from bili_asr.artifact_inventory import require_no_links, require_regular_file, portable_artifact_parts, stream_hash
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.editorial import EditorialConfig
from bili_asr.editorial_provider import CREDENTIAL_NAMES, editorial_provider
from bili_asr.runtime_bindings import ModelBindingError, load_runtime_bindings
from bili_asr.services.archive_snapshot import verified_snapshot_reader, SnapshotError
from bili_asr.storage.database import connect_database
from bili_asr.storage.snapshots import required_artifacts, preview_interrupted_jobs, validate_snapshot_database
from bili_asr.storage.workflow import WorkflowRepository


@dataclass(frozen=True)
class RuntimeProbe:
    tools: frozenset[str]
    packages: frozenset[str]
    devices: frozenset[str]
    credential_names: frozenset[str]
    youtube_ready: bool = False

    @classmethod
    def local(cls, *, check_gpu: bool = False) -> RuntimeProbe:
        tools = frozenset(name for name in ("ffmpeg", "ffprobe") if shutil.which(name))
        def installed(name):
            try:
                return importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                return False
        packages = frozenset(name for name in ("torch", "transformers", "accelerate", "numpy", "soundfile", "soxr", "huggingface_hub")
                             if installed(name))
        devices = {"cpu"}
        if check_gpu and "torch" in packages:
            # Isolate driver initialization from the doctor process. Report only
            # device identifiers, never driver exception text or environment.
            code = "import torch,json; print(json.dumps({'count':torch.cuda.device_count() if torch.cuda.is_available() else 0,'hip':bool(torch.version.hip)}))"
            try:
                process = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, check=False)
                result = json.loads(process.stdout) if process.returncode == 0 else {}
                count = result.get("count", 0)
                if type(count) is int and 0 < count <= 256:
                    devices.add("cuda")
                    if result.get("hip"):
                        devices.add("rocm")
                    for index in range(count):
                        devices.add(f"cuda:{index}")
                        if result.get("hip"):
                            devices.add(f"rocm:{index}")
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        names = frozenset(name for name in CREDENTIAL_NAMES if os.environ.get(name, "").strip())
        youtube_ready = False
        return cls(tools, packages, frozenset(devices), names, youtube_ready)


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _model_available(name: str, revision: str | None) -> bool:
    path = Path(name)
    if path.is_dir():
        try:
            require_no_links(path)
            if require_regular_file(path / "config.json").st_size > 1024 * 1024:
                return False
            if not isinstance(json.loads((path / "config.json").read_text("utf-8")), dict):
                return False
            weights = [item for suffix in ("*.safetensors", "*.bin") for item in path.glob(suffix)]
            return bool(weights) and all(require_regular_file(item).st_size > 0 for item in weights)
        except (ValueError, OSError):
            return False
    if path.is_absolute() or name.startswith(("models/", "./", "../")) or "\\" in name:
        return False
    try:
        from huggingface_hub import snapshot_download
        cached = Path(snapshot_download(name, revision=revision or None, local_files_only=True))
        return (cached / "config.json").is_file() and any(cached.glob("*.safetensors"))
    except (ImportError, ValueError, OSError):
        return False


def _requirements(kind: str, platform: str, probe: RuntimeProbe) -> list[str]:
    if kind in {"audio", "subtitle"}:
        return ["handler:collection-removed"]
    missing = []
    if kind == "asr":
        missing.extend(f"tool:{name}" for name in ("ffmpeg", "ffprobe") if name not in probe.tools)
    if kind == "asr":
        missing.extend(f"package:{name}" for name in ("torch", "transformers", "accelerate", "numpy", "soundfile", "soxr") if name not in probe.packages)
    if kind == "index":
        missing.append("handler:index")
    return missing


def _editorial_requirements(connection, row, payload, probe) -> list[str]:
    """Resolve the exact frozen input, including a not-yet-bound ASR chain."""
    from bili_asr.storage.editorial_facts import input_for_proofread, load_input
    try:
        input_id = input_for_proofread(connection, row["job_id"])
        if input_id is None:
            if "asr_job_id" not in payload or not isinstance(payload.get("editorial_config"), dict):
                raise ValueError("missing frozen editorial configuration")
            values = payload["editorial_config"]
        else:
            prepared = load_input(connection, input_id)
            if prepared["snapshot"]["video_part_id"] != row["video_part_id"]:
                raise ValueError("frozen input belongs to another part")
            values = prepared["snapshot"]["config"]
        # Missing endpoint is not a historical default: every frozen editorial
        # snapshot stored it. Do not guess from today's global configuration.
        if not isinstance(values, dict) or "base_url" not in values:
            raise ValueError("missing frozen endpoint")
        config = EditorialConfig.from_snapshot(values)
        credential = editorial_provider(config.base_url).credential_name
    except (ValueError, TypeError, KeyError, AttributeError):
        return ["editorial:invalid-frozen-config"]
    return [] if credential in probe.credential_names else ["credential:" + credential]


def _job_report(connection, *, restored: bool, probe: RuntimeProbe, bindings=None, now: int | None = None) -> dict:
    """Batch prerequisites and keyset jobs; no second persisted status system."""
    from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2
    now = int(time.time()) if now is None else now
    repository = WorkflowRepository(connection)
    universal = runtime_contract(connection) == UNIVERSAL_V2
    profile_readiness = {}
    states = Counter()
    jobs = []
    cursor = ""
    while True:
        rows = connection.execute("SELECT * FROM workflow_jobs WHERE job_id>? ORDER BY job_id LIMIT 256", (cursor,)).fetchall()
        if not rows:
            break
        cursor = rows[-1]["job_id"]
        ids = [row["job_id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        blockers = {}
        for row in connection.execute(
            "SELECT d.job_id,p.job_id AS prerequisite,p.status FROM workflow_job_dependencies d "
            "JOIN workflow_jobs p ON p.job_id=d.prerequisite_job_id "
            f"WHERE d.job_id IN ({placeholders}) AND p.status<>'succeeded' ORDER BY d.job_id,p.job_id", ids):
            blockers.setdefault(row["job_id"], []).append({"job_id": row["prerequisite"], "status": row["status"]})
        platforms = {}
        part_ids = tuple({row["video_part_id"] for row in rows if row["video_part_id"] is not None})
        if part_ids:
            parts = ",".join("?" for _ in part_ids)
            source = "v_source_parts" if universal else "video_parts"
            field = "platform" if universal else "'bilibili' AS platform"
            platforms = {row[0]: row[1] for row in connection.execute(f"SELECT video_part_id,{field} FROM {source} WHERE video_part_id IN ({parts})", part_ids)}
        for row in rows:
            status = row["status"]
            if status in {"succeeded", "cancelled"}:
                state = "completed" if status == "succeeded" else "cancelled_terminal"
                states[state] += 1
                continue
            missing = _requirements(row["kind"], platforms.get(row["video_part_id"], "bilibili"), probe)
            valid_payload = True
            try:
                from bili_asr.workflow_models import JobKind
                from bili_asr.workflow_payloads import validate_payload
                payload = json.loads(row["payload_json"])
                validate_payload(JobKind(row["kind"]), payload,
                                 part_id=row["video_part_id"], profile_id=row["profile_id"])
            except (ValueError, TypeError, KeyError):
                valid_payload = False
                missing.append("payload:invalid")
            if row["kind"] == "proofread" and valid_payload:
                missing += _editorial_requirements(connection, row, payload, probe)
            binding_identity = None
            if row["kind"] == "asr":
                profile_id = row["profile_id"]
                if profile_id not in profile_readiness:
                    absent, identity = [], None
                    try:
                        config = repository.profile(profile_id).asr_config()
                        if bindings:
                            config, identity = bindings.resolve(config)
                        device = config.device.casefold()
                        if device not in probe.devices:
                            absent.append("device:" + device)
                        if not _model_available(config.model_name, config.model_revision):
                            absent.append("model:asr-unavailable")
                        if not _model_available(config.aligner_name, config.aligner_revision):
                            absent.append("model:aligner-unavailable")
                    except ModelBindingError:
                        absent.append("model:identity-unverified")
                    except (ValueError, TypeError, KeyError):
                        absent.append("profile:invalid-frozen-config")
                    profile_readiness[profile_id] = absent, identity
                absent, binding_identity = profile_readiness[profile_id]
                missing += absent
            dependencies = blockers.get(row["job_id"], [])
            if not valid_payload:
                state = "blocked_invalid_payload"
            elif status == "failed":
                state = "manual_retry"
            elif status == "running" and not restored:
                state = "running_owned" if row["lease_expires_at"] and row["lease_expires_at"] > now else "expired_lease"
            elif missing:
                state = "blocked_environment"
            elif dependencies:
                state = "waiting_dependency"
            elif row["available_at"] > now and not (restored and status == "running"):
                state = "waiting_schedule"
            else:
                state = "ready_now"
            states[state] += 1
            item = {"job_id": row["job_id"], "kind": row["kind"], "stored_status": status,
                    "derived_state": state, "attempt_count": row["attempt_count"],
                    "missing": sorted(set(missing)), "dependencies": dependencies}
            if binding_identity:
                item["runtime_binding"] = binding_identity
            jobs.append(item)
    return {"job_states": dict(sorted(states.items())), "pending_jobs": jobs,
            "runtime_ready": not any(job["missing"] for job in jobs),
            "credentials_verified_online": False}


def _needs_gpu(connection) -> bool:
    return connection.execute("SELECT 1 FROM workflow_jobs j JOIN workflow_asr_profiles p ON p.profile_id=j.profile_id "
                              "WHERE j.status IN ('queued','running','failed') AND (p.device LIKE 'cuda%' OR p.device LIKE 'rocm%') LIMIT 1").fetchone() is not None


def _inspect_verified(verified, *, probe, bindings) -> dict:
    with closing(connect_database(verified.database_path, readonly=True, must_exist=True)) as connection:
        readiness = _job_report(connection, restored=True, probe=probe or RuntimeProbe.local(check_gpu=_needs_gpu(connection)), bindings=bindings)
        changes = list(preview_interrupted_jobs(connection, verified.manifest["snapshot_id"]))
    return {"operation": "inspect", "data_complete": True, "snapshot_id": verified.manifest["snapshot_id"],
            "database_contract": verified.manifest["database_contract"], "producer_version": verified.manifest["producer_version"],
            "consumer_version": __version__, "file_count": len(verified.manifest["files"]),
            "recovery_changes": changes, "model_load_tested": False, **readiness}


def inspect_snapshot(snapshot: Path, *, probe: RuntimeProbe | None = None, runtime_bindings: Path | None = None) -> dict:
    bindings = load_runtime_bindings(runtime_bindings)
    with verified_snapshot_reader(snapshot) as verified:
        return _inspect_verified(verified, probe=probe, bindings=bindings)


def plan_restore(snapshot: Path, target: Path, *, probe: RuntimeProbe | None = None, runtime_bindings: Path | None = None) -> dict:
    """Validate package/target/free space without creating target directories."""
    source, destination = Path(os.path.abspath(snapshot)), Path(os.path.abspath(target))
    require_no_links(destination)
    if source.is_relative_to(destination):
        raise SnapshotError("restore target cannot contain its snapshot")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SnapshotError("restore target must be new or empty")
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    require_no_links(ancestor)
    before = require_regular_file(source)
    with source.open("rb") as stream:
        _size, digest = stream_hash(stream)
    bindings = load_runtime_bindings(runtime_bindings)
    with verified_snapshot_reader(source) as verified:
        report = _inspect_verified(verified, probe=probe, bindings=bindings)
        required = sum(entry["size"] for entry in verified.manifest["files"])
        db_size = next(entry["size"] for entry in verified.manifest["files"] if entry["path"] == "archive.db")
    after = require_regular_file(source)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SnapshotError("snapshot changed during planning")
    # Staging + rollback journal; reserve conservatively for recovery updates.
    required += 2 * db_size + 16 * 1024 * 1024
    free = shutil.disk_usage(ancestor).free
    report.update(operation="plan", target=str(destination), snapshot_sha256=digest,
                  required_free_bytes=required, available_free_bytes=free, target_ready=free >= required,
                  package_version=__version__, python_version=sys.version.split()[0],
                  installed_packages={name: _package_version(name) for name in ("bili-asr", "torch", "yt-dlp")})
    identity = {key: report[key] for key in ("snapshot_id", "snapshot_sha256", "database_contract", "target", "package_version")}
    report["plan_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report["apply_revalidates_bundle_and_target"] = True
    return report


def doctor_archive(archive_root: Path, *, artifact_roots: ArtifactRoots | None = None,
                   probe: RuntimeProbe | None = None, runtime_bindings: Path | None = None) -> dict:
    roots = artifact_roots or ArtifactRoots.of(archive_root)
    bindings = load_runtime_bindings(runtime_bindings)
    failures = []
    with ArchiveSession(archive_root, mode=ArchiveAccessMode.READ, artifact_roots=roots) as session:
        validate_snapshot_database(session.database_path)
        references = required_artifacts(session.database_path)
        for relative, expected in references.items():
            parts = portable_artifact_parts(relative)
            chosen = None
            for base in roots.read_bases():
                candidate = base.joinpath(*parts)
                if candidate.exists() or candidate.is_symlink():
                    chosen = candidate
                    break
            try:
                if chosen is None:
                    failures.append({"artifact": relative, "code": "missing_artifact"})
                    continue
                require_regular_file(chosen)
                if expected is not None:
                    with chosen.open("rb") as stream:
                        _size, digest = stream_hash(stream)
                    if digest != expected:
                        failures.append({"artifact": relative, "code": "artifact_hash_mismatch"})
            except (ValueError, OSError):
                failures.append({"artifact": relative, "code": "unsafe_artifact"})
        from bili_asr.archive import archive_bundle_complete
        for (raw,) in session.connection.execute("SELECT artifact_json FROM workflow_publications"):
            paths = json.loads(raw)
            if not any(archive_bundle_complete(base, paths) for base in roots.read_bases()):
                failures.append({"artifact": paths["srt_path"], "code": "bundle_integrity_failed"})
        report = _job_report(session.connection, restored=False,
                             probe=probe or RuntimeProbe.local(check_gpu=_needs_gpu(session.connection)), bindings=bindings)
    return {"operation": "doctor", "data_complete": not failures,
            "checked_artifacts": len(references), "artifact_failures": failures,
            "consumer_version": __version__, "model_load_tested": False, **report}


__all__ = ["RuntimeProbe", "inspect_snapshot", "plan_restore", "doctor_archive"]
