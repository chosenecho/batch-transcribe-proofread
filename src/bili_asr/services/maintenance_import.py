"""Planned relational imports with durable staging and atomic directory install.

V1 package bytes remain readable. V2 plans bind both source and existing target,
and make replacement explicit. A retained exchange directory is the rollback
point; it is never automatically removed.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.artifact_inventory import (
    collect_artifacts,
    portable_artifact_parts,
    stream_hash,
)
from bili_asr.services import maintenance_package_core as package
from bili_asr.storage.maintenance_merge import merge_database
from bili_asr.storage.maintenance_selection import table_ledger

PLAN = "archive-maintenance-import-plan/v2"
RECEIPT = "archive-maintenance-import-receipt/v2"


def _owned_file(path: Path) -> None:
    info = package.require_regular_file(path)
    if info.st_nlink != 1:
        raise ValueError("maintenance staging cannot contain hard-linked files")


def _save(path: Path, value: dict) -> None:
    package.absolute(path)
    temporary = path.with_name(path.name + ".pending")
    package.absolute(temporary)
    if temporary.exists():
        _owned_file(temporary)
        temporary.unlink()
    if path.exists():
        _owned_file(path)
    with temporary.open("xb") as stream:
        stream.write(package.canonical(value) + b"\n")
        package._sync_file(stream)
    temporary.replace(path)
    package._sync_directory(path.parent)


def _copy(original: Path, output: Path, expected: dict) -> None:
    package.absolute(output)
    if output.exists():
        _owned_file(output)
        if package._record(expected["path"], output) != expected:
            raise ValueError("completed staging file changed: " + expected["path"])
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".maintenance-partial")
    package.absolute(temporary)
    if temporary.exists():
        _owned_file(temporary)
        temporary.unlink()
    with original.open("rb") as reader, temporary.open("xb") as writer:
        actual = stream_hash(reader, writer)
        package._sync_file(writer)
    if actual != (expected["size"], expected["sha256"]):
        raise ValueError("source changed while copying staged import")
    temporary.replace(output)
    package._sync_directory(output.parent)


def _target_inventory(target: Path) -> tuple[dict | None, dict]:
    if not target.exists() or not any(target.iterdir()):
        return None, {}
    return package._inventory(target, target)


def _build(package_path: Path, target: Path, strategy: str, stage: Path, scope: dict | None = None) -> tuple[dict, dict]:
    incoming = stage.parent / "incoming"
    incoming.mkdir()
    manifest = package._extract(package_path, incoming)
    original_created_at = manifest["created_at"]
    original_producer = manifest["producer_version"]
    if scope is not None:
        selected = stage.parent / "scoped.zip"
        package.export_package(incoming, selected, scope=scope)
        shutil.rmtree(incoming)
        incoming.mkdir()
        manifest = package._extract(selected, incoming)
        manifest["created_at"] = original_created_at
        manifest["producer_version"] = original_producer
    stage.mkdir()
    changes = {"strategy": strategy, "tables": [], "id_mappings": {}, "overwritten": 0}
    target_files = {}
    if strategy in {"replace", "merge"}:
        target_inventory, _ = package._inventory(target, target)
        target_files = {entry["path"]: entry for entry in target_inventory["files"]}
    if strategy == "merge":
        _, physical = package._inventory(target, target)
        for key, source in physical.items():
            _copy(source, stage.joinpath(*portable_artifact_parts(key)), package._record(key, source))
        changes = {"strategy": strategy, **merge_database(incoming / "archive.db", stage / "archive.db")}
    for entry in manifest["files"]:
        if strategy == "merge" and entry["path"] == "archive.db":
            continue
        _copy(incoming.joinpath(*portable_artifact_parts(entry["path"])),
              stage.joinpath(*portable_artifact_parts(entry["path"])), entry)
    files = {"archive.db": stage / "archive.db", **collect_artifacts((stage,))}
    combined = dict(manifest)
    combined["files"] = [package._record(key, value) for key, value in sorted(files.items())]
    combined["tables"] = table_ledger(stage / "archive.db")
    if strategy != "merge":
        previous_tables = {item["name"]: item for item in table_ledger(target / "archive.db")} if strategy == "replace" else {}
        changes["tables"] = {item["name"]: {"added": item["row_count"], "replaced_previous": previous_tables.get(item["name"], {}).get("row_count", 0),
                                            "skipped": 0, "mapped": 0} for item in combined["tables"]}
        changes["overwritten"] = sum(item["row_count"] for item in previous_tables.values())
    changes["files"] = [{**item, "action": "added" if item["path"] not in target_files else
                           "skipped" if item == target_files[item["path"]] else "replaced"}
                          for item in combined["files"]]
    if strategy == "merge":
        combined["selection"] = {"mode": "full", "policy": "all-private-history/v1"}
    combined["content_id"] = package.digest({key: value for key, value in combined.items()
                                             if key not in {"content_id", "created_at", "producer_version"}})
    package._validate_manifest(combined)
    baseline = {entry["path"]: entry for entry in combined["files"]}
    package._check_references(stage / "archive.db", baseline)
    package._check_attempt_artifacts(stage / "archive.db", baseline)
    return combined, changes


def plan_import(package_path: Path, target_root: Path, *, strategy: str = "new", scope: dict | None = None) -> dict:
    if strategy not in {"new", "replace", "merge"}:
        raise ValueError("unknown maintenance import strategy")
    source, target = package.absolute(package_path), package.absolute(target_root)
    if source.is_relative_to(target):
        raise ValueError("import package cannot be inside the target")
    before = package._record("package", source)
    declared = package.inspect_package(source)["manifest"]
    incoming_bytes = sum(item["size"] for item in declared["files"])
    package._free_space(target, incoming_bytes * 3 + 32 * 1024**2)
    package.verify_package(source)
    inventory, _ = _target_inventory(target)
    if strategy == "new" and inventory is not None:
        raise ValueError("new import requires an empty target")
    if strategy in {"replace", "merge"} and inventory is None:
        raise ValueError("replace and merge require an existing archive")
    # Preview performs the same relational union as apply, in a disposable root.
    # It never opens a target write connection or creates target directories.
    with tempfile.TemporaryDirectory(prefix="maintenance-preview-") as temporary:
        resulting, changes = _build(source, target, strategy, Path(temporary) / "archive", scope)
    if package._record("package", source) != before or _target_inventory(target)[0] != inventory:
        raise ValueError("package or target changed during import preflight")
    needed = sum(entry["size"] for entry in resulting["files"]) * 2 + 32 * 1024**2
    package._free_space(target, needed)
    value = {"format": PLAN, "strategy": strategy, "package": str(source), "package_file": before,
             "source_content_id": package.inspect_package(source)["manifest"]["content_id"],
             "content_id": resulting["content_id"], "target_root": str(target),
             "target_fingerprint": None if inventory is None else inventory["fingerprint"],
             "target_inventory": inventory, "result_manifest": resulting, "changes": changes,
             "required_free_bytes": needed, "scope": scope}
    return {**value, "plan_id": package.digest(value)}


def _validate(plan: dict) -> None:
    if (plan.get("format") != PLAN or plan.get("strategy") not in {"new", "replace", "merge"}
            or plan.get("plan_id") != package.digest({k: v for k, v in plan.items() if k != "plan_id"})):
        raise ValueError("invalid resumable import plan")
    for key in ("package", "target_root"):
        if str(package.absolute(Path(plan[key]))) != plan[key]:
            raise ValueError("import plan paths must be absolute")
    package._validate_manifest(plan["result_manifest"])
    if plan["content_id"] != plan["result_manifest"]["content_id"]:
        raise ValueError("import result manifest identity mismatch")


def _exchange(left: Path, right: Path) -> None:
    """Linux exchanges directory entries in one filesystem transaction."""
    if os.name != "posix":
        raise ValueError("atomic replace/merge installation requires Linux renameat2")
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "renameat2", None)
    if function is None:
        raise ValueError("this platform has no atomic directory exchange")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "atomic maintenance directory exchange failed", str(right))


def check_import(target_root: Path, plan_id: str, *, staged: bool = False) -> dict:
    root = package.absolute(target_root)
    receipt_path = package._receipt_path(root, plan_id)
    receipt = package._json(receipt_path)
    if (receipt.get("format") != RECEIPT or receipt.get("receipt_id") != package.digest(
            {key: value for key, value in receipt.items() if key != "receipt_id"})):
        raise ValueError("invalid resumable import receipt")
    plan = receipt["plan"]
    _validate(plan)
    if plan["plan_id"] != plan_id or (not staged and str(root) != plan["target_root"]):
        raise ValueError("import receipt belongs to another installation")
    actual = {"archive.db": root / "archive.db", **collect_artifacts((root,))}
    baseline = {entry["path"]: entry for entry in plan["result_manifest"]["files"]}
    if set(actual) != set(baseline) | {receipt_path.relative_to(root).as_posix()}:
        raise ValueError("import installation file set changed")
    for key, expected in baseline.items():
        if package._record(key, actual[key]) != expected:
            raise ValueError("import installation baseline changed: " + key)
    package.validate_snapshot_database(root / "archive.db", expected_contract=plan["result_manifest"]["database_contract"])
    if table_ledger(root / "archive.db") != plan["result_manifest"]["tables"]:
        raise ValueError("import installation rows changed")
    return {"operation": "maintenance-import-check", "valid": True, "plan_id": plan_id,
            "receipt_id": receipt["receipt_id"], "installation_baseline_matches": True,
            "strategy": plan["strategy"], "rollback_root": receipt["rollback_root"]}


def apply_import(plan_path: Path) -> dict:
    plan = package._json(package.absolute(plan_path))
    _validate(plan)
    source, target = Path(plan["package"]), Path(plan["target_root"])
    if package._record("package", source) != plan["package_file"]:
        raise ValueError("import package changed since planning")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = target.parent / ("." + target.name + ".maintenance-" + plan["plan_id"])
    package.absolute(work)
    with ArchiveSession(target, mode=ArchiveAccessMode.MAINTENANCE).access(allow_missing=True):
        if package._receipt_path(target, plan["plan_id"]).exists():
            return {**check_import(target, plan["plan_id"]), "reused": True}
        if _target_inventory(target)[0] != plan["target_inventory"]:
            raise ValueError("import target changed since planning")
        current = plan_import(source, target, strategy=plan["strategy"], scope=plan["scope"])
        if current != plan:
            raise ValueError("import plan result does not derive from its frozen source, target and selectors")
        package._free_space(target, plan["required_free_bytes"])
        work.mkdir(mode=0o700, exist_ok=True)
        journal = work / "checkpoint.json"
        value = {"format": "maintenance-import-checkpoint/v1", "plan_id": plan["plan_id"], "phase": "copying"}
        if journal.exists():
            _owned_file(journal)
            saved = package._json(journal)
            if saved.get("plan_id") != plan["plan_id"] or saved.get("format") != value["format"]:
                raise ValueError("staging checkpoint belongs to another plan")
        else:
            _save(journal, value)
        stage = work / "archive"
        converted = work / "converted"
        if not converted.exists():
            # A conversion interrupted before its atomic publication is rebuilt
            # alone. Once published, its exact files survive process death.
            with tempfile.TemporaryDirectory(prefix=".conversion-", dir=work) as temporary:
                candidate = Path(temporary) / "archive"
                manifest, changes = _build(source, target, plan["strategy"], candidate, plan["scope"])
                if manifest["content_id"] != plan["content_id"] or changes != plan["changes"]:
                    raise ValueError("import conversion no longer matches preview")
                for directory, _, _ in os.walk(candidate, topdown=False):
                    package._sync_directory(Path(directory))
                candidate.rename(converted)
                package._sync_directory(work)
        manifest = plan["result_manifest"]
        converted_files = {"archive.db": converted / "archive.db", **collect_artifacts((converted,))}
        if set(converted_files) != {item["path"] for item in manifest["files"]}:
            raise ValueError("completed import conversion file set changed")
        for entry in manifest["files"]:
            _owned_file(converted_files[entry["path"]])
            if package._record(entry["path"], converted_files[entry["path"]]) != entry:
                raise ValueError("completed import conversion changed")
        _save(journal, {**value, "phase": "converted", "content_id": manifest["content_id"]})
        stage.mkdir(exist_ok=True)
        for entry in manifest["files"]:
            relative = portable_artifact_parts(entry["path"])
            _copy(converted.joinpath(*relative), stage.joinpath(*relative), entry)
            _save(journal, {**value, "phase": "copying", "content_id": manifest["content_id"],
                            "completed_file": entry["path"], "completed_bytes": entry["size"]})
        rollback = None if plan["strategy"] == "new" else str(work / "archive")
        receipt_value = {"format": RECEIPT, "plan": plan, "rollback_root": rollback}
        receipt = {**receipt_value, "receipt_id": package.digest(receipt_value)}
        path = package._receipt_path(stage, plan["plan_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        _save(path, receipt)
        check_import(stage, plan["plan_id"], staged=True)
        if package._record("package", source) != plan["package_file"] or _target_inventory(target)[0] != plan["target_inventory"]:
            raise ValueError("package or target changed before atomic installation")
        for directory, _, _ in os.walk(stage, topdown=False):
            package._sync_directory(Path(directory))
        _save(journal, {**value, "phase": "verified"})
        if plan["strategy"] == "new":
            if target.exists():
                target.rmdir()
            stage.rename(target)
        else:
            _exchange(stage, target)
        package._sync_directory(target.parent)
        _save(journal, {**value, "phase": "installed"})
    return {**check_import(target, plan["plan_id"]), "reused": False, "target_root": str(target)}
