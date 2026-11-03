"""Explicit artifact inventory, offload plans and catalog upgrade commands."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr.artifact_inventory import require_no_links
from bili_asr.artifact_root import ArtifactRoots, roots_for
from bili_asr.diagnostics import write_stderr
from bili_asr.storage_targets import (
    bind_directory_target,
    sync_directory,
    unique_json_object,
)


def _hold_arguments(parser: argparse.ArgumentParser) -> None:
    holds = parser.add_mutually_exclusive_group()
    holds.add_argument("--holds-file", help="Versioned JSON observation of external retention holds")
    holds.add_argument("--no-external-holds", action="store_true", help="Declare that external retention holds have been checked and none exist")


def _selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", action="append", choices=("audio", "bundle", "document", "release", "source-evidence", "migration"))
    parser.add_argument("--platform", action="append")
    parser.add_argument("--creator-id", action="append")
    parser.add_argument("--video-id", action="append")
    parser.add_argument("--part-id", action="append", type=int)
    parser.add_argument("--version", action="append")
    _hold_arguments(parser)


def add_artifacts_parser(subparsers: argparse._SubParsersAction, *, archive_root: str) -> None:
    parser = subparsers.add_parser("artifacts", help="Inspect artifacts, plan offload and explicitly upgrade the storage catalog")
    actions = parser.add_subparsers(dest="artifacts_action", required=True)
    for name, help_text in (("inventory", "Read-only inventory of actual artifact replicas"),
                            ("plan", "Freeze a deeply verified offload selection without moving files")):
        action = actions.add_parser(name, help=help_text)
        action.set_defaults(database_policy=ArchiveAccessMode.READ)
        action.add_argument("--archive-root", default=archive_root)
        action.add_argument("--artifact-root", default=None)
        action.add_argument("--max-bytes-per-second", type=int)
        action.add_argument("--out", help="New JSON report outside both source roots; otherwise stdout")
        _selection_arguments(action)
        if name == "inventory":
            action.add_argument("--deep", action="store_true", help="Read and hash bytes; the default only observes readable paths")
        else:
            action.add_argument("--target-id", required=True, help="Logical storage target identity")
            action.add_argument("--purpose", choices=("offload", "copy"), default="offload",
                help="Copy freezes stable bytes even while retained; a copy plan never authorizes deletion")
    upgrade = actions.add_parser("upgrade", help="Preserve an archive in a separate target and install artifact storage explicitly")
    upgrade.set_defaults(database_policy=None)
    upgrade.add_argument("--archive-root", required=True, help="Stopped source archive")
    upgrade.add_argument("--artifact-root", default=None)
    upgrade.add_argument("--target-root", required=True, help="Separate new or empty archive directory")
    upgrade.add_argument("--dry-run", action="store_true")
    capture = actions.add_parser("capture-groups", help="Preserve proven current transcript and immutable text groups")
    capture.set_defaults(database_policy=None)
    capture.add_argument("--archive-root", default=archive_root)
    capture.add_argument("--artifact-root", default=None)
    restore_group = actions.add_parser("restore-group", help="Restore a complete original text group")
    restore_group.set_defaults(database_policy=None)
    restore_group.add_argument("--archive-root", default=archive_root)
    restore_group.add_argument("--artifact-root", default=None)
    restore_group.add_argument("--group-id", required=True)
    restore_group.add_argument("--storage-target", action="append")
    status = actions.add_parser("status", help="Read independent durability, locality and readiness observations")
    status.set_defaults(database_policy=None)
    status.add_argument("--archive-root", default=archive_root)
    status.add_argument("--artifact-root", default=None)
    status.add_argument("--storage-target", action="append")
    status.add_argument("--check-targets", action="store_true", help="Probe target identity and manifests without extracting payloads")
    for name in ("policy-show", "policy-set", "policy-run"):
        action = actions.add_parser(name, help="Explicit online artifact policy " + name.removeprefix("policy-"))
        action.set_defaults(database_policy=None)
        action.add_argument("--archive-root", default=archive_root)
        action.add_argument("--artifact-root", default=None)
        action.add_argument("--policy-id", default="default")
        if name == "policy-show":
            action.add_argument("--default-version", type=int, choices=(1, 2), default=1,
                help="Defaults for an unconfigured policy; version 2 is audio-only and forbids deletion")
        if name == "policy-set":
            action.add_argument("--config", required=True, help="Complete version 1 or audio retention version 2 policy JSON")
        if name == "policy-run":
            action.add_argument("--target-root")
            action.add_argument("--storage-target", action="append", help="TARGET_ID=DIRECTORY for each additional audio replica")
            action.add_argument("--watch", action="store_true", help="Poll at the configured scan/backoff deadline")
            action.add_argument("--drain-file")
            _hold_arguments(action)
    for name in ("bind-target", "transfer", "restore", "reconcile", "check"):
        action = actions.add_parser(name, help=f"Artifact storage {name}")
        action.set_defaults(database_policy=None)
        action.add_argument("--archive-root", default=archive_root)
        action.add_argument("--artifact-root", default=None)
        if name == "check":
            action.add_argument("--package", required=True)
            action.add_argument("--expected-sha256")
            continue
        action.add_argument("--target-root", required=True, help="Prepared storage directory; never auto-created")
        if name in {"transfer", "reconcile"}:
            action.add_argument("--plan", required=True)
            _hold_arguments(action)
        else:
            action.add_argument("--target-id", required=True)
        if name == "transfer":
            action.add_argument("--mode", choices=("copy", "offload"), default="copy")
            action.add_argument("--max-package-bytes", type=int, default=1024**3)
            action.add_argument("--max-package-objects", type=int, default=1000)
        if name == "restore":
            action.add_argument("--object-id", required=True, help="Retained SHA-256 object identity")
            action.add_argument("--storage-key", required=True, help="Retained audio relative path to restore")


def _external_holds(args: argparse.Namespace) -> dict[str, tuple[str, ...]] | None:
    if args.no_external_holds:
        return {}
    if not args.holds_file:
        return None
    path = Path(args.holds_file)
    require_no_links(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("holds file exceeds 1 MiB")
    with path.open("rb") as stream:
        encoded = stream.read(1024 * 1024 + 1)
    if len(encoded) > 1024 * 1024:
        raise ValueError("holds file exceeds 1 MiB")
    document = json.loads(encoded, object_pairs_hook=unique_json_object)
    if not isinstance(document, dict) or set(document) != {"version", "holds"} or type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("holds file requires version 1 and an explicit holds mapping")
    if not isinstance(document["holds"], dict):
        raise TypeError("invalid holds mapping: expected reference keys and retention reasons")
    result = {}
    for key, reasons in document["holds"].items():
        if not isinstance(key, str) or not key or not isinstance(reasons, list) or not reasons or any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
            raise ValueError("invalid hold key or retention reason")
        result[key] = tuple(reasons)
    return result


def _write_report(path: Path, report: dict, roots: ArtifactRoots) -> None:
    path = Path(os.path.abspath(path))
    require_no_links(path)
    if any(path.is_relative_to(base) for base in roots.read_bases()):
        raise ValueError("report output must be outside archive and artifact roots")
    if path.exists():
        raise ValueError("report output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".artifact-report-", dir=path.parent) as staging:
        temporary = Path(staging) / "report.json"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
        sync_directory(path.parent)


def _read_plan(path: Path) -> dict:
    require_no_links(path)
    with path.open("rb") as stream:
        encoded = stream.read(16 * 1024**2 + 1)
    if len(encoded) > 16 * 1024**2:
        raise ValueError("plan exceeds 16 MiB")
    return json.loads(encoded, object_pairs_hook=unique_json_object)


def _cmd_artifacts(args: argparse.Namespace) -> int:
    try:
        roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
        if args.artifacts_action == "upgrade":
            from bili_asr.services.artifact_catalog_upgrade import (
                upgrade_artifact_catalog,
            )
            report = upgrade_artifact_catalog(Path(args.archive_root), Path(args.target_root),
                                              artifact_roots=roots, dry_run=args.dry_run)
        elif args.artifacts_action == "bind-target":
            report = bind_directory_target(Path(args.target_root), args.target_id, roots=roots)
        elif args.artifacts_action == "capture-groups":
            from bili_asr.services.artifact_groups import capture_artifact_groups
            report = capture_artifact_groups(roots)
        elif args.artifacts_action == "restore-group":
            from bili_asr.services.artifact_access import parse_target_bindings
            from bili_asr.services.artifact_groups import restore_artifact_group
            report = restore_artifact_group(roots, args.group_id, storage_targets=parse_target_bindings(args.storage_target))
        elif args.artifacts_action == "status":
            from bili_asr.archive_session import ArchiveSession
            from bili_asr.services.artifact_access import parse_target_bindings
            from bili_asr.services.artifact_state import artifact_state
            with ArchiveSession(roots.archive_root, mode=ArchiveAccessMode.READ, artifact_roots=roots) as session:
                report = artifact_state(session.connection, roots, storage_targets=parse_target_bindings(args.storage_target),
                                        check_targets=args.check_targets)
        elif args.artifacts_action.startswith("policy-"):
            from bili_asr.archive_session import ArchiveSession
            from bili_asr.services.artifact_policy import (
                configure_policy,
                read_policy,
                run_policy_once,
            )
            if args.artifacts_action == "policy-run":
                from bili_asr.services.artifact_access import parse_target_bindings
                def checkpoint():
                    if args.drain_file and Path(args.drain_file).exists():
                        raise ValueError("audio_retention_drained")
                try:
                    while True:
                        if args.drain_file and Path(args.drain_file).exists():
                            report = {"state": "drained", "policy_id": args.policy_id}
                            break
                        report = run_policy_once(roots, policy_id=args.policy_id,
                            target_root=None if args.target_root is None else Path(args.target_root), external_holds=lambda: _external_holds(args),
                            storage_targets=parse_target_bindings(args.storage_target), checkpoint=checkpoint)
                        if report.get("reason") == "audio_retention_interrupted":
                            report = {"state": "drained", "policy_id": args.policy_id, "last_run": report}
                            break
                        if args.drain_file and Path(args.drain_file).exists():
                            report = {"state": "drained", "policy_id": args.policy_id, "last_run": report}
                            break
                        if not args.watch or report["state"] == "off":
                            break
                        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
                        deadline = report.get("retry_after", time.time() + 60)
                        while time.time() < deadline:
                            if args.drain_file and Path(args.drain_file).exists():
                                break
                            time.sleep(min(1, max(0, deadline - time.time())))
                except KeyboardInterrupt:
                    report = {"state": "drained", "policy_id": args.policy_id}
            else:
                mode = ArchiveAccessMode.WRITE if args.artifacts_action == "policy-set" else ArchiveAccessMode.READ
                with ArchiveSession(roots.archive_root, mode=mode, artifact_roots=roots) as session:
                    report = (configure_policy(session.connection, args.policy_id, _read_plan(Path(args.config)))
                              if args.artifacts_action == "policy-set" else read_policy(session.connection, args.policy_id,
                                  default_version=args.default_version))
        elif args.artifacts_action == "check":
            from bili_asr.artifact_packages import check_artifact_package
            report = check_artifact_package(Path(args.package), expected_sha256=args.expected_sha256)
        elif args.artifacts_action in {"transfer", "reconcile"}:
            from bili_asr.services.artifact_transfer import (
                reconcile_artifact_transfer,
                transfer_artifacts,
            )
            plan = _read_plan(Path(args.plan))
            options = {"target_root": Path(args.target_root), "external_holds": lambda: _external_holds(args)}
            if args.artifacts_action == "transfer":
                report = transfer_artifacts(roots, plan, mode=args.mode, max_bytes=args.max_package_bytes,
                                            max_objects=args.max_package_objects, **options)
            else:
                report = reconcile_artifact_transfer(roots, plan, **options)
        elif args.artifacts_action == "restore":
            from bili_asr.services.artifact_restore import restore_artifact
            report = restore_artifact(roots, args.object_id, target_id=args.target_id,
                                      target_root=Path(args.target_root), storage_key=args.storage_key)
        else:
            from bili_asr.services.artifact_inventory_service import (
                ArtifactSelection,
                inventory_artifacts,
                plan_artifact_offload, plan_artifact_copy,
            )
            selection = ArtifactSelection(kinds=tuple(args.kind or ()), platforms=tuple(args.platform or ()),
                                          creator_ids=tuple(args.creator_id or ()), video_ids=tuple(args.video_id or ()),
                                          part_ids=tuple(args.part_id or ()), versions=tuple(args.version or ()))
            report = inventory_artifacts(roots.archive_root / "archive.db", roots,
                                         deep=args.artifacts_action == "plan" or args.deep,
                                         selection=selection, external_holds=_external_holds(args),
                                         max_bytes_per_second=args.max_bytes_per_second)
            if args.artifacts_action == "plan":
                planner = plan_artifact_copy if args.purpose == "copy" else plan_artifact_offload
                report = planner(report, target_id=args.target_id)
            if args.out:
                _write_report(Path(args.out), report, roots)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except RecursionError:
        write_stderr(f"artifacts {args.artifacts_action}: JSON nesting exceeds the bound")
        return 1
    except (TypeError, ValueError, OSError, sqlite3.Error) as exc:
        write_stderr(f"artifacts {args.artifacts_action}: {exc}")
        return 1
