"""One daily operator surface, reusing native planning, workers and recovery."""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from bili_asr.diagnostics import write_stderr


def add_production_parser(subparsers) -> None:
    parser = subparsers.add_parser("production", help="Configure, run, stop, diagnose and migrate a production archive")
    actions = parser.add_subparsers(dest="production_action", required=True)
    entries = {}
    for name in ("init", "config", "run", "start", "stop", "resume", "restart", "status", "doctor",
                 "plan", "proofread", "backup", "inspect", "restore"):
        entries[name] = actions.add_parser(name)
        entries[name].add_argument("--config", type=Path, default=Path("production.json"))
    init = entries["init"]
    init.add_argument("--archive-root", default="archive", help="Resolved relative to the profile, not the current directory")
    init.add_argument("--artifact-root", default=None)
    controls = init.add_mutually_exclusive_group()
    controls.add_argument("--controls", type=Path, help="JSON object with typed components using their actual coordination locks")
    controls.add_argument("--no-external-controls", action="store_true")
    entries["config"].add_argument("--set", action="append", default=[], metavar="KEY=JSON_OR_STRING")
    entries["run"].add_argument("--_run-id", default=None, help=argparse.SUPPRESS)
    entries["start"].add_argument("--startup-timeout", type=float, default=10)
    entries["stop"].add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    entries["stop"].add_argument("--timeout", type=float, default=None)
    for name in ("status",):
        entries[name].add_argument("--jobs", action="store_true")
        entries[name].add_argument("--failed", action="store_true")
        entries[name].add_argument("--limit", type=int, default=100)
    plan = entries["plan"]
    selection = plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--part-id", type=int, action="append")
    selection.add_argument("--bvid", action="append")
    selection.add_argument("--all-parts", action="store_true")
    plan.add_argument("--page-index", type=int, default=None)
    plan.add_argument("--asr-policy", choices=("all", "selected"), default="all")
    plan.add_argument("--proofread", action="store_true")
    proofread = entries["proofread"]
    selection = proofread.add_mutually_exclusive_group(required=True)
    selection.add_argument("--part-id", type=int)
    selection.add_argument("--base-transcript-id", type=int)
    reference = proofread.add_mutually_exclusive_group()
    reference.add_argument("--reference-transcript-id", type=int)
    reference.add_argument("--no-reference", action="store_true")
    proofread.add_argument("--refresh-input", action="store_true")
    entries["backup"].add_argument("--out", type=Path, required=True)
    entries["backup"].add_argument("--compression", choices=("stored", "deflate"), default="stored")
    for name in ("inspect", "restore"):
        entries[name].add_argument("--directory", type=Path, required=True)
    entries["inspect"].add_argument("--jobs", action="store_true", help="Read pending jobs and frozen runtime requirements offline")
    entries["restore"].add_argument("--target", type=Path, required=True)
    entries["restore"].add_argument("--dry-run", action="store_true")


def _plan(args, profile, *, proofread: bool):
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.canonical_json import digest
    from bili_asr.editorial import EditorialConfig
    from bili_asr.production_profile import asr_config
    from bili_asr.services.workflow_application import WorkflowApplication
    from bili_asr.workflow_models import AsrPolicy, AsrProfile
    with ArchiveSession(profile["archive_root"], mode=ArchiveAccessMode.WRITE) as session:
        application = WorkflowApplication(session)
        config = EditorialConfig(**profile["editorial"])
        if proofread:
            return application.proofread(part_id=args.part_id, base_transcript_id=args.base_transcript_id,
                reference_transcript_id=args.reference_transcript_id, no_reference=args.no_reference,
                config=config, refresh_input=args.refresh_input)
        values = asdict(asr_config(profile))
        values.pop("local_source")
        values["model_revision"] = values["model_revision"] or ""
        frozen = AsrProfile(profile_key="production-" + digest(values)[:16], **values)
        planned = application.plan(part_ids=args.part_id, bvids=args.bvid, page_index=args.page_index,
            all_parts=args.all_parts, policy=AsrPolicy(args.asr_policy), profile=frozen,
            editorial_config=config.to_dict() if args.proofread else None)
        return {"profile_id": planned.profile_id, "jobs": asdict(planned.jobs),
                "targets": [asdict(target) for target in planned.selection.targets]}


def _cmd_production(args) -> int:
    from bili_asr.production_profile import (
        default_profile,
        load_profile,
        resolved_profile,
        save_profile,
        update_profile,
    )
    from bili_asr.production_runtime import (
        resume_production,
        run_production,
        runtime_status,
        start_production,
        stop_production,
    )
    from bili_asr.services.retry_maintenance_io import read_json
    action, path = args.production_action, args.config.absolute()
    try:
        if action == "init":
            components = [] if args.no_external_controls else None
            if args.controls is not None:
                declaration = read_json(args.controls)
                if set(declaration) != {"components"}:
                    raise ValueError("control declaration must contain only components")
                components = declaration["components"]
            profile = default_profile(archive_root=args.archive_root, external_controls=components)
            profile["artifact_root"] = args.artifact_root
            save_profile(path, profile, create=True)
            result = {"config": str(path), "archive_created": False, "external_controls_declared": components is not None}
        elif action == "config":
            result = update_profile(path, args.set) if args.set else load_profile(path)
        elif action == "run":
            result = run_production(path, run_id=args._run_id)
        elif action == "start":
            if not 0 < args.startup_timeout <= 60:
                raise ValueError("startup timeout must be within 0..60 seconds")
            result = start_production(path, startup_timeout=args.startup_timeout)
        elif action == "stop":
            if args.timeout is not None and not 0 < args.timeout <= 3600:
                raise ValueError("stop timeout must be within 0..3600 seconds")
            result = stop_production(path, wait=args.wait, timeout=args.timeout)
        elif action == "resume":
            result = resume_production(path)
        elif action == "restart":
            stop_production(path)
            resume_production(path)
            result = start_production(path)
        elif action == "backup":
            from bili_asr.production_backup import save_production_backup
            result = save_production_backup(path, args.out, compression=args.compression)
        elif action in {"inspect", "restore"}:
            from bili_asr.production_backup import (
                restore_production_backup,
                verify_production_backup,
            )
            result = (verify_production_backup(args.directory) if action == "inspect" else
                      restore_production_backup(args.directory, args.target, dry_run=args.dry_run))
            if action == "inspect" and args.jobs:
                from bili_asr.services.archive_recovery import inspect_snapshot
                result["archive_state"] = inspect_snapshot(args.directory / "archive.zip")
        else:
            profile = resolved_profile(path)
            if action in {"plan", "proofread"}:
                result = _plan(args, profile, proofread=action == "proofread")
            elif action == "doctor":
                from bili_asr.artifact_root import roots_for
                from bili_asr.services.archive_recovery import doctor_archive
                roots = roots_for(profile["archive_root"], flag_value=profile["artifact_root"] or profile["archive_root"],
                                  require_writable=False)
                result = doctor_archive(Path(profile["archive_root"]), artifact_roots=roots,
                                        runtime_bindings=profile["runtime"]["runtime_bindings"])
                result["production"] = runtime_status(path)
            else:
                from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
                from bili_asr.services.workflow_status import workflow_status_report
                controls = [item for item in profile["external_controls"] or [] if item["kind"].startswith("retry-control/")]
                result = runtime_status(path)
                with ArchiveSession(profile["archive_root"], mode=ArchiveAccessMode.READ) as session:
                    result["workflow"] = workflow_status_report(session.connection,
                        include_jobs=args.jobs, failed_only=args.failed, limit=args.limit,
                        control_state_path=controls[0]["path"] if controls else None,
                        no_control_state=profile["external_controls"] == [])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if (result.get("data_complete") is False or result.get("target_ready") is False
                or result.get("runtime_ready") is False):
            return 1
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        write_stderr(f"production {action}: {exc}")
        return 1
