"""Checked handoff without the optional publication lifecycle."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bili_asr.diagnostics import write_stderr


def add_delivery_parser(subparsers, *, archive_root: str) -> None:
    parser = subparsers.add_parser("delivery", help="Hand off checked outputs to independent data consumers")
    actions = parser.add_subparsers(dest="delivery_action", required=True)
    export = actions.add_parser("export", help="Create a new immutable delivery; no edition or approval required")
    export.add_argument("--archive-root", default=archive_root)
    export.add_argument("--artifact-root", default=None)
    export.add_argument("--revision-id", action="append", default=[])
    export.add_argument("--transcript-id", type=int, action="append", default=[])
    export.add_argument("--extensions", type=Path, help="Optional free-form JSON metadata; preserved without field pruning")
    export.add_argument("--attachment", action="append", default=[], help="Portable NAME=FILE; e.g. user-info.json=/path/user.json")
    export.add_argument("--out", type=Path, required=True)
    check = actions.add_parser("check", help="Verify a delivery offline, without an archive database")
    check.add_argument("--directory", type=Path, required=True)
    listing = actions.add_parser("list", help="Discover explicit delivery IDs; listing does not certify quality")
    listing.add_argument("--archive-root", default=archive_root)
    listing.add_argument("--part-id", type=int, default=None)
    listing.add_argument("--kind", choices=("editorial", "transcript"), default="editorial")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)


def _cmd_delivery(args) -> int:
    from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
    from bili_asr.artifact_root import roots_for
    from bili_asr.delivery import export_delivery, verify_delivery
    from bili_asr.services.retry_maintenance_io import read_json
    try:
        if args.delivery_action == "check":
            result = verify_delivery(args.directory)
        elif args.delivery_action == "list":
            if not 1 <= args.limit <= 1000 or args.offset < 0:
                raise ValueError("list requires limit 1..1000 and nonnegative offset")
            with ArchiveSession(args.archive_root, mode=ArchiveAccessMode.READ) as session:
                if args.kind == "editorial":
                    rows = session.connection.execute(
                        "SELECT r.revision_id,r.input_id,r.quality_status,r.created_at,i.video_part_id "
                        "FROM editorial_revisions r JOIN editorial_inputs i USING(input_id) "
                        "WHERE (? IS NULL OR i.video_part_id=?) ORDER BY r.created_at,r.revision_id LIMIT ? OFFSET ?",
                        (args.part_id, args.part_id, args.limit + 1, args.offset)).fetchall()
                else:
                    rows = session.connection.execute(
                        "SELECT transcript_id,video_part_id,source_kind,language,model_id,version,content_sha256 "
                        "FROM transcripts WHERE (? IS NULL OR video_part_id=?) "
                        "ORDER BY transcript_id LIMIT ? OFFSET ?",
                        (args.part_id, args.part_id, args.limit + 1, args.offset)).fetchall()
                result = {"kind": args.kind, "records": [dict(row) for row in rows[:args.limit]],
                          "next_offset": args.offset + args.limit if len(rows) > args.limit else None,
                          "quality_checked": False}
        else:
            attachments = {}
            for item in args.attachment:
                name, separator, path = item.partition("=")
                if not separator or not name or not path or name in attachments:
                    raise ValueError("attachments require unique NAME=FILE bindings")
                attachments[name] = Path(path)
            extensions = None if args.extensions is None else read_json(args.extensions)
            roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
            with ArchiveSession(args.archive_root, mode=ArchiveAccessMode.READ, artifact_roots=roots) as session:
                result = export_delivery(session.connection, output=args.out, artifact_roots=roots.read_bases(),
                    revision_ids=args.revision_id, transcript_ids=args.transcript_id,
                    extensions=extensions, attachments=attachments)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        write_stderr(f"delivery: {exc}")
        return 1
