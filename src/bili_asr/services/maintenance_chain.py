"""Offline file-level differential chains and dependency-safe retention.

Every restore point names its complete logical inventory. Container members
carry only changed bytes; database changes are whole SQLite files, preserving
original historical values rather than inventing SQL patches.
"""
from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    check_artifact_collisions,
    portable_artifact_parts,
    stream_hash,
)
from bili_asr.services import maintenance_package_core as package
from bili_asr.storage.maintenance_selection import table_ledger

NAME = "differential.json"
FORMAT = "archive-maintenance-differential/v1"
MAX_CHAIN_POINTS = 64


def _repository(argument):
    """All modifying chain operations share one stable repository lock."""
    def decorate(function):
        @wraps(function)
        def coordinated(*args, **kwargs):
            import inspect
            values = inspect.signature(function).bind(*args, **kwargs).arguments
            root = argument(values)
            with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
                return function(*args, **kwargs)
        return coordinated
    return decorate


def _name(value: str) -> None:
    if (not isinstance(value, str) or not value.endswith(".zip") or value != Path(value).name
            or "/" in value or "\\" in value or value.startswith(".")):
        raise ValueError("snapshot dependency must be a plain ZIP filename")


def _manifest(path: Path) -> dict | None:
    package.absolute(path)
    with zipfile.ZipFile(path) as bundle:
        if NAME not in bundle.namelist():
            return None
        entry = bundle.getinfo(NAME)
        if entry.file_size > package._LIMIT:
            raise ValueError("oversized differential manifest")
        import json
        document = json.loads(bundle.read(NAME), object_pairs_hook=package._manifest_object)
        if (not isinstance(document, dict) or set(document) != {"format", "base", "target", "changed", "deleted", "chain_id", "created_at", "container"}
                or document["format"] != FORMAT or document["chain_id"] != package.digest(
                    {key: value for key, value in document.items() if key not in {"chain_id", "created_at"}})):
            raise ValueError("invalid differential chain identity")
        base = document["base"]
        if not isinstance(base, dict) or set(base) != {"name", "sha256", "content_id"}:
            raise ValueError("invalid differential baseline")
        _name(base["name"])
        if not package._HASH.fullmatch(base["sha256"]) or not package._HASH.fullmatch(base["content_id"]):
            raise ValueError("invalid differential baseline identity")
        package._validate_manifest(document["target"])
        target = {item["path"]: item for item in document["target"]["files"]}
        changed = document["changed"]
        if not isinstance(changed, list) or len(changed) != len(set(changed)) or any(key not in target for key in changed):
            raise ValueError("invalid differential changed inventory")
        deleted = document["deleted"]
        if not isinstance(deleted, list) or len(deleted) != len(set(deleted)):
            raise ValueError("invalid differential deletion inventory")
        for key in deleted:
            portable_artifact_parts(key)
            if key in target:
                raise ValueError("differential deletes a retained object")
        names = []
        for member in bundle.infolist():
            mode = member.external_attr >> 16
            if (member.is_dir() or member.orig_filename != member.filename or member.flag_bits & 1
                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise ValueError("unsafe differential member")
            names.append(member.filename)
        if len(names) != len(set(names)) or set(names) != {*changed, NAME}:
            raise ValueError("differential member inventory mismatch")
        for key in changed:
            if bundle.getinfo(key).file_size != target[key]["size"]:
                raise ValueError("differential declared payload size mismatch")
        check_artifact_collisions(changed)
        if document["container"] not in ({"compression": "stored", "encryption": None}, {"compression": "deflate", "encryption": None}):
            raise ValueError("unsupported differential container encryption or compression")
        return document


def materialize(path: Path, stage: Path, *, seen: tuple[Path, ...] = ()) -> tuple[dict, list[dict]]:
    path = package.absolute(path)
    if path in seen or len(seen) >= MAX_CHAIN_POINTS:
        raise ValueError("cyclic or overlong differential chain")
    before = package._record(path.name, path)
    delta = _manifest(path)
    if delta is None:
        with zipfile.ZipFile(path) as bundle:
            maintenance = package._NAME in bundle.namelist()
        if maintenance:
            manifest = package._extract(path, stage)
        else:
            from bili_asr.services.snapshot_protocol import _validate_into
            _validate_into(path, stage)
            # Preserve original DB and artifact bytes while giving historical
            # full snapshots the same typed logical inventory as a v1 package.
            with tempfile.TemporaryDirectory(prefix="chain-full-contract-") as temporary:
                full = Path(temporary) / "baseline.zip"
                package.export_package(stage, full)
                manifest = package.inspect_package(full)["manifest"]
        chain = [{**before, "content_id": manifest["content_id"], "base": None}]
    else:
        baseline = path.parent / delta["base"]["name"]
        if not baseline.is_file():
            raise FileNotFoundError("missing differential baseline: " + str(baseline))
        if package._record(baseline.name, baseline)["sha256"] != delta["base"]["sha256"]:
            raise ValueError("differential baseline container changed")
        previous, chain = materialize(baseline, stage, seen=(*seen, path))
        if previous["content_id"] != delta["base"]["content_id"]:
            raise ValueError("differential baseline content changed")
        original = {item["path"]: item for item in previous["files"]}
        target = {item["path"]: item for item in delta["target"]["files"]}
        expected_changes = sorted(key for key in target if original.get(key) != target[key])
        if delta["changed"] != expected_changes or delta["deleted"] != sorted(set(original) - set(target)):
            raise ValueError("differential transition does not match declared inventories")
        for key in delta["deleted"]:
            victim = stage.joinpath(*portable_artifact_parts(key))
            if not victim.is_file():
                raise ValueError("differential deletion has no baseline object: " + key)
            victim.unlink()
        with zipfile.ZipFile(path) as bundle:
            for key in delta["changed"]:
                output = stage.joinpath(*portable_artifact_parts(key))
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(key) as reader, output.open("wb") as writer:
                    actual = stream_hash(reader, writer)
                    package._sync_file(writer)
                if actual != (target[key]["size"], target[key]["sha256"]):
                    raise ValueError("differential payload hash mismatch: " + key)
        manifest = delta["target"]
        package.validate_snapshot_database(stage / "archive.db", expected_contract=manifest["database_contract"])
        if table_ledger(stage / "archive.db") != manifest["tables"]:
            raise ValueError("differential table ledger mismatch")
        package._check_references(stage / "archive.db", target)
        package._check_attempt_artifacts(stage / "archive.db", target)
        chain.append({**before, "content_id": manifest["content_id"], "base": baseline.name})
    if package._record(path.name, path) != before:
        raise ValueError("snapshot chain changed while verifying")
    return manifest, chain


@_repository(lambda values: package.absolute(values["path"]).parent)
def verify_chain(path: Path) -> dict:
    with zipfile.ZipFile(path) as bundle:
        public = "public-backup.json" in bundle.namelist()
    if public:
        from bili_asr.services.maintenance_publication import verify_public_backup
        verified = verify_public_backup(path)
        return {"operation": "maintenance-chain-verify", "valid": True, "chain": [{**package._record(Path(path).name, path),
                "content_id": verified["content_id"], "base": None}], "content_id": verified["content_id"],
                "source_fingerprint": verified["manifest"]["source_fingerprint"],
                "file_count": len(verified["manifest"]["files"]), "total_bytes": verified["total_bytes"], "database_recoverable": False}
    with tempfile.TemporaryDirectory(prefix="maintenance-chain-check-") as temporary:
        manifest, chain = materialize(path, Path(temporary))
    return {"operation": "maintenance-chain-verify", "valid": True, "chain": chain,
            "content_id": manifest["content_id"], "file_count": len(manifest["files"]),
            "profile": manifest["profile"], "selection": manifest["selection"],
            "source_fingerprint": manifest["source"]["fingerprint"],
            "total_bytes": sum(entry["size"] for entry in manifest["files"])}


@_repository(lambda values: package.absolute(values["output"]).parent)
def save_delta(source_root: Path, output: Path, baseline: Path, *, artifact_root=None, scope=None,
               compression: str = "stored") -> dict:
    out, base = package.absolute(output), package.absolute(baseline)
    _name(out.name)
    _name(base.name)
    if out.parent != base.parent or out.exists():
        raise ValueError("differential must be new and share its baseline repository")
    if compression not in {"stored", "deflate"}:
        raise ValueError("differential compression must be stored or deflate")
    with tempfile.TemporaryDirectory(prefix=".differential-stage-", dir=out.parent) as temporary:
        work = Path(temporary)
        prior = work / "prior"
        prior.mkdir()
        previous, chain = materialize(base, prior)
        full = work / "full.zip"
        package.export_package(source_root, full, artifact_root=artifact_root, scope=scope)
        current = package.inspect_package(full)["manifest"]
        before = {item["path"]: item for item in previous["files"]}
        after = {item["path"]: item for item in current["files"]}
        changed = sorted(key for key in after if before.get(key) != after[key])
        value = {"format": FORMAT, "base": {"name": base.name, "sha256": chain[-1]["sha256"],
                 "content_id": previous["content_id"]}, "target": current, "changed": changed,
                 "deleted": sorted(set(before) - set(after)),
                 "container": {"compression": compression, "encryption": None}}
        manifest = {**value, "chain_id": package.digest(value), "created_at": datetime.now(UTC).isoformat()}
        staged = work / out.name
        with zipfile.ZipFile(full) as incoming, zipfile.ZipFile(staged, "w", compression=
                zipfile.ZIP_STORED if compression == "stored" else zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            for key in changed:
                with incoming.open(key) as reader, bundle.open(key, "w", force_zip64=True) as writer:
                    stream_hash(reader, writer)
            bundle.writestr(NAME, package.canonical(manifest))
        # Publish only after a complete detached transition check.
        preview = work / "delta-verify"
        preview.mkdir()
        # Relative parent lookup uses repository, so temporarily hardlink the
        # baseline dependency chain into the private validation directory.
        for item in chain:
            os.link(base.parent / item["path"], work / item["path"])
        materialize(staged, preview)
        if verify_chain(base)["chain"] != chain:
            raise ValueError("baseline chain changed before delta publication")
        with staged.open("r+b") as stream:
            package._sync_file(stream)
        if os.name == "nt":
            staged.rename(out)
        else:
            os.link(staged, out)
        package._sync_directory(out.parent)
    return {"operation": "maintenance-delta-save", "valid": True, "snapshot": str(out),
            "chain_id": manifest["chain_id"], "changed_files": len(changed), "deleted_files": len(manifest["deleted"])}


@_repository(lambda values: package.absolute(values["snapshot"]).parent)
def materialized_package(snapshot: Path, output: Path) -> dict:
    out = package.absolute(output)
    if out.exists():
        raise ValueError("materialized package output already exists")
    with tempfile.TemporaryDirectory(prefix=".chain-materialize-", dir=out.parent) as temporary:
        stage = Path(temporary) / "archive"
        stage.mkdir()
        manifest, chain = materialize(snapshot, stage)
        staged = Path(temporary) / "full.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for item in manifest["files"]:
                with stage.joinpath(*portable_artifact_parts(item["path"])).open("rb") as reader, bundle.open(item["path"], "w", force_zip64=True) as writer:
                    stream_hash(reader, writer)
            bundle.writestr(package._NAME, package.canonical(manifest))
        package.verify_package(staged)
        with staged.open("r+b") as stream:
            package._sync_file(stream)
        if os.name == "nt":
            staged.rename(out)
        else:
            os.link(staged, out)
        package._sync_directory(out.parent)
    return {"operation": "maintenance-chain-materialize", "valid": True, "package": str(out), "chain": chain}


@_repository(lambda values: package.absolute(values["repository"]))
def retention_plan(repository: Path, *, keep_count: int = 5, keep_days: int = 30, now: datetime | None = None) -> dict:
    root = package.absolute(repository)
    if type(keep_count) is not int or keep_count < 1 or type(keep_days) is not int or keep_days < 0:
        raise ValueError("retention must keep at least one point and nonnegative days")
    points = []
    for path in sorted(root.glob("*.zip")):
        report = verify_chain(path)
        delta = _manifest(path)
        if delta is None:
            with zipfile.ZipFile(path) as bundle:
                if package._NAME in bundle.namelist():
                    manifest = package._read_manifest(bundle)
                elif "public-backup.json" in bundle.namelist():
                    from bili_asr.services.maintenance_publication import (
                        verify_public_backup,
                    )
                    manifest = verify_public_backup(path)["manifest"]
                else:
                    from bili_asr.services.snapshot_protocol import _read_manifest
                    manifest = _read_manifest(bundle)
        else:
            manifest = delta
        created = datetime.fromisoformat(manifest["created_at"])
        if created.utcoffset() != UTC.utcoffset(created):
            raise ValueError("retention timestamp must be UTC")
        points.append({"path": path.name, "created_at": created.isoformat(), "chain": report["chain"]})
    points.sort(key=lambda item: (item["created_at"], item["path"]), reverse=True)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
    selected = {item["path"] for index, item in enumerate(points) if index < keep_count or datetime.fromisoformat(item["created_at"]) >= cutoff}
    protected = {member["path"] for item in points if item["path"] in selected for member in item["chain"]}
    files = [package._record(path.name, path) for path in sorted(root.glob("*.zip"))]
    value = {"format": "maintenance-retention-plan/v1", "repository": str(root), "files": files,
             "selected_restore_points": sorted(selected), "protected_dependencies": sorted(protected),
             "delete": [item for item in files if item["path"] not in protected],
             "keep_count": keep_count, "keep_days": keep_days}
    return {**value, "plan_id": package.digest(value)}


@_repository(lambda values: package.absolute(Path(values["plan"]["repository"])))
def apply_retention(plan: dict) -> dict:
    if plan.get("plan_id") != package.digest({key: value for key, value in plan.items() if key != "plan_id"}):
        raise ValueError("retention plan identity changed")
    root = package.absolute(Path(plan["repository"]))
    if (plan.get("format") != "maintenance-retention-plan/v1" or type(plan.get("keep_count")) is not int
            or plan["keep_count"] < 1 or type(plan.get("keep_days")) is not int or plan["keep_days"] < 0
            or not isinstance(plan.get("selected_restore_points"), list)
            or not plan["selected_restore_points"] or len(plan["selected_restore_points"]) != len(set(plan["selected_restore_points"]))):
        raise ValueError("retention requires a nonempty protected restore-point set")
    actual = [package._record(path.name, path) for path in sorted(root.glob("*.zip"))]
    if actual != plan["files"]:
        raise ValueError("snapshot repository changed since retention planning")
    # Self hashes are identity, not authorization. Independently derive the
    # retention minimum from current timestamps/count policy, so a recomputed
    # plan cannot remove all restore points or ignore the retention rules.
    minimum = retention_plan(root, keep_count=plan["keep_count"], keep_days=plan["keep_days"])
    if not set(minimum["selected_restore_points"]) <= set(plan["selected_restore_points"]):
        raise ValueError("retention plan discards a point required by its count/day policy")
    if {entry["path"] for entry in plan["delete"]} != {entry["path"] for entry in actual} - set(plan["protected_dependencies"]):
        raise ValueError("retention deletion inventory does not match its dependency proof")
    protected = set()
    for name in plan["selected_restore_points"]:
        _name(name)
        protected.update(item["path"] for item in verify_chain(root / name)["chain"])
    if protected != set(plan["protected_dependencies"]):
        raise ValueError("retention dependency proof changed")
    for item in plan["delete"]:
        _name(item["path"])
        if item["path"] in protected or item not in actual:
            raise ValueError("retention attempts to delete a referenced object")
    for item in plan["delete"]:
        (root / item["path"]).unlink()
    package._sync_directory(root)
    return {"operation": "maintenance-retention-apply", "valid": True, "deleted": [item["path"] for item in plan["delete"]],
            "protected": sorted(protected), "plan_id": plan["plan_id"]}
