"""Argument parser for the SQLite workflow CLI."""

from __future__ import annotations

import argparse

from bili_asr.cli._shared import DEFAULT_ARCHIVE_ROOT, _UsageErrorArgumentParser


def build_parser() -> argparse.ArgumentParser:
    parser = _UsageErrorArgumentParser(
        prog="batch-transcribe",
        description="Batch transcription, AI proofreading, review and archive CLI.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command")
    from bili_asr.cli.transcribe import add_transcribe_parser
    add_transcribe_parser(subparsers)

    from bili_asr.cli.delivery import add_delivery_parser
    add_delivery_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.production import add_production_parser
    add_production_parser(subparsers)

    from bili_asr.cli.workflow import add_workflow_parser
    add_workflow_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.maintenance import add_maintenance_parser
    add_maintenance_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.maintenance_package import add_maintenance_package_parser
    add_maintenance_package_parser(subparsers)
    from bili_asr.cli.task_state import add_task_state_parser
    add_task_state_parser(subparsers)
    from bili_asr.cli.publication import add_publication_parser
    from bili_asr.cli.editorial import add_editorial_parser
    add_publication_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    add_editorial_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.provenance import add_provenance_parser
    add_provenance_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.production_acceptance import add_production_acceptance_parser
    add_production_acceptance_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.snapshot import add_snapshot_parser
    add_snapshot_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.archive import add_archive_parser
    add_archive_parser(subparsers)
    from bili_asr.cli.artifacts import add_artifacts_parser
    add_artifacts_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.remote_storage import add_remote_parsers
    add_remote_parsers(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)
    from bili_asr.cli.local_input import add_input_parser
    add_input_parser(subparsers, archive_root=DEFAULT_ARCHIVE_ROOT)

    dedup = subparsers.add_parser("dedup", help="Inspect exact content reuse without changing the archive")
    dedup_actions = dedup.add_subparsers(dest="dedup_action", required=True)
    report = dedup_actions.add_parser("report", help="Report exact audio and transcript reuse")
    report.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)
    report.add_argument("--format", choices=("text", "json"), default="text")
    report.add_argument("--limit", type=int, default=20)

    status = subparsers.add_parser("status", help="Show SQLite archive and workflow status")
    status.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)

    runs = subparsers.add_parser("runs", help="Inspect historical collection records offline")
    runs.add_argument("--limit", type=int, default=None)
    runs.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)

    search = subparsers.add_parser("search", help="Search stored video metadata and transcript segments")
    search.add_argument("query")
    search.add_argument(
        "--scope", choices=("transcripts", "metadata", "all"), default="transcripts",
        help="Search transcripts (default), title/description/tags, or both; all puts metadata first",
    )
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--rebuild", action="store_true", help="Update the transcript FTS index before searching (transcripts/all only)")
    search.add_argument("--from", dest="pubdate_from", default=None, metavar="YYYY-MM-DD")
    search.add_argument("--to", dest="pubdate_to", default=None, metavar="YYYY-MM-DD")
    search.add_argument("--format", choices=("table", "json"), default="table")
    search.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)

    search_index = subparsers.add_parser("search-index", help="Build or update SQLite FTS5")
    search_index.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)

    coverage = subparsers.add_parser("coverage", help="Report workflow coverage from SQLite")
    coverage.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)
    coverage.add_argument("--scope", default=None)
    coverage.add_argument("--format", choices=("json", "csv"), default="json")
    coverage.add_argument("--strict", action="store_true")

    verify = subparsers.add_parser("verify", help="Verify workflow artifacts")
    verify.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)
    verify.add_argument("--scope", default=None)
    verify.add_argument("--format", choices=("json", "text"), default="json")
    verify.add_argument("--strict", action="store_true")

    export = subparsers.add_parser("export", help="Export workflow records as JSON or CSV")
    export.add_argument("--format", choices=("json", "csv"), required=True)
    export.add_argument("--out", default=None)
    export.add_argument("--status", action="append", default=None)
    export.add_argument("--with-text", action="store_true")
    export.add_argument("--private", action="store_true", help="Acknowledge private text export when compliance mode is enabled")
    export.add_argument("--archive-root", default=DEFAULT_ARCHIVE_ROOT)

    environment = subparsers.add_parser("check-asr-env", help="Inspect ASR deployment or verify the AMD WSL recipe")
    environment.add_argument("--backend", choices=("amd-wsl", "cuda", "rocm", "hcu"), default="amd-wsl")
    environment.add_argument("--probe-gpu", action="store_true", help="Bounded GPU tensor and model-class import probe, no model loading")
    environment.add_argument("--probe-timeout", type=float, default=20)
    environment.add_argument("--cache-root", default=None)

    from bili_asr.cli.registry import add_policy_arguments
    add_policy_arguments(subparsers)
    return parser
