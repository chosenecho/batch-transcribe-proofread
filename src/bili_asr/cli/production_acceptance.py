"""CLI for immutable production acceptance records."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bili_asr.diagnostics import write_stderr
from bili_asr.production_acceptance import (
    ProductionAcceptanceError,
    record_acceptance,
    show_record_file,
    verify_record_file,
)


def _cmd_production_acceptance(args: argparse.Namespace) -> int:
    action = args.acceptance_action
    try:
        if action == "record":
            result = record_acceptance(
                input_path=Path(args.input),
                release_manifest=Path(args.release_manifest),
                archive_root=Path(args.archive_root),
                worker_health_dir=Path(args.worker_health_dir) if args.worker_health_dir else None,
                output_path=Path(args.output),
                confirm_production=args.confirm_production_verified,
            )
        elif action == "verify":
            result = verify_record_file(Path(args.record))
        elif action == "show":
            result = show_record_file(Path(args.record))
        else:  # argparse restricts this; retain a fail-closed dispatch.
            raise ProductionAcceptanceError("unsupported acceptance action")
    except (OSError, ValueError, RuntimeError) as error:
        write_stderr(f"production-acceptance {action}: {error}")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0


def add_production_acceptance_parser(subparsers, *, archive_root: str) -> None:
    parser = subparsers.add_parser(
        "production-acceptance",
        help="Create and inspect immutable, operator-attested production acceptance records",
        description=("Record production checks independently of release.json and worker health. "
                     "Verify only checks record consistency; it does not attest production truth."),
    )
    actions = parser.add_subparsers(dest="acceptance_action", required=True)
    record = actions.add_parser("record", help="Write one new immutable acceptance record")
    record.add_argument("--input", required=True, help="Bounded operator input JSON (no paths or credentials)")
    record.add_argument("--release-manifest", required=True, help="Immutable release.json; matching wheel must sit beside it")
    record.add_argument("--archive-root", default=archive_root, help="Archive whose contract identity is read-only inspected")
    record.add_argument("--worker-health-dir", default=None, help="Optional worker heartbeat directory; only identity fields are copied")
    record.add_argument("--output", required=True, help="New record path; an existing file is never overwritten")
    record.add_argument("--confirm-production-verified", action="store_true",
                        help="Explicit operator attestation; has effect only when every required check is pass")
    verify = actions.add_parser("verify", help="Check record schema and derived status only")
    verify.add_argument("record", help="Acceptance record JSON")
    show = actions.add_parser("show", help="Read a validated acceptance record")
    show.add_argument("record", help="Acceptance record JSON")
    for command in (record, verify, show):
        command.set_defaults(database_policy=None, artifact_policy=None)
