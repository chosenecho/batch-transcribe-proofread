"""Private provenance traces and portable evidence without starting workers."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from bili_asr.cli.publication import _common, archive_connection, print_result
from bili_asr.cli.registry import ArtifactPolicy
from bili_asr.diagnostics import write_stderr


def _cmd_provenance(args) -> int:
    from bili_asr.provenance_contract import read_graph
    from bili_asr.provenance_report import report_graph
    from bili_asr.services.provenance import export_trace, public_summary, trace
    try:
        if args.provenance_action == "read":
            result = report_graph(read_graph(Path(args.file)))
        else:
            with closing(archive_connection(args.archive_root)) as connection:
                roots = args.artifact_roots.read_bases()
                if args.provenance_action == "summary":
                    result = public_summary(connection, args.release_id, roots)
                else:
                    selector = {name: getattr(args, name, None) for name in
                                ("edition_id", "release_id", "revision_id", "artifact_sha256")}
                    result = (export_trace(connection, output=Path(args.out), artifact_roots=roots, **selector)
                              if args.provenance_action == "export" else report_graph(trace(connection, artifact_roots=roots, **selector)))
        print_result(result, args.format)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, UnicodeError) as error:
        write_stderr(f"provenance {args.provenance_action}: {error}")
        return 1


def add_provenance_parser(subparsers, *, archive_root: str) -> None:
    parser = subparsers.add_parser("provenance", help="Trace final manuscripts through frozen private evidence")
    actions = parser.add_subparsers(dest="provenance_action", required=True)
    for action in ("trace", "export"):
        command = actions.add_parser(action, help="Read a complete private evidence chain")
        _common(command, archive_root, artifacts=ArtifactPolicy.READ)
        selector = command.add_mutually_exclusive_group(required=True)
        for name in ("edition-id", "release-id", "revision-id", "artifact-sha256"):
            selector.add_argument("--" + name)
        if action == "export":
            command.add_argument("--out", required=True, help="New private UTF-8 evidence file; existing files are refused")
    read = actions.add_parser("read", help="Verify and read a private evidence file entirely offline")
    read.add_argument("--file", required=True)
    read.add_argument("--format", choices=("text", "json"), default="json")
    read.set_defaults(database_policy=None, artifact_policy=ArtifactPolicy.NONE)
    summary = actions.add_parser("summary", help="Read a fixed public summary for an authorized current release")
    _common(summary, archive_root, artifacts=ArtifactPolicy.READ)
    summary.add_argument("--release-id", required=True)
    summary.set_defaults(requires_publication_enabled=True)
