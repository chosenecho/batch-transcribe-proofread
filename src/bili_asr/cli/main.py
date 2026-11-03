"""Public entry point: `bili-asr` -> `main()`."""

from __future__ import annotations

import argparse
from importlib import import_module
import sys
import types


def build_parser():
    # Verify the release before importing command modules from the installation.
    from bili_asr.cli.parser import build_parser as create_parser
    return create_parser()


def _dispatch_command(args: argparse.Namespace) -> int:
    from bili_asr.cli.registry import COMMANDS

    spec = COMMANDS.get(args.command)
    if spec is None:
        raise ValueError(f"command {args.command!r} is not implemented")
    return getattr(import_module(spec.handler_module), spec.handler)(args)


def _main(
    argv: list[str] | None = None,
    *,
    _publication_worker: bool = False,
    _asr_worker: bool = False,
) -> int:
    """Invoke parser and dispatcher with explicit module-level test seams."""
    from bili_asr.release_identity import ReleaseIdentityError, require_expected_build
    try:
        require_expected_build()
    except ReleaseIdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    import sqlite3
    from bili_asr.diagnostics import write_stderr
    from bili_asr.storage.database import SchemaContractError, SQLITE_BUSY_TIMEOUT_ENV
    from bili_asr.artifact_root import ArtifactRootError, roots_for
    from bili_asr.archive_maintenance import ArchiveAccessError
    from bili_asr.archive_session import ArchiveSession
    from bili_asr.cli.registry import COMMANDS, ArtifactPolicy

    spec = COMMANDS.get(args.command)
    if spec is None:
        raise ValueError(f"command {args.command!r} is not implemented")
    if getattr(args, "requires_publication_enabled", False):
        from bili_asr.content_rights import ContentRightsError, require_publication_enabled
        try:
            require_publication_enabled()
        except ContentRightsError as exc:
            write_stderr(f"{args.command}: {exc}")
            return 1
    # Resolve configured artifact roots once at the command boundary.
    artifact_policy = getattr(args, "artifact_policy", spec.artifacts)
    metadata_only_search = args.command == "search" and getattr(args, "scope", "transcripts") == "metadata"
    if artifact_policy is not ArtifactPolicy.NONE and not metadata_only_search:
        try:
            if artifact_policy is ArtifactPolicy.WRITE:
                from bili_asr.cli.publication import validate_archive
                validate_archive(args.archive_root)
            args.artifact_roots = roots_for(
                args.archive_root,
                flag_value=args.artifact_root,
                require_writable=artifact_policy is ArtifactPolicy.WRITE,
            )
        except (ArtifactRootError, OSError, ValueError, RuntimeError) as exc:
            write_stderr(f"{args.command}: {exc}")
            return 1
    # Database access is declared by the command/action, independently of
    # artifact policy. The handler owns connection and transaction lifetimes.
    database_policy = spec.database_for(args)
    try:
        if database_policy is not None:
            session = ArchiveSession(getattr(args, spec.archive_argument), mode=database_policy,
                                     artifact_roots=getattr(args, "artifact_roots", None))
            with session.access(allow_missing=True):
                return _dispatch_command(args)
        return _dispatch_command(args)
    except (ArchiveAccessError, SchemaContractError) as exc:
        write_stderr(f"{args.command}: {exc}")
        return 1
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise
        write_stderr(f"{args.command}: SQLite contention timeout exceeded; stop competing writers "
                     f"or increase {SQLITE_BUSY_TIMEOUT_ENV}. Jobs remain recoverable; retry after contention clears.")
        return 1
    except ValueError as exc:
        if not str(exc).startswith(SQLITE_BUSY_TIMEOUT_ENV):
            raise
        write_stderr(f"{args.command}: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    """Module-level entry point retained for ``python -m bili_asr.cli.main``."""
    return _main(argv)

class _CallableMainModule(types.ModuleType):
    """Preserve the historical callable main import."""

    def __call__(self, argv=None):
        return self.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
else:
    sys.modules[__name__].__class__ = _CallableMainModule
    # Compatibility through the next minor release (0.2): old integrations
    # imported cli.main.main while the entrypoint was being split.
    __path__ = []
    sys.modules.setdefault(__name__ + ".main", sys.modules[__name__])
