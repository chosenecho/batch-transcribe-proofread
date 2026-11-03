"""Public maintenance API with versioned import orchestration."""
from __future__ import annotations

from bili_asr.services.maintenance_package_core import (
    _CONTRACT,
    _FORMAT,
    _HASH,
    _LIMIT,
    _NAME,
    _PLAN,
    _PROFILE,
    _RECEIPT,
    UTC,
    ArchiveAccessMode,
    ArchiveSession,
    Path,
    __version__,
    _attempt_artifacts,
    _check_attempt_artifacts,
    _check_bundle_markers,
    _check_references,
    _database_state,
    _extract,
    _free_space,
    _hash_file,
    _identity,
    _inventory,
    _json,
    _manifest_object,
    _read_manifest,
    _receipt_path,
    _record,
    _root_entries,
    _selected_artifacts,
    _sync_directory,
    _sync_file,
    _validate_manifest,
    absolute,
    canonical,
    check_artifact_collisions,
    closing,
    collect_artifacts,
    datetime,
    digest,
    export_package,
    hashlib,
    inspect_package,
    json,
    os,
    portable_artifact_parts,
    re,
    readonly,
    refuse_unfinished,
    require_no_links,
    require_regular_file,
    required_artifacts,
    runtime_contract,
    select_database,
    shutil,
    stat,
    stream_hash,
    table_ledger,
    tempfile,
    validate_snapshot_database,
    verify_package,
    zipfile,
)
from bili_asr.services.snapshot_protocol import _empty_destination


def plan_import(package: Path, target_root: Path, *, strategy: str = "new", resumable: bool = False, scope: dict | None = None) -> dict:
    if resumable or strategy != "new" or scope is not None:
        from bili_asr.services.maintenance_import import plan_import as planned
        return planned(package, target_root, strategy=strategy, scope=scope)
    path, target = absolute(package), absolute(target_root)
    if path.is_relative_to(target):
        raise ValueError("maintenance target cannot contain its package")
    _empty_destination(target)
    before = _record("package", path)
    verified = verify_package(path)
    if _record("package", path) != before:
        raise ValueError("maintenance package changed during preflight")
    needed = verified["total_bytes"] + 32 * 1024**2
    _free_space(target, needed)
    plan = {"format": _PLAN, "policy": "new-target-only", "package": str(path),
            "package_file": before, "content_id": verified["content_id"],
            "target_root": str(target), "required_free_bytes": needed}
    return {**plan, "plan_id": digest(plan)}


def _validate_plan(plan: dict) -> None:
    if (set(plan) != {"format", "policy", "package", "package_file", "content_id", "target_root", "required_free_bytes", "plan_id"}
            or plan["format"] != _PLAN or plan["policy"] != "new-target-only"
            or plan["plan_id"] != digest({key: value for key, value in plan.items() if key != "plan_id"})):
        raise ValueError("invalid maintenance import plan")
    if type(plan["required_free_bytes"]) is not int or plan["required_free_bytes"] < 32 * 1024**2:
        raise ValueError("invalid maintenance staging estimate")
    if str(absolute(Path(plan["target_root"]))) != plan["target_root"] or str(absolute(Path(plan["package"]))) != plan["package"]:
        raise ValueError("maintenance plan paths must be absolute")




def _check_installation(root: Path, plan_id: str, *, staged: bool = False) -> dict:
    path = _receipt_path(root, plan_id)
    receipt = _json(path)
    if receipt.get("format") == "archive-maintenance-import-receipt/v2":
        from bili_asr.services.maintenance_import import check_import as checked
        return checked(root, plan_id, staged=staged)
    if (set(receipt) != {"format", "plan", "manifest", "receipt_id"} or receipt["format"] != _RECEIPT
            or receipt["receipt_id"] != digest({key: value for key, value in receipt.items() if key != "receipt_id"})):
        raise ValueError("invalid maintenance receipt")
    _validate_plan(receipt["plan"])
    if receipt["plan"]["plan_id"] != plan_id:
        raise ValueError("maintenance receipt belongs to another plan")
    if not staged and receipt["plan"]["target_root"] != str(root):
        raise ValueError("maintenance receipt target path does not match this installation")
    manifest = receipt["manifest"]
    _validate_manifest(manifest)
    if manifest["content_id"] != receipt["plan"]["content_id"]:
        raise ValueError("maintenance receipt content mismatch")
    before = _database_state(root / "archive.db")
    files = {"archive.db": root / "archive.db", **collect_artifacts((root,))}
    baseline = {entry["path"]: entry for entry in manifest["files"]}
    receipt_key = path.relative_to(root).as_posix()
    if set(files) != set(baseline) | {receipt_key}:
        raise ValueError("maintenance installation file set changed")
    for key, expected in baseline.items():
        if _record(key, files[key]) != expected:
            raise ValueError("maintenance installation baseline changed: " + key)
    validate_snapshot_database(root / "archive.db", expected_contract=manifest["database_contract"])
    if table_ledger(root / "archive.db") != manifest["tables"]:
        raise ValueError("maintenance installation typed rows changed")
    _check_references(root / "archive.db", baseline)
    _check_attempt_artifacts(root / "archive.db", baseline)
    if _database_state(root / "archive.db") != before:
        raise ValueError("maintenance installation changed during verification")
    return {"operation": "maintenance-import-check", "valid": True, "plan_id": plan_id,
            "receipt_id": receipt["receipt_id"], "installation_baseline_matches": True}


def check_import(target_root: Path, *, plan_id: str) -> dict:
    root = absolute(target_root)
    with ArchiveSession(root, mode=ArchiveAccessMode.MAINTENANCE).access():
        return _check_installation(root, plan_id)


def apply_import(plan_path: Path) -> dict:
    plan = _json(absolute(plan_path))
    if plan.get("format") == "archive-maintenance-import-plan/v2":
        from bili_asr.services.maintenance_import import apply_import as applied
        return applied(plan_path)
    _validate_plan(plan)
    package, target = absolute(Path(plan["package"])), absolute(Path(plan["target_root"]))
    if _record("package", package) != plan["package_file"]:
        raise ValueError("maintenance package changed since import planning")
    target.parent.mkdir(parents=True, exist_ok=True)
    with ArchiveSession(target, mode=ArchiveAccessMode.MAINTENANCE).access(allow_missing=True):
        if _receipt_path(target, plan["plan_id"]).exists():
            return {**_check_installation(target, plan["plan_id"]), "reused": True}
        current = plan_import(package, target)
        if current != plan:
            raise ValueError("maintenance preflight no longer matches its plan")
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.maintenance-", dir=target.parent) as temporary:
            stage = Path(temporary) / "archive"
            stage.mkdir()
            manifest = _extract(package, stage)
            if _record("package", package) != plan["package_file"]:
                raise ValueError("maintenance package changed during import")
            path = _receipt_path(stage, plan["plan_id"])
            if path.exists():
                raise ValueError("package collides with its new import receipt")
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {"format": _RECEIPT, "plan": plan, "manifest": manifest}
            receipt = {**value, "receipt_id": digest(value)}
            with path.open("xb") as stream:
                stream.write(canonical(receipt) + b"\n")
                _sync_file(stream)
            _check_installation(stage, plan["plan_id"], staged=True)
            for directory, _, _ in os.walk(stage, topdown=False):
                _sync_directory(Path(directory))
            if _empty_destination(target):
                target.rmdir()
            stage.rename(target)
            _sync_directory(target.parent)
    return {"operation": "maintenance-import-apply", "valid": True, "plan_id": plan["plan_id"],
            "receipt_id": receipt["receipt_id"], "reused": False, "target_root": str(target)}


def write_plan(plan: dict, output: Path) -> None:
    """Save a plan exclusively; a failed write never leaves a partial document."""
    out = absolute(output)
    if out.exists() or not out.parent.is_dir() or out.is_relative_to(Path(plan["target_root"])):
        raise ValueError("plan output must be a new file outside the target with an existing parent")
    with tempfile.TemporaryDirectory(prefix=".maintenance-plan-", dir=out.parent) as temporary:
        staged = Path(temporary) / "plan.json"
        with staged.open("xb") as stream:
            stream.write(canonical(plan) + b"\n")
            _sync_file(stream)
        if os.name == "nt":
            staged.rename(out)
        else:
            os.link(staged, out)
            staged.unlink()
        _sync_directory(out.parent)


__all__ = [
    'UTC',
    '_CONTRACT',
    '_FORMAT',
    '_HASH',
    '_LIMIT',
    '_NAME',
    '_PLAN',
    '_PROFILE',
    '_RECEIPT',
    'ArchiveAccessMode',
    'ArchiveSession',
    'Path',
    '__version__',
    '_attempt_artifacts',
    '_check_attempt_artifacts',
    '_check_bundle_markers',
    '_check_references',
    '_database_state',
    '_empty_destination',
    '_extract',
    '_free_space',
    '_hash_file',
    '_identity',
    '_inventory',
    '_json',
    '_manifest_object',
    '_read_manifest',
    '_receipt_path',
    '_record',
    '_root_entries',
    '_selected_artifacts',
    '_sync_directory',
    '_sync_file',
    '_validate_manifest',
    'absolute',
    'apply_import',
    'canonical',
    'check_artifact_collisions',
    'check_import',
    'closing',
    'collect_artifacts',
    'datetime',
    'digest',
    'export_package',
    'hashlib',
    'inspect_package',
    'json',
    'os',
    'plan_import',
    'portable_artifact_parts',
    're',
    'readonly',
    'refuse_unfinished',
    'require_no_links',
    'require_regular_file',
    'required_artifacts',
    'runtime_contract',
    'select_database',
    'shutil',
    'stat',
    'stream_hash',
    'table_ledger',
    'tempfile',
    'validate_snapshot_database',
    'verify_package',
    'write_plan',
    'zipfile',
]
