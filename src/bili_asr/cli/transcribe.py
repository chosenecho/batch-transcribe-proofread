from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from bili_asr.archive_modes import ArchiveAccessMode


def add_transcribe_parser(subparsers):
    parser = subparsers.add_parser(
        "transcribe", help="Transcribe audio using a DashScope API key (Qwen Audio 3.1)"
    )
    actions = parser.add_subparsers(dest="transcribe_action", required=True)
    local = actions.add_parser(
        "file", help="Transcribe one local audio file without a GPU"
    )
    local.add_argument("--audio", type=Path, required=True)
    local.add_argument(
        "--expected-sha256", help="Refuse if the input does not match this SHA-256"
    )
    youtube = actions.add_parser(
        "youtube",
        help="Transcribe downloaded YouTube audio from an explicit target list",
    )
    youtube.add_argument("--archive-root", type=Path, required=True)
    youtube.add_argument(
        "--input",
        type=Path,
        required=True,
        help="YouTube target CSV, JSON, or line list",
    )
    youtube.set_defaults(database_policy=ArchiveAccessMode.READ)
    for action in (local, youtube):
        action.add_argument(
            "--output-root",
            type=Path,
            required=True,
            help="Separate cloud result directory",
        )
        action.add_argument(
            "--api-key-file",
            type=Path,
            help="Private UTF-8 file containing the API key; otherwise DASHSCOPE_API_KEY",
        )
        action.add_argument(
            "--retry-failed",
            action="store_true",
            help="Explicitly retry failed or interrupted paid requests, preserving attempts",
        )
    return parser


def _read_api_key(path):
    try:
        key = (
            path.read_text("utf-8-sig").strip()
            if path
            else os.environ.get("DASHSCOPE_API_KEY", "").strip()
        )
    except (OSError, UnicodeError):
        raise ValueError("api_key_file_unavailable") from None
    if not key or any(c.isspace() for c in key):
        raise ValueError("Set DASHSCOPE_API_KEY or provide a private --api-key-file")
    return key


def _cmd_transcribe(args):
    from bili_asr.asr import bailian

    key = None
    try:
        key = _read_api_key(args.api_key_file)
        if args.transcribe_action == "youtube":
            from bili_asr.services.bailian_batch import run_youtube_batch

            return run_youtube_batch(
                archive_root=args.archive_root,
                input_path=args.input,
                output_root=args.output_root,
                api_key=key,
                retry_failed=args.retry_failed,
            )
        if args.expected_sha256 and not re.fullmatch(
            "[a-fA-F0-9]{64}", args.expected_sha256
        ):
            raise ValueError("expected_sha256_must_be_64_hex_characters")
        expected = (
            args.expected_sha256.lower()
            if args.expected_sha256
            else bailian.sha256(args.audio)
        )
        result = bailian.transcribe(
            args.audio,
            args.output_root,
            api_key=key,
            expected_sha256=expected,
            retry_failed=args.retry_failed,
        )
        print(
            json.dumps(
                {"model": bailian.MODEL, "source": "asr-cloud", **result},
                ensure_ascii=False,
            )
        )
        return 1 if result["requires_review"] else 0
    except (ValueError, bailian.CloudError) as exc:
        message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
        print("transcribe: " + message, file=sys.stderr)
    except sqlite3.Error:
        print(
            "transcribe: archive_database_unavailable_or_incompatible", file=sys.stderr
        )
    except (OSError, subprocess.SubprocessError):
        print(
            "transcribe: input_or_output_unavailable_or_ffmpeg_failed", file=sys.stderr
        )
    return 1
