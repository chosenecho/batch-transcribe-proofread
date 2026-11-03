"""Portable full recovery points: native snapshot plus configuration and controls."""
from __future__ import annotations

import hashlib
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path

from bili_asr.portable_bundle import (
    json_bytes,
    new_bundle,
    new_directory,
    seal_bundle,
    verify_bundle,
    write_file,
)
from bili_asr.production_profile import load_profile, resolved_profile, validate_profile
from bili_asr.services.archive_snapshot import (
    check_snapshot,
    restore_snapshot,
    save_snapshot,
)
from bili_asr.services.maintenance_task_state import (
    _control,
    _observe,
    validate_components,
)
from bili_asr.services.retry_maintenance_io import maintenance_lock, read_json
from bili_asr.workflow_supervisor import _slot_lock

FORMAT = "asr-production-backup/v1"


def save_production_backup(path: Path, output: Path, *, compression: str = "stored") -> dict:
    path, output = Path(path).absolute(), Path(output).absolute()
    with maintenance_lock(path.with_name("." + path.name + ".lock")):
        original = load_profile(path)
        profile = resolved_profile(path, original)
        if profile["external_controls"] is None:
            raise ValueError("declare typed external_controls or explicitly set external_controls=[] before backup")
        components = validate_components(profile["external_controls"])
        roots = [Path(profile["archive_root"]), Path(profile["artifact_root"] or profile["archive_root"])]
        if any(output.is_relative_to(root) or root.is_relative_to(output) for root in roots):
            raise ValueError("production backups must be outside the source roots")
        if path.is_relative_to(output) or any(Path(item["path"]).is_relative_to(output) for item in components):
            raise ValueError("backup destination contains a source component")
        with ExitStack() as resources:
            for lock in sorted({item["lock_path"] for item in components}):
                resources.enter_context(maintenance_lock(Path(lock)))
            resources.enter_context(_slot_lock(roots[0]))
            before = _observe(components)
            with new_bundle(output) as stage:
                from bili_asr.services.artifact_access import parse_target_bindings
                snapshot = save_snapshot(roots[0], stage / "archive.zip",
                    artifact_root=roots[1] if roots[1] != roots[0] else None,
                    storage_targets=parse_target_bindings(profile["runtime"]["storage_targets"]),
                    compression=compression)
                write_file(stage, "profile.json", json_bytes(original))
                files, controls = ["archive.zip", "profile.json"], []
                for index, entry in enumerate(before):
                    name = f"controls/component-{index}.json"
                    content = Path(entry["source_path"]).read_bytes()
                    if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                        raise ValueError("external control changed during capture")
                    write_file(stage, name, content)
                    files.append(name)
                    controls.append({"kind": entry["kind"], "path": name})
                if _observe(components) != before:
                    raise ValueError("external controls changed across snapshot capture")
                manifest = seal_bundle(stage, format=FORMAT, files=files, controls=controls,
                    snapshot_id=snapshot["snapshot_id"], self_contained_data=True,
                    runtime_included=False, credentials_included=False)
    return {"output": str(output), "bundle_id": manifest["bundle_id"],
            "snapshot_id": snapshot["snapshot_id"], "external_controls": len(controls)}


def verify_production_backup(directory: Path) -> dict:
    directory = Path(directory)
    manifest = verify_bundle(directory, format=FORMAT)
    profile = load_profile(directory / "profile.json")
    controls = manifest.get("controls")
    if not isinstance(controls, list) or profile["external_controls"] is None or len(controls) != len(profile["external_controls"]):
        raise ValueError("backup controls do not match the declared production profile")
    names = {entry["path"] for entry in manifest["files"]}
    if names != {"archive.zip", "profile.json"} | {f"controls/component-{i}.json" for i in range(len(controls))}:
        raise ValueError("unsupported production backup inventory")
    for index, component in enumerate(controls):
        name = f"controls/component-{index}.json"
        if component != {"kind": profile["external_controls"][index]["kind"], "path": name}:
            raise ValueError("backup control identity mismatch")
        _control(read_json(directory / name), component["kind"])
    snapshot = check_snapshot(directory / "archive.zip")
    if snapshot["snapshot_id"] != manifest.get("snapshot_id"):
        raise ValueError("production backup refers to another native snapshot")
    return {"manifest": manifest, "profile": profile, "snapshot": snapshot}


def restore_production_backup(directory: Path, target: Path, *, dry_run: bool = False) -> dict:
    directory, target = Path(directory).absolute(), Path(target).absolute()
    verified = verify_production_backup(directory)
    if target.exists() or directory.is_relative_to(target) or target.is_relative_to(directory):
        raise ValueError("restore requires a new destination outside the backup")
    if dry_run:
        from bili_asr.services.archive_recovery import plan_restore
        plan = plan_restore(directory / "archive.zip", target / "archive")
        return {"dry_run": True, "target": str(target), "workers_started": False,
                "data_complete": plan["data_complete"], "target_ready": plan["target_ready"],
                "archive_plan": plan}
    with new_directory(target) as stage:
        result = restore_snapshot(directory / "archive.zip", stage / "archive", report_path=stage / "recovery.ndjson")
        profile = deepcopy(verified["profile"])
        profile.update(archive_root="archive", artifact_root=None, external_controls=[])
        profile["runtime"].update(runtime_bindings=None, storage_targets=[], warmup_audio=None, cache_root=None)
        for component in verified["manifest"]["controls"]:
            name = component["path"]
            write_file(stage, name, (directory / name).read_bytes())
            profile["external_controls"].append({"kind": component["kind"], "path": "./" + name,
                                                 "lock_path": "./" + name + ".lock"})
        validate_profile(profile)
        write_file(stage, "production.json", json_bytes(profile))
        write_file(stage, "recovery.json", json_bytes({"backup_bundle_id": verified["manifest"]["bundle_id"],
            "snapshot_id": result["snapshot_id"], "recovered": result["recovered"], "workers_started": False,
            "runtime_rebinding_required": True}))
    return {"target": str(target), "archive_root": str(target / "archive"),
            "config": str(target / "production.json"), "installed": True,
            "snapshot_id": result["snapshot_id"], "recovered": result["recovered"], "workers_started": False}
