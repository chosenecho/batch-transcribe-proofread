"""Explicit empty-target initialization and offline archive conversion."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from bili_asr.diagnostics import write_stderr


def add_archive_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("archive", help="Initialize, inspect or explicitly convert an archive")
    actions = parser.add_subparsers(dest="archive_action", required=True)
    preflight = actions.add_parser(
        "migration-preflight", help="Validate and inventory a stopped, checkpointed legacy archive",
    )
    preflight.add_argument("--source-root", required=True, help="Existing legacy archive directory")
    preflight.add_argument(
        "--source-artifact-root", help="Explicit source artifact root; does not use BILI_ARTIFACT_ROOT",
    )
    preflight.add_argument("--format", choices=("json", "text"), default="json")
    initialize = actions.add_parser("init", help="Explicitly initialize an empty universal-v2 archive")
    initialize.add_argument("--target-root", required=True)
    initialize.add_argument("--with-source-observations", action="store_true",
                            help="Preserve private raw metadata with the explicit source-observations/v1 extension")
    initialize.set_defaults(database_policy=None)
    convert = actions.add_parser("migrate", help="Copy and validate a stopped Bilibili archive into a separate empty universal target")
    source = convert.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-root")
    source.add_argument("--source-snapshot")
    convert.add_argument("--source-artifact-root")
    convert.add_argument("--target-root", required=True)
    convert.add_argument("--dry-run", action="store_true")
    convert.add_argument("--expected-fingerprint")
    convert.set_defaults(database_policy=None)
    check = actions.add_parser("migration-check", help="Verify the immutable migration report and preserved target objects")
    check.add_argument("--target-root", required=True)
    check.set_defaults(database_policy=None)
    paths = actions.add_parser("upgrade-paths", help="List exact registered offline contract upgrade edges")
    paths.set_defaults(database_policy=None)
    derivative = actions.add_parser("derive-vtt", help="Export WebVTT from verified stored segments without ASR")
    derivative.add_argument("--source-root", required=True)
    derivative.add_argument("--source-artifact-root")
    derivative.add_argument("--transcript-id", type=int, required=True)
    derivative.add_argument("--output", type=Path, required=True)
    derivative.set_defaults(database_policy=None)
    plan = actions.add_parser("upgrade-plan", help="Bind an offline source, converter and target to a verifiable upgrade plan")
    plan.add_argument("--source-root", required=True)
    plan.add_argument("--source-artifact-root")
    plan.add_argument("--target-root", required=True)
    plan.add_argument("--target-contract", action="append", required=True)
    plan.add_argument("--edge", action="append", help="Select every edge in order if paths are ambiguous")
    controls = plan.add_mutually_exclusive_group()
    controls.add_argument("--control-state", type=Path, action="append")
    controls.add_argument("--no-external-control-state", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(database_policy=None)
    apply = actions.add_parser("upgrade-apply", help="Revalidate a saved plan and atomically install an isolated target")
    apply.add_argument("--plan", type=Path, required=True)
    apply.set_defaults(database_policy=None)
    upgraded = actions.add_parser("upgrade-check", help="Verify an unchanged upgrade installation baseline")
    upgraded.add_argument("--target-root", required=True)
    upgraded.add_argument("--plan-id", required=True)
    upgraded.add_argument("--audit", action="store_true", help="Report history, current-fact and external-control drift without permitting baseline reuse")
    upgraded.set_defaults(database_policy=None)


def _cmd_archive(args: argparse.Namespace) -> int:
    from bili_asr.services.migration_preflight import migration_preflight
    from bili_asr.services.archive_migration import initialize_archive, migrate_archive, check_migrated_archive, migrate_snapshot

    try:
        if args.archive_action == "derive-vtt":
            from bili_asr.services.transcript_derivatives import export_transcript_vtt
            report = export_transcript_vtt(Path(args.source_root), transcript_id=args.transcript_id, output=args.output,
                artifact_root=Path(args.source_artifact_root) if args.source_artifact_root else None)
        elif args.archive_action.startswith("upgrade-"):
            report = _cmd_upgrade(args)
        elif args.archive_action == "init":
            report = initialize_archive(Path(args.target_root), with_source_observations=args.with_source_observations)
        elif args.archive_action == "migration-check":
            report = check_migrated_archive(Path(args.target_root))
        elif args.archive_action == "migrate":
            options = {"dry_run": args.dry_run, "expected_fingerprint": args.expected_fingerprint}
            if args.source_snapshot:
                if args.source_artifact_root:
                    raise ValueError("snapshot migration cannot select an external artifact root")
                report = migrate_snapshot(Path(args.source_snapshot), Path(args.target_root), **options)
            else:
                report = migrate_archive(Path(args.source_root), Path(args.target_root),
                    artifact_root=Path(args.source_artifact_root) if args.source_artifact_root else None, **options)
        else:
            report = migration_preflight(
            Path(args.source_root),
            artifact_root=Path(args.source_artifact_root) if args.source_artifact_root else None,
        )
    except (ValueError, OSError) as exc:
        write_stderr(f"archive {args.archive_action}: {exc}")
        return 1
    except sqlite3.Error:
        write_stderr(f"archive {args.archive_action}: database validation failed")
        return 1
    if getattr(args, "format", "json") == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        totals = report["totals"]
        print(f"Source contract: {report['source']['contract_id']}")
        print(f"Source fingerprint: {report['source']['fingerprint']}")
        print(f"Tables: {len(report['tables'])}; rows: {totals['table_rows']}; "
              f"files: {totals['file_count']}; bytes: {totals['file_bytes']}")
        print("Mode: stopped-checkpointed; conversion performed: false; target created: false")
        print("Recovery candidates: " + json.dumps(report["recovery_candidates"], sort_keys=True))
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> dict:
    from dataclasses import asdict
    from bili_asr.contracts.registry import UPGRADE_EDGES
    from bili_asr.services.archive_upgrade import apply_upgrade, check_upgrade, plan_upgrade
    if args.archive_action == "upgrade-paths":
        return {"format_version": 1, "upgrades": [asdict(edge) for edge in UPGRADE_EDGES]}
    if args.archive_action == "upgrade-check":
        if args.audit:
            from bili_asr.services.archive_upgrade import audit_upgrade
            return audit_upgrade(Path(args.target_root), expected_plan_id=args.plan_id)
        return check_upgrade(Path(args.target_root), expected_plan_id=args.plan_id)
    if args.archive_action == "upgrade-apply":
        return apply_upgrade(json.loads(args.plan.read_text(encoding="utf-8")))
    report = plan_upgrade(Path(args.source_root), Path(args.target_root),
        artifact_root=Path(args.source_artifact_root) if args.source_artifact_root else None,
        target_contracts=tuple(args.target_contract), selected_path=tuple(args.edge) if args.edge else None,
        control_state=tuple(args.control_state or ()), no_external_control_state=args.no_external_control_state)
    from bili_asr.artifact_inventory import require_no_links
    require_no_links(args.output.absolute())
    # Never overwrite an earlier plan or put operational evidence into source/target.
    output = args.output.absolute()
    for root in (args.source_root, args.source_artifact_root, args.target_root):
        if root and output.is_relative_to(Path(root).absolute()):
            raise ValueError("upgrade plan output must be outside source/artifact/target roots")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report
