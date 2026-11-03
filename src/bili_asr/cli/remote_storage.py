"""Explicit remote-copy and non-self-contained backup commands."""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from bili_asr.artifact_root import roots_for
from bili_asr.diagnostics import write_stderr
from bili_asr.remote_storage import (
    RemoteStorageError,
    SSHDirectoryBackend,
    UnavailableBackend,
    read_ssh_binding,
)
from bili_asr.storage_targets import unique_json_object


def add_remote_parsers(subparsers: argparse._SubParsersAction, *, archive_root: str):
    remote = subparsers.add_parser("remote", help="Explicit SSH directory package copy and restoration")
    actions = remote.add_subparsers(dest="remote_action", required=True)
    for name in ("prepare", "probe", "push", "restore"):
        action = actions.add_parser(name)
        action.set_defaults(database_policy=None)
        action.add_argument("--binding", required=True, help="SSH binding JSON; secrets are environment references")
        action.add_argument("--archive-root", default=archive_root)
        action.add_argument("--artifact-root")
        if name == "push":
            action.add_argument("--package", required=True)
        if name == "restore":
            action.add_argument("--object-id", required=True)
            action.add_argument("--storage-key", required=True)
            action.add_argument("--scratch-root")
    reference = subparsers.add_parser("reference", help="Non-self-contained state backups with explicit dependencies")
    actions = reference.add_subparsers(dest="reference_action", required=True)
    for name in ("save", "inspect", "check", "plan", "restore"):
        action = actions.add_parser(name)
        action.set_defaults(database_policy=None)
        action.add_argument("--archive-root", default=archive_root)
        if name == "save":
            action.add_argument("--artifact-root")
            action.add_argument("--out", required=True)
            action.add_argument("--holds-file", help="Only version 1 holds mapping is accepted; arbitrary ops state is rejected")
            action.add_argument("--retry-control-file", help="Explicitly project retry holds and spent budgets into scope-bound v2 evidence")
            action.add_argument("--retry-control-lock", help="Declared coordination lock of the retry-control owner")
            action.add_argument("--remote-binding", action="append", help="Read instance identity offline; never connect during save")
        else:
            action.add_argument("--file", required=True)
        if name == "check":
            action.add_argument("--online", action="store_true", help="Explicitly read and hash currently available external packages")
            action.add_argument("--storage-target", action="append", help="TARGET_ID=DIRECTORY")
            action.add_argument("--remote-binding", action="append")
        if name == "restore":
            action.add_argument("--source-workers-stopped", action="store_true", help="Record explicit execution handoff attestation; never starts workers")


def _cmd_remote(args):
    try:
        binding = read_ssh_binding(Path(args.binding))
        with SSHDirectoryBackend(binding) as backend:
            if args.remote_action == "prepare":
                report = backend.prepare()
            elif args.remote_action == "probe":
                report = backend.probe()
            else:
                from bili_asr.services.remote_artifacts import (
                    push_remote_package,
                    restore_remote_artifact,
                )
                roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
                if args.remote_action == "push":
                    report = push_remote_package(roots, Path(args.package), backend)
                else:
                    report = restore_remote_artifact(roots, args.object_id, args.storage_key, backend,
                                                      scratch_root=Path(args.scratch_root) if args.scratch_root else None)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Binding files can contain connection data. Do not echo exception text.
        import sys
        error = sys.exception()
        write_stderr("remote: " + getattr(error, "code", "operation_failed"))
        return 1


def _cmd_reference(args):
    from bili_asr.services.reference_backup import (
        check_reference_backup,
        plan_reference_restore,
        restore_reference_backup,
        save_reference_backup,
    )
    try:
        if args.reference_action == "save":
            roots = roots_for(args.archive_root, flag_value=args.artifact_root, require_writable=False)
            holds = None
            if args.holds_file:
                from bili_asr.artifact_inventory import require_regular_file
                path = Path(args.holds_file)
                if require_regular_file(path).st_size > 1024**2:
                    raise ValueError("holds file exceeds 1 MiB")
                holds = json.loads(path.read_bytes(), object_pairs_hook=unique_json_object)
            instances = {}
            for path in args.remote_binding or ():
                binding = read_ssh_binding(Path(path))
                if binding.target_id in instances or binding.instance_id is None:
                    raise ValueError("remote target needs one explicit instance binding")
                instances[binding.target_id] = binding.instance_id
            report = save_reference_backup(roots, Path(args.out), holds=holds, target_instances=instances,
                                           retry_control_path=Path(args.retry_control_file) if args.retry_control_file else None,
                                           retry_control_lock=Path(args.retry_control_lock) if args.retry_control_lock else None)
        elif args.reference_action in {"check", "inspect"}:
            from bili_asr.services.artifact_access import parse_target_bindings
            online = getattr(args, "online", False)
            with ExitStack() as stack:
                backends = {}
                if online:
                    for path in args.remote_binding or ():
                        binding = read_ssh_binding(Path(path))
                        if binding.target_id in backends:
                            raise ValueError("duplicate remote target binding")
                        try:
                            backends[binding.target_id] = stack.enter_context(SSHDirectoryBackend(binding))
                        except RemoteStorageError as error:
                            backends[binding.target_id] = UnavailableBackend(binding, error)
                report = check_reference_backup(Path(args.file), online=online, remote_backends=backends,
                                                local_targets=parse_target_bindings(getattr(args, "storage_target", None)))
            report["operation"] = "reference-" + args.reference_action
        elif args.reference_action == "plan":
            report = plan_reference_restore(Path(args.file), Path(args.archive_root))
        else:
            report = restore_reference_backup(Path(args.file), Path(args.archive_root),
                                               source_workers_stopped=args.source_workers_stopped)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        import sys
        error = sys.exception()
        write_stderr("reference: " + getattr(error, "code", "validation_or_operation_failed"))
        return 1
