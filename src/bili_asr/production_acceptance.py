"""Versioned, operator-attested production acceptance records.

Records are separate, write-once files. This module records evidence pointers,
never evidence content, and does not claim that structural verification proves
that an operator's production observations are true.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import zipfile

from bili_asr.contracts.json_schema import validate_json
from bili_asr.file_ops import checked_path, fsync_directory, json_bytes

FORMAT = "bili-asr-production-acceptance"
INPUT_FORMAT = "bili-asr-production-acceptance-input"
SCHEMA_ID = "production-acceptance/v1"
INPUT_SCHEMA_ID = "production-acceptance-input/v1"
CHECK_IDS = (
    "source-delivered",
    "wheel-installed",
    "worker-loaded",
    "database-contract",
    "external-actor-scope",
    "offline-imports",
    "model-class-import",
    "gpu-short-pipeline",
    "gpu-long-sample",
    "drain-and-rollback",
)
WORKER_ROLES = {"asr", "acquisition", "cpu", "editorial", "mixed"}
WORKER_PHASES = {"ready", "running", "draining", "stopped"}
PLATFORMS = {"linux-x86_64", "linux-aarch64", "windows-amd64", "macos-arm64", "other"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_RECORD = 16 * 1024 * 1024
_MAX_WORKER_AGE = 30
_FUTURE_TOLERANCE = 5


class ProductionAcceptanceError(ValueError):
    """Invalid production acceptance input or record."""


def _safe_checked_path(path: Path, message: str) -> Path:
    try:
        return checked_path(path)
    except Exception as exc:
        raise ProductionAcceptanceError(message) from exc


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProductionAcceptanceError("JSON contains a duplicate field")
        result[key] = value
    return result


def _read_json(path: Path, *, limit: int) -> dict:
    try:
        path = checked_path(path)
        if not path.is_file() or path.stat().st_size > limit:
            raise ProductionAcceptanceError("input file is missing or too large")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicates)
    except ProductionAcceptanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProductionAcceptanceError("cannot read JSON input") from exc
    if not isinstance(value, dict):
        raise ProductionAcceptanceError("JSON root must be an object")
    return value


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProductionAcceptanceError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionAcceptanceError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ProductionAcceptanceError(f"{label} must be UTC")
    return parsed.astimezone(timezone.utc)


def _digest_file(path: Path) -> str:
    try:
        path = checked_path(path)
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except Exception as exc:
        raise ProductionAcceptanceError("release artifact is unreadable") from exc


def _release_identity(manifest_path: Path) -> dict:
    manifest_path = _safe_checked_path(manifest_path, "release manifest path cannot be checked safely")
    manifest = _read_json(manifest_path, limit=64 * 1024)
    required = {"schema_version", "build_id", "source_commit", "lock_sha256", "wheel", "wheel_sha256", "production_acceptance"}
    if (set(manifest) != required or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest["source_commit"], str) or not _COMMIT.fullmatch(manifest["source_commit"])
            or not isinstance(manifest["build_id"], str) or not _HEX64.fullmatch(manifest["build_id"])
            or not isinstance(manifest["lock_sha256"], str) or not _HEX64.fullmatch(manifest["lock_sha256"])
            or not isinstance(manifest["wheel_sha256"], str) or not _HEX64.fullmatch(manifest["wheel_sha256"])
            or manifest["production_acceptance"] != "pending"
            or not isinstance(manifest["wheel"], str) or Path(manifest["wheel"]).name != manifest["wheel"]
            or not manifest["wheel"].endswith(".whl")):
        raise ProductionAcceptanceError("release manifest identity is invalid")
    wheel_path = _safe_checked_path(manifest_path.parent / manifest["wheel"],
                                    "release wheel path cannot be checked safely")
    wheel_digest = _digest_file(wheel_path)
    if wheel_digest != manifest["wheel_sha256"]:
        raise ProductionAcceptanceError("release wheel checksum mismatch")
    try:
        from bili_asr.release_identity import MANIFEST_NAME, validate_manifest, wheel_package_files
        with zipfile.ZipFile(wheel_path) as archive:
            embedded = validate_manifest(json.loads(archive.read(f"bili_asr/{MANIFEST_NAME}")))
            members = wheel_package_files(archive)
        if (embedded["build_id"] != manifest["build_id"]
                or embedded["source_commit"] != manifest["source_commit"]
                or embedded["lock_sha256"] != manifest["lock_sha256"]
                or members != embedded["files"]):
            raise ProductionAcceptanceError("release wheel build identity mismatch")
    except ProductionAcceptanceError:
        raise
    except (OSError, KeyError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProductionAcceptanceError("release wheel contents are invalid") from exc
    return {"source_commit": manifest["source_commit"], "build_id": manifest["build_id"],
            "wheel_name": manifest["wheel"], "wheel_sha256": wheel_digest,
            "lock_sha256": manifest["lock_sha256"]}


def _database_contract(archive_root: Path) -> dict:
    """Read the registered archive/extension identity without opening a writer."""
    try:
        from bili_asr.archive_modes import ArchiveAccessMode
        from bili_asr.archive_session import ArchiveSession
        from bili_asr.contracts.fingerprints import database_fingerprint
        from bili_asr.storage.archive_contracts import runtime_contract
        from bili_asr.storage.extension_contracts import (
            require_artifact_catalog,
            require_artifact_online,
            require_import_extension,
            require_supplement_extension,
        )
        from bili_asr.storage.observation_rows import require_observations

        with ArchiveSession(archive_root, mode=ArchiveAccessMode.READ) as session:
            connection = session.connection
            runtime = runtime_contract(connection)
            imports = require_import_extension(connection)
            supplements = require_supplement_extension(connection)
            artifacts = require_artifact_catalog(connection)
            online = require_artifact_online(connection)
            observations = require_observations(connection)
            rights_objects = {
                "content_rights_events", "idx_content_rights_edition", "idx_content_rights_part",
                "immutable_content_rights_update", "immutable_content_rights_delete",
            }
            present_rights = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE sql IS NOT NULL") if row[0] in rights_objects}
            if present_rights and present_rights != rights_objects:
                raise ProductionAcceptanceError("database rights extension is incomplete")
            rights = bool(present_rights)
            fingerprint = database_fingerprint(runtime, imports, supplements, artifacts, online, rights, observations)
        if runtime not in {"bilibili-v1", "universal-v2"} or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise ProductionAcceptanceError("database contract identity is unsupported")
        return {"runtime_contract": runtime, "fingerprint": fingerprint}
    except ProductionAcceptanceError:
        raise
    except Exception as exc:
        # Database/SQLite exceptions can contain local paths. Keep them out of
        # this record command's user-facing diagnostic and all saved evidence.
        raise ProductionAcceptanceError("database contract could not be read and validated") from exc


def _worker_snapshot(directory: Path | None, *, build_id: str, now: datetime) -> list[dict]:
    if directory is None:
        return []
    try:
        directory = checked_path(directory)
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise ProductionAcceptanceError("worker health location is not a directory")
        paths = sorted(directory.glob("*.json"))
        if len(paths) > 1000:
            raise ProductionAcceptanceError("worker health inventory is too large")
        result = []
        for path in paths:
            value = _read_json(path, limit=64 * 1024)
            if value.get("phase") == "stopped":
                continue
            build = value.get("build")
            heartbeat = value.get("heartbeat_at")
            profile_digests = value.get("profile_digests", [])
            if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
                    or type(value.get("pid")) is not int or value["pid"] <= 0
                    or not isinstance(value.get("instance_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", value["instance_id"])
                    or value.get("role") not in WORKER_ROLES or value.get("phase") not in WORKER_PHASES
                    or not isinstance(build, dict) or not isinstance(profile_digests, list)
                    or len(profile_digests) > 64
                    or any(not isinstance(item, str) or not _HEX64.fullmatch(item) for item in profile_digests)):
                raise ProductionAcceptanceError("worker health record is invalid")
            heartbeat_time = _utc(heartbeat, "worker heartbeat")
            age = (now - heartbeat_time).total_seconds()
            fresh = -_FUTURE_TOLERANCE <= age <= _MAX_WORKER_AGE
            worker_build = build.get("build_id") if build.get("state") == "verified" else None
            if worker_build is not None and (not isinstance(worker_build, str) or not _HEX64.fullmatch(worker_build)):
                raise ProductionAcceptanceError("worker build identity is invalid")
            result.append({"pid": value["pid"], "instance_id": value["instance_id"], "role": value["role"],
                           "phase": value["phase"], "heartbeat_at": heartbeat_time.isoformat(timespec="seconds"),
                           "heartbeat_fresh": fresh, "build_id": worker_build,
                           "build_matches_release": worker_build == build_id,
                           "profile_digests": sorted(set(profile_digests))})
        return sorted(result, key=lambda item: (item["role"], item["pid"], item["instance_id"]))
    except ProductionAcceptanceError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ProductionAcceptanceError("worker health inventory cannot be read safely") from exc


def _worker_set_matches(workers: list[dict], build_id: str) -> bool:
    return bool(workers) and all(
        item["heartbeat_fresh"] and item["build_matches_release"] and item["phase"] in {"ready", "running"}
        and item["build_id"] == build_id for item in workers)


def _worker_inventory_matches(scope: dict, workers: list[dict], build_id: str) -> bool:
    expected_roles = set(scope["expected_worker_roles"])
    observed_roles = {worker["role"] for worker in workers}
    return bool(expected_roles) and expected_roles == observed_roles and _worker_set_matches(workers, build_id)


def _worker_scope_complete(scope: dict) -> bool:
    return scope["worker_inventory_complete"] is True and bool(scope["expected_worker_roles"])


def _scope_complete(scope: dict) -> bool:
    external_statuses = {entry["status"] for entry in scope["external_actor_inventory"]}
    return _worker_scope_complete(scope) and "not_run" not in external_statuses


def _external_actor_scope_matches(scope: dict, workers: list[dict], build_id: str) -> bool:
    return (_scope_complete(scope)
            and _worker_inventory_matches(scope, workers, build_id))


def _stage_state(status: str) -> str:
    return {"pass": "verified", "fail": "failed", "not_run": "pending"}[status]


def _derive_status(checks: list[dict], workers: list[dict], build_id: str,
                   operator_confirmed: bool, scope: dict) -> dict:
    by_id = {check["id"]: check["status"] for check in checks}
    if set(by_id) != set(CHECK_IDS) or len(checks) != len(CHECK_IDS):
        raise ProductionAcceptanceError("acceptance checks must contain each registered check exactly once")
    if by_id["worker-loaded"] == "pass" and not (
            _worker_scope_complete(scope) and _worker_inventory_matches(scope, workers, build_id)):
        raise ProductionAcceptanceError("worker-loaded cannot pass without complete fresh role coverage")
    if by_id["external-actor-scope"] == "pass" and not _external_actor_scope_matches(scope, workers, build_id):
        raise ProductionAcceptanceError("external-actor-scope cannot pass without complete worker and actor coverage")
    has_included_actor = any(entry["status"] == "included" for entry in scope["external_actor_inventory"])
    production = ("failed" if "fail" in by_id.values() else
                  "production_verified" if all(value == "pass" for value in by_id.values())
                  and operator_confirmed and _scope_complete(scope)
                  and _worker_inventory_matches(scope, workers, build_id)
                  and (not has_included_actor or by_id["external-actor-scope"] == "pass") else "pending")
    return {"source_delivered": _stage_state(by_id["source-delivered"]),
            "installed": _stage_state(by_id["wheel-installed"]),
            "loaded": _stage_state(by_id["worker-loaded"]), "production": production}


def _profile_digests(workers: list[dict]) -> list[str]:
    return sorted({digest for worker in workers for digest in worker["profile_digests"]})


def _validate_record(record: dict) -> None:
    try:
        validate_json(SCHEMA_ID, record)
        recorded_at = _utc(record["recorded_at"], "record timestamp")
        for check in record["checks"]:
            if check["id"] not in CHECK_IDS:
                raise ProductionAcceptanceError("acceptance check ID is not registered")
        if {item["id"] for item in record["checks"]} != set(CHECK_IDS):
            raise ProductionAcceptanceError("acceptance check set is incomplete or duplicated")
        workers = record["workers"]
        build_id = record["release"]["build_id"]
        for worker in workers:
            beat = _utc(worker["heartbeat_at"], "worker heartbeat")
            age = (recorded_at - beat).total_seconds()
            expected_fresh = -_FUTURE_TOLERANCE <= age <= _MAX_WORKER_AGE
            if worker["heartbeat_fresh"] is not expected_fresh:
                raise ProductionAcceptanceError("worker freshness is inconsistent with the recorded time")
            expected_match = worker["build_id"] == build_id
            if worker["build_matches_release"] is not expected_match:
                raise ProductionAcceptanceError("worker build comparison is inconsistent")
        if record["profile_digests"] != _profile_digests(workers):
            raise ProductionAcceptanceError("profile digest set is inconsistent with worker evidence")
        expected_status = _derive_status(record["checks"], workers, build_id,
                                         record["operator_confirmed_production"], record["scope"])
        if record["status"] != expected_status:
            raise ProductionAcceptanceError("acceptance status is inconsistent with its checks")
    except ProductionAcceptanceError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionAcceptanceError("acceptance record is invalid") from exc


def verify_record_file(path: Path) -> dict:
    """Read-only structural and internal-consistency check; not a host attestation."""
    record = _read_json(path, limit=_MAX_RECORD)
    _validate_record(record)
    return {"record_id": record["record_id"], "status": record["status"],
            "checks": len(record["checks"]), "worker_instances": len(record["workers"]),
            "verification": "self_consistent_only"}


def show_record_file(path: Path) -> dict:
    record = _read_json(path, limit=_MAX_RECORD)
    _validate_record(record)
    return record


def _read_input(path: Path) -> dict:
    value = _read_json(path, limit=64 * 1024)
    if set(value) != {"format", "schema_version", "environment", "operator_id", "scope", "checks"}:
        raise ProductionAcceptanceError("operator input contains unsupported fields")
    try:
        validate_json(INPUT_SCHEMA_ID, value)
    except ValueError as exc:
        raise ProductionAcceptanceError("operator input does not match the registered version-1 input schema") from exc
    if value["format"] != INPUT_FORMAT or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ProductionAcceptanceError("unsupported operator input format")
    environment = value["environment"]
    if (not isinstance(environment, dict) or set(environment) != {"id", "platform"}
            or not isinstance(environment["id"], str) or not _ALIAS.fullmatch(environment["id"])
            or environment["platform"] not in PLATFORMS):
        raise ProductionAcceptanceError("environment must use a non-sensitive ID and registered platform")
    if not isinstance(value["operator_id"], str) or not _ALIAS.fullmatch(value["operator_id"]):
        raise ProductionAcceptanceError("operator ID must be a non-sensitive alias")
    _validate_scope(value["scope"])
    if not isinstance(value["checks"], list):
        raise ProductionAcceptanceError("operator checks must be a list")
    checks = value["checks"]
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "status", "evidence"}:
            raise ProductionAcceptanceError("operator check contains unsupported fields")
    if (len(checks) != len(CHECK_IDS)
            or {check["id"] for check in checks} != set(CHECK_IDS)):
        raise ProductionAcceptanceError("operator checks must contain every registered check exactly once")
    return value


def _validate_scope(scope: object) -> None:
    required = {"expected_worker_roles", "worker_inventory_complete", "worker_inventory_evidence",
                "external_actor_inventory"}
    if not isinstance(scope, dict) or set(scope) != required:
        raise ProductionAcceptanceError("operator scope contains unsupported fields")
    roles = scope["expected_worker_roles"]
    if (not isinstance(roles, list) or not roles or len(roles) > len(WORKER_ROLES)
            or any(not isinstance(role, str) for role in roles)
            or len(set(roles)) != len(roles) or any(role not in WORKER_ROLES for role in roles)):
        raise ProductionAcceptanceError("operator scope has invalid worker roles")
    if type(scope["worker_inventory_complete"]) is not bool:
        raise ProductionAcceptanceError("operator scope completeness must be explicit")
    _validate_evidence(scope["worker_inventory_evidence"])
    actors = scope["external_actor_inventory"]
    actor_kinds = {"scheduler", "backup_helper", "worker_supervisor", "gpu_runner", "other"}
    if not isinstance(actors, list) or len(actors) != len(actor_kinds):
        raise ProductionAcceptanceError("operator scope must enumerate every external actor kind")
    seen = set()
    for actor in actors:
        if not isinstance(actor, dict) or set(actor) != {"kind", "status", "evidence"}:
            raise ProductionAcceptanceError("operator external actor entry is invalid")
        if (not isinstance(actor["kind"], str) or actor["kind"] not in actor_kinds
                or actor["kind"] in seen):
            raise ProductionAcceptanceError("operator external actor inventory is incomplete")
        seen.add(actor["kind"])
        if actor["status"] not in {"none", "included", "not_run"}:
            raise ProductionAcceptanceError("operator external actor status is invalid")
        _validate_evidence(actor["evidence"])
    if seen != actor_kinds:
        raise ProductionAcceptanceError("operator external actor inventory is incomplete")


def _validate_evidence(value: object) -> None:
    if (not isinstance(value, dict) or set(value) != {"id", "sha256"}
            or not isinstance(value["id"], str) or not _EVIDENCE_ID.fullmatch(value["id"])
            or not isinstance(value["sha256"], str) or not _HEX64.fullmatch(value["sha256"])):
        raise ProductionAcceptanceError("evidence must contain only a bounded ID and SHA256")


def _write_once_atomic(path: Path, payload: bytes) -> None:
    """Publish complete bytes atomically without replacing an existing path."""
    try:
        path = checked_path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = checked_path(path)
        if path.exists():
            raise ProductionAcceptanceError("acceptance output already exists")
    except ProductionAcceptanceError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProductionAcceptanceError("cannot safely prepare acceptance output") from exc
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".acceptance-", dir=path.parent)
    except OSError as exc:
        raise ProductionAcceptanceError("cannot safely prepare acceptance output") from exc
    temporary = Path(temporary_name)
    output_published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            view = memoryview(payload)
            while view:
                count = stream.write(view)
                if not count:
                    raise OSError("short write")
                view = view[count:]
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(temporary)
        checked_path(path)
        # Hard-link publication is atomic and fails if the destination exists.
        # On filesystems without hard-link support we fail closed; never fall
        # back to a visible partial file or an overwriting replace.
        os.link(temporary, path, follow_symlinks=False)
        output_published = True
        fsync_directory(path.parent)
    except FileExistsError as exc:
        raise ProductionAcceptanceError("acceptance output already exists") from exc
    except ProductionAcceptanceError:
        raise
    except Exception as exc:
        if output_published:
            raise ProductionAcceptanceError(
                "acceptance record may already have been published; run production-acceptance verify before retrying"
            ) from exc
        raise ProductionAcceptanceError("cannot safely publish acceptance record") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception as exc:
            if output_published:
                raise ProductionAcceptanceError(
                    "acceptance record may already have been published; run production-acceptance verify before retrying"
                ) from exc
            raise ProductionAcceptanceError(
                "cannot safely clean up temporary acceptance record"
            ) from exc
    try:
        fsync_directory(path.parent)
    except Exception as exc:
        raise ProductionAcceptanceError(
            "acceptance record may already have been published; run production-acceptance verify before retrying"
        ) from exc


def record_acceptance(*, input_path: Path, release_manifest: Path, archive_root: Path,
                      worker_health_dir: Path | None, output_path: Path,
                      confirm_production: bool = False) -> dict:
    """Build and atomically create one immutable record from bounded evidence."""
    if type(confirm_production) is not bool:
        raise ProductionAcceptanceError("operator confirmation must be boolean")
    operator_input = _read_input(input_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    release = _release_identity(release_manifest)
    try:
        from bili_asr.release_identity import build_status
        running_build = build_status(expected_build_id=release["build_id"])
    except Exception as exc:
        raise ProductionAcceptanceError("running package build identity could not be verified") from exc
    if (not isinstance(running_build, dict) or running_build.get("state") != "verified"
            or running_build.get("build_id") != release["build_id"]):
        raise ProductionAcceptanceError("running package build identity does not match the release wheel")
    database = _database_contract(archive_root)
    workers = _worker_snapshot(worker_health_dir, build_id=release["build_id"], now=now)
    checks = sorted(operator_input["checks"], key=lambda item: str(item.get("id", "")))
    record = {"format": FORMAT, "schema_version": 1, "record_id": uuid.uuid4().hex,
              "recorded_at": now.isoformat(), "environment": operator_input["environment"],
              "operator_id": operator_input["operator_id"], "release": release,
              "database_contract": database, "scope": operator_input["scope"], "workers": workers,
              "profile_digests": _profile_digests(workers), "checks": checks,
              "operator_confirmed_production": confirm_production}
    record["status"] = _derive_status(checks, workers, release["build_id"], confirm_production,
                                      operator_input["scope"])
    _validate_record(record)
    payload = json_bytes(record)
    if len(payload) > _MAX_RECORD:
        raise ProductionAcceptanceError("acceptance record exceeds the registered size limit")
    _write_once_atomic(output_path, payload)
    return {"record_id": record["record_id"], "status": record["status"]}
