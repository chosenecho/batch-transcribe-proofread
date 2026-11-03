"""Offline maintenance package CLI; services own every access boundary."""
from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from contextlib import ExitStack
from pathlib import Path


def add_maintenance_package_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("maintenance", help="Offline verified archive packages and recoverable imports")
    actions = parser.add_subparsers(dest="maintenance_action", required=True)
    export = actions.add_parser("export", help="Export all history or native video/part dependency closure")
    export.add_argument("--source-root", type=Path, required=True)
    export.add_argument("--artifact-root", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--part-id", type=int, action="append")
    export.add_argument("--bvid", action="append")
    export.add_argument("--profile", choices=("private-history", "workflow-transcripts", "database-only", "database-subtitles", "publication"), default="private-history")
    export.add_argument("--dataset", action="append")
    export.add_argument("--artifact-type", action="append")
    export.add_argument("--directory", action="append")
    export.add_argument("--since", type=int)
    export.add_argument("--until", type=int)
    export.add_argument("--status", action="append")
    for action in ("inspect", "verify"):
        child = actions.add_parser(action)
        child.add_argument("--package", type=Path, required=True)
    plan = actions.add_parser("import-plan", help="Verify and preflight without creating a target")
    plan.add_argument("--package", type=Path, required=True)
    plan.add_argument("--target-root", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--strategy", choices=("new", "replace", "merge"), default="new")
    plan.add_argument("--part-id", type=int, action="append")
    plan.add_argument("--bvid", action="append")
    plan.add_argument("--dataset", action="append")
    plan.add_argument("--artifact-type", action="append")
    plan.add_argument("--directory", action="append")
    plan.add_argument("--since", type=int)
    plan.add_argument("--until", type=int)
    plan.add_argument("--status", action="append")
    plan.add_argument("--profile", choices=("private-history", "workflow-transcripts", "database-only", "database-subtitles"), default="private-history")
    apply = actions.add_parser("import-apply", help="Revalidate a saved plan and atomically install a staged target")
    apply.add_argument("--plan", type=Path, required=True)
    check = actions.add_parser("import-check", help="Verify the immutable installation baseline")
    check.add_argument("--target-root", type=Path, required=True)
    check.add_argument("--plan-id", required=True)
    delta = actions.add_parser("delta-save", help="Save verified file changes against a full or incremental baseline")
    delta.add_argument("--source-root", type=Path, required=True)
    delta.add_argument("--artifact-root", type=Path)
    delta.add_argument("--output", type=Path, required=True)
    delta.add_argument("--baseline", type=Path, required=True)
    delta.add_argument("--compression", choices=("stored", "deflate"), default="stored")
    delta.add_argument("--part-id", type=int, action="append")
    delta.add_argument("--bvid", action="append")
    delta.add_argument("--dataset", action="append")
    delta.add_argument("--artifact-type", action="append")
    delta.add_argument("--directory", action="append")
    delta.add_argument("--since", type=int)
    delta.add_argument("--until", type=int)
    delta.add_argument("--status", action="append")
    delta.add_argument("--profile", choices=("private-history", "workflow-transcripts", "database-only", "database-subtitles"), default="private-history")
    chain = actions.add_parser("chain-verify", help="Verify a complete offline snapshot dependency chain")
    chain.add_argument("--package", type=Path, required=True)
    materialize = actions.add_parser("chain-materialize", help="Materialize a verified chain as an importable full package")
    materialize.add_argument("--package", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    retention = actions.add_parser("retention-plan", help="Prove safe deletion while retaining referenced baselines")
    retention.add_argument("--repository", type=Path, required=True)
    retention.add_argument("--keep-count", type=int, default=5)
    retention.add_argument("--keep-days", type=int, default=30)
    retention.add_argument("--output", type=Path, required=True)
    prune = actions.add_parser("retention-apply")
    prune.add_argument("--plan", type=Path, required=True)
    checkpoint = actions.add_parser("checkpoint", help="Refuse active writers and leases before SQLite WAL truncation")
    checkpoint.add_argument("--source-root", type=Path, required=True)
    configure = actions.add_parser("backup-policy", help="Create an explicit local automatic backup policy")
    configure.add_argument("--source-root", type=Path, required=True)
    configure.add_argument("--artifact-root", type=Path)
    configure.add_argument("--repository", type=Path, required=True)
    configure.add_argument("--output", type=Path, required=True)
    configure.add_argument("--interval-seconds", type=int, default=3600)
    configure.add_argument("--keep-count", type=int, default=5)
    configure.add_argument("--keep-days", type=int, default=30)
    configure.add_argument("--alert-after", type=int, default=3)
    configure.add_argument("--mode", choices=("full", "incremental", "differential"), default="incremental")
    configure.add_argument("--profile", choices=("private-history", "workflow-transcripts", "database-only", "database-subtitles", "publication", "task-state"), default="private-history")
    configure.add_argument("--external-components", type=Path, help="JSON object with a components list of typed state declarations")
    configure.add_argument("--no-external-control-state", action="store_true", help="Explicitly declare this archive has no external retry state")
    configure.add_argument("--media-policy", choices=("local-required", "verified-references"), default="local-required",
                           help="Explicit task-state v2 references to freshly verified cold audio replicas")
    configure.add_argument("--dataset", action="append")
    configure.add_argument("--artifact-type", action="append")
    configure.add_argument("--directory", action="append")
    configure.add_argument("--since", type=int)
    configure.add_argument("--until", type=int)
    configure.add_argument("--status", action="append")
    configure.add_argument("--compression", choices=("stored", "deflate"), default="stored")
    configure.add_argument("--part-id", type=int, action="append")
    configure.add_argument("--bvid", action="append")
    run = actions.add_parser("backup-run", help="Run one due time, explicit event, or manual backup")
    run.add_argument("--policy", type=Path, required=True)
    run.add_argument("--trigger", choices=("time", "event", "manual"), default="manual")
    run.add_argument("--event-id")
    run.add_argument("--storage-target", action="append", help="TARGET_ID=DIRECTORY for explicit cold reference verification")
    run.add_argument("--remote-binding", action="append", type=Path, help="SSH target binding for explicit cold reference verification")
    status = actions.add_parser("backup-status")
    status.add_argument("--repository", type=Path, required=True)
    status.add_argument("--policy-id", required=True)


def _scope(args):
    from bili_asr.storage.maintenance_scope import scope
    if any((args.dataset, args.artifact_type, args.directory, args.since is not None,
            args.until is not None, args.status, getattr(args, "part_id", None), getattr(args, "bvid", None),
            getattr(args, "profile", "private-history") != "private-history")):
        return scope(datasets=args.dataset, artifact_types=args.artifact_type,
                     directories=args.directory, since=args.since, until=args.until,
                     statuses=args.status, profile=getattr(args, "profile", "private-history"),
                     part_ids=getattr(args, "part_id", None), bvids=getattr(args, "bvid", None))
    return None


def _write_new(document: dict, output: Path) -> None:
    from bili_asr.services.maintenance_package import _sync_file, absolute, canonical
    out = absolute(output)
    if not out.parent.is_dir():
        raise ValueError("maintenance report parent must exist")
    with out.open("xb") as stream:
        stream.write(canonical(document) + b"\n")
        _sync_file(stream)


def _cmd_maintenance_package(args: argparse.Namespace) -> int:
    from bili_asr.services.maintenance_package import (
        apply_import,
        check_import,
        export_package,
        inspect_package,
        plan_import,
        verify_package,
        write_plan,
    )
    action = args.maintenance_action
    try:
        if action == "export":
            if args.profile == "publication":
                from bili_asr.services.maintenance_publication import (
                    export_public_profile,
                )
                if any((args.part_id, args.bvid, args.dataset, args.artifact_type, args.directory,
                        args.since is not None, args.until is not None, args.status)):
                    raise ValueError("public publication profile uses current approved releases; use publication export for release selection")
                report = export_public_profile(args.source_root, args.output, artifact_root=args.artifact_root)
            else:
                report = export_package(args.source_root, args.output, part_ids=args.part_id,
                                        bvids=args.bvid, artifact_root=args.artifact_root, scope=_scope(args))
        elif action == "inspect":
            report = inspect_package(args.package)
        elif action == "verify":
            report = verify_package(args.package)
        elif action == "import-plan":
            report = plan_import(args.package, args.target_root, strategy=args.strategy, resumable=True, scope=_scope(args))
            write_plan(report, args.output)
        elif action == "import-apply":
            report = apply_import(args.plan)
        elif action == "import-check":
            report = check_import(args.target_root, plan_id=args.plan_id)
        elif action == "delta-save":
            from bili_asr.services.maintenance_chain import save_delta
            report = save_delta(args.source_root, args.output, args.baseline, artifact_root=args.artifact_root, compression=args.compression, scope=_scope(args))
        elif action == "chain-verify":
            from bili_asr.services.maintenance_chain import verify_chain
            report = verify_chain(args.package)
        elif action == "chain-materialize":
            from bili_asr.services.maintenance_chain import materialized_package
            report = materialized_package(args.package, args.output)
        elif action == "retention-plan":
            from bili_asr.services.maintenance_chain import retention_plan
            report = retention_plan(args.repository, keep_count=args.keep_count, keep_days=args.keep_days)
            _write_new(report, args.output)
        elif action == "retention-apply":
            from bili_asr.services.maintenance_chain import apply_retention
            from bili_asr.services.maintenance_package import _json
            report = apply_retention(_json(args.plan))
        elif action == "checkpoint":
            from bili_asr.services.maintenance_backup import checkpoint_archive
            report = checkpoint_archive(args.source_root)
        elif action == "backup-policy":
            from bili_asr.services.maintenance_backup import policy
            from bili_asr.services.maintenance_package_core import _json
            task_state = args.profile == "task-state"
            components = {"components": []} if args.external_components is None else _json(args.external_components)
            if set(components) != {"components"} or not isinstance(components["components"], list):
                raise ValueError("external components file must contain only a components list")
            if task_state and any((args.dataset, args.artifact_type, args.directory, args.since is not None,
                                   args.until is not None, args.status, args.part_id, args.bvid)):
                raise ValueError("task-state backs up complete task history without row selectors")
            report = policy(args.source_root, args.repository, interval_seconds=args.interval_seconds,
                            keep_count=args.keep_count, keep_days=args.keep_days, alert_after=args.alert_after,
                            artifact_root=args.artifact_root, selection=None if task_state else _scope(args),
                            mode=args.mode, compression=args.compression, profile="task-state" if task_state else "archive",
                            external_components=components["components"],
                            no_external_control_state=args.no_external_control_state, media_policy=args.media_policy)
            _write_new(report, args.output)
        elif action == "backup-run":
            from bili_asr.remote_storage import SSHDirectoryBackend, read_ssh_binding
            from bili_asr.services.artifact_access import parse_target_bindings
            from bili_asr.services.maintenance_backup import run_backup
            from bili_asr.services.maintenance_package import _json
            document = _json(args.policy)
            if (args.storage_target or args.remote_binding) and document.get("media_policy") != "verified-references":
                raise ValueError("target bindings require explicit verified-references task-state policy")
            with ExitStack() as stack:
                backends = {}
                for path in args.remote_binding or ():
                    binding = read_ssh_binding(path)
                    if binding.target_id in backends or binding.instance_id is None:
                        raise ValueError("cold references require one exact remote instance per target")
                    backends[binding.target_id] = stack.enter_context(SSHDirectoryBackend(binding))
                report = run_backup(document, trigger=args.trigger, event_id=args.event_id,
                                    local_targets=parse_target_bindings(args.storage_target), remote_backends=backends)
        elif action == "backup-status":
            from bili_asr.services.maintenance_backup import backup_status
            report = backup_status(args.repository, args.policy_id)
        else:
            raise ValueError("unsupported maintenance action")
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"operation": "maintenance-" + action, "valid": False,
                          "error": {"code": "maintenance-validation-failed", "message": str(exc)}}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("valid", True) else 1
