"""Internal review exports have an explicit command and separate output contract."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bili_asr.cli.publication import _common, archive_connection, print_result
from bili_asr.cli.registry import ArtifactPolicy
from bili_asr.diagnostics import write_stderr


def _cmd_editorial(args) -> int:
    from bili_asr.publication_export import export_editorial

    try:
        if args.editorial_action in {"compare", "compare-report", "compare-review", "record-review"}:
            from bili_asr.services.editorial_comparison import (
                compare_presets,
                comparison_report,
                record_review,
                write_review_packet,
            )
            ledger = Path(args.ledger)
            if args.editorial_action == "compare":
                from bili_asr.storage.editorial import EditorialRepository
                with closing(archive_connection(args.archive_root)) as connection:
                    prepared = EditorialRepository(connection).load_input(args.input_id)
                    if args.allow_paid_calls:
                        from bili_asr.editorial_quality import check_source_quality
                        check_source_quality(prepared, connection=connection)
                pricing = None if args.pricing is None else json.loads(Path(args.pricing).read_text(encoding="utf-8"))
                result = compare_presets(prepared, ledger, endpoint=args.editorial_base_url,
                    model=args.editorial_model, allow_paid_calls=args.allow_paid_calls,
                    maximum=args.max_physical_calls, max_output_tokens=args.max_output_tokens, pricing=pricing)
            elif args.editorial_action == "compare-report":
                result = comparison_report(ledger)
            elif args.editorial_action == "compare-review":
                result = write_review_packet(ledger, Path(args.out))
            else:
                review = json.loads(Path(args.review).read_text(encoding="utf-8"))
                result = record_review(ledger, review, Path(args.out))
            print_result(result, "json")
            return 0
        if args.editorial_action == "replay-index":
            from bili_asr.storage.editorial_replay import install_indexes
            with closing(archive_connection(args.archive_root, readonly=False)) as connection:
                print_result(install_indexes(connection, recover=args.recover), "json")
            return 0
        if args.editorial_action == "call-integrity":
            from bili_asr.storage.editorial_replay import integrity_report
            with closing(archive_connection(args.archive_root)) as connection:
                print_result(integrity_report(connection, limit=args.limit), "json")
            return 0
        if args.editorial_action == "plan":
            from dataclasses import replace

            from bili_asr.editorial import EditorialConfig
            from bili_asr.editorial_budget import plan_report, replan
            from bili_asr.storage.editorial import EditorialRepository

            with closing(archive_connection(args.archive_root)) as connection:
                original = EditorialRepository(connection).load_input(args.input_id)
            config = EditorialConfig.from_snapshot(original["snapshot"]["config"])
            plans = []
            for preset in args.preset or ("baseline-v1", "large-v1"):
                guards = EditorialConfig.preset(preset)
                candidate = replace(config, budget_policy="token-budget-v2", chunk_preset=preset,
                                    max_chunk_segments=guards.max_chunk_segments, max_chunk_chars=guards.max_chunk_chars)
                plans.append(plan_report(replan(original, candidate)))
            print_result({"source_input_id": args.input_id, "plans": plans, "physical_calls": 0}, "json")
            return 0
        with closing(archive_connection(args.archive_root)) as connection:
            result = export_editorial(
                connection, revision_id=args.revision_id, edition_id=args.edition_id,
                artifact_roots=args.artifact_roots.read_bases(), output=Path(args.out),
                include_provenance=args.provenance,
            )
        print_result(result, args.format)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        write_stderr(f"editorial: {getattr(exc, 'error_code', getattr(exc, 'code', type(exc).__name__))}: {exc}")
        return 1


def add_editorial_parser(subparsers, *, archive_root: str) -> None:
    parser = subparsers.add_parser("editorial", help="Export AI 合成稿件 and 校验参照稿件 for internal review")
    actions = parser.add_subparsers(dest="editorial_action", required=True)
    replay = actions.add_parser("replay-index", help="Explicitly install exactly validated derived response-reuse indexes")
    _common(replay, archive_root)
    from bili_asr.archive_modes import ArchiveAccessMode
    replay.set_defaults(database_policy=ArchiveAccessMode.WRITE)
    replay.add_argument("--recover", action="store_true", help="Complete a partial group only when every present DDL matches")
    integrity = actions.add_parser("call-integrity", help="Locate malformed call JSON without blocking unrelated requests")
    _common(integrity, archive_root)
    integrity.add_argument("--limit", type=int, default=100)
    plan = actions.add_parser("plan", help="Compare chunk presets over one immutable saved input without calling a model")
    _common(plan, archive_root)
    plan.add_argument("--input-id", required=True)
    plan.add_argument("--preset", action="append", choices=("baseline-v1", "large-v1"))
    comparison = actions.add_parser("compare", help="Opt in to a bounded preset comparison in a dedicated ledger")
    _common(comparison, archive_root)
    comparison.add_argument("--input-id", required=True)
    comparison.add_argument("--ledger", required=True)
    comparison.add_argument("--editorial-base-url", required=True)
    comparison.add_argument("--editorial-model", required=True)
    comparison.add_argument("--max-physical-calls", type=int, default=8)
    comparison.add_argument("--max-output-tokens", type=int)
    comparison.add_argument("--pricing", help="JSON with dated, explicit prices; omitted prices remain unknown")
    comparison.add_argument("--allow-paid-calls", action="store_true")
    for action, help_text in (("compare-report", "Read saved comparison usage, errors and structural coverage"),
                              ("compare-review", "Export a private semantic review packet"),
                              ("record-review", "Save a named review bound to the comparison and report hashes")):
        command = actions.add_parser(action, help=help_text)
        _common(command, archive_root)
        command.add_argument("--ledger", required=True)
        if action != "compare-report":
            command.add_argument("--out", required=True)
        if action == "record-review":
            command.add_argument("--review", required=True)
    export = actions.add_parser("export", help="Export selected edition, fixed references and differences")
    _common(export, archive_root, artifacts=ArtifactPolicy.READ)
    export.add_argument("--revision-id", required=True)
    export.add_argument("--edition-id", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--provenance", action="store_true", help="Include the complete portable private evidence chain")
