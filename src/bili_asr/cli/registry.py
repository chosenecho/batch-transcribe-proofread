"""Registered CLI handlers and their artifact access policy."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from bili_asr.archive_modes import ArchiveAccessMode


class ArtifactPolicy(Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class CommandSpec:
    handler: str
    handler_module: str
    artifacts: ArtifactPolicy
    database: ArchiveAccessMode | None
    write_flags: tuple[str, ...] = ()
    archive_argument: str = "archive_root"

    def __post_init__(self) -> None:
        if not isinstance(self.handler_module, str) or not self.handler_module.startswith("bili_asr.cli."):
            raise TypeError("command needs an explicit handler owner")
        if not isinstance(self.artifacts, ArtifactPolicy):
            raise TypeError("command artifact policy must be ArtifactPolicy")
        if self.database is not None and not isinstance(self.database, ArchiveAccessMode):
            raise TypeError("command database policy must be ArchiveAccessMode or None")
        if not isinstance(self.write_flags, tuple) or not all(isinstance(name, str) for name in self.write_flags):
            raise TypeError("command write flags must be a tuple of argument names")
        if not isinstance(self.archive_argument, str) or not self.archive_argument:
            raise TypeError("command archive argument must name a parsed argument")

    def database_for(self, args) -> ArchiveAccessMode | None:
        if any(getattr(args, name, False) for name in self.write_flags):
            return ArchiveAccessMode.WRITE
        return getattr(args, "database_policy", self.database)


COMMANDS = MappingProxyType({
    "runs": CommandSpec("_cmd_runs", "bili_asr.cli.status_cmd", ArtifactPolicy.NONE, ArchiveAccessMode.READ),
    "input": CommandSpec("_cmd_input", "bili_asr.cli.local_input", ArtifactPolicy.NONE, None),
    "production": CommandSpec("_cmd_production", 'bili_asr.cli.production', ArtifactPolicy.NONE, None),
    "delivery": CommandSpec("_cmd_delivery", 'bili_asr.cli.delivery', ArtifactPolicy.NONE, None),
    "transcribe": CommandSpec("_cmd_transcribe", "bili_asr.cli.transcribe", ArtifactPolicy.NONE, None),
    "task-state": CommandSpec("_cmd_task_state", 'bili_asr.cli.task_state', ArtifactPolicy.NONE, None),
    "maintenance": CommandSpec("_cmd_maintenance_package", 'bili_asr.cli.maintenance_package', ArtifactPolicy.NONE, None),
    "retry-maintenance": CommandSpec("_cmd_retry_maintenance", 'bili_asr.cli.maintenance', ArtifactPolicy.NONE, None),
    "workflow": CommandSpec("_cmd_workflow", 'bili_asr.cli.workflow', ArtifactPolicy.NONE, ArchiveAccessMode.WRITE, write_flags=("apply",)),
    "snapshot": CommandSpec("_cmd_snapshot", 'bili_asr.cli.snapshot', ArtifactPolicy.NONE, ArchiveAccessMode.MAINTENANCE),
    "archive": CommandSpec("_cmd_archive", 'bili_asr.cli.archive', ArtifactPolicy.NONE, ArchiveAccessMode.MAINTENANCE, archive_argument="source_root"),
    "artifacts": CommandSpec("_cmd_artifacts", 'bili_asr.cli.artifacts', ArtifactPolicy.NONE, ArchiveAccessMode.READ),
    "remote": CommandSpec("_cmd_remote", 'bili_asr.cli.remote_storage', ArtifactPolicy.NONE, None),
    "reference": CommandSpec("_cmd_reference", 'bili_asr.cli.remote_storage', ArtifactPolicy.NONE, None),
    "status": CommandSpec("_cmd_status", 'bili_asr.cli.status_cmd', ArtifactPolicy.NONE, ArchiveAccessMode.READ),
    "coverage": CommandSpec("_cmd_coverage", 'bili_asr.cli.status_cmd', ArtifactPolicy.READ, ArchiveAccessMode.READ),
    "verify": CommandSpec("_cmd_verify", 'bili_asr.cli.ops', ArtifactPolicy.READ, ArchiveAccessMode.READ),
    "search": CommandSpec("_cmd_search", 'bili_asr.cli.search', ArtifactPolicy.READ, ArchiveAccessMode.READ, write_flags=("rebuild",)),
    "search-index": CommandSpec("_cmd_search_index", 'bili_asr.cli.search', ArtifactPolicy.READ, ArchiveAccessMode.WRITE),
    "check-asr-env": CommandSpec("_cmd_check_asr_env", 'bili_asr.cli.ops', ArtifactPolicy.NONE, None),
    "export": CommandSpec("_cmd_export", 'bili_asr.cli.ops', ArtifactPolicy.READ, ArchiveAccessMode.READ),
    "publication": CommandSpec("_cmd_publication", 'bili_asr.cli.publication', ArtifactPolicy.NONE, ArchiveAccessMode.READ),
    "editorial": CommandSpec("_cmd_editorial", 'bili_asr.cli.editorial', ArtifactPolicy.NONE, ArchiveAccessMode.READ),
    "provenance": CommandSpec("_cmd_provenance", 'bili_asr.cli.provenance', ArtifactPolicy.NONE, None),
    "production-acceptance": CommandSpec("_cmd_production_acceptance", 'bili_asr.cli.production_acceptance', ArtifactPolicy.NONE, None),
    "dedup": CommandSpec("_cmd_dedup", 'bili_asr.cli.dedup', ArtifactPolicy.READ, ArchiveAccessMode.READ),
})


def add_policy_arguments(subparsers: argparse._SubParsersAction) -> None:
    """Add the configured artifact root to commands that inspect artifacts."""
    from bili_asr.cli._shared import _ARTIFACT_ROOT_HELP

    if set(subparsers.choices) != set(COMMANDS):
        raise ValueError("CLI parser commands do not match the command registry")
    for name, parser in subparsers.choices.items():
        spec = COMMANDS[name]
        if spec.artifacts is not ArtifactPolicy.NONE:
            parser.add_argument("--artifact-root", default=None, help=_ARTIFACT_ROOT_HELP)
