"""Explicit database, maintenance coordination and artifact scope for an archive.

Only BOOTSTRAP creates or refreshes schema. READ and WRITE open an existing
archive and validate its contract without DDL; MAINTENANCE holds exclusive
access and reads the source. A connection returned for legacy callers owns its
session, so closing it also releases maintenance coordination.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from bili_asr.archive_maintenance import ArchiveAccessError, archive_access
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.storage.database import (
    connect_database, initialize_schema, require_archive_schema, require_manuscript_schema,
)


from bili_asr.archive_modes import ArchiveAccessMode, ArchiveContract


def _checked_path(path: Path) -> None:
    # Preserve lexical paths until links/reparse points have been rejected.
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ArchiveAccessError(f"archive session path cannot use links (link or reparse point): {component}")


class _SessionConnection(sqlite3.Connection):
    _release = None

    def close(self) -> None:
        # A failed close (for example on another thread) leaves the connection
        # alive, so its access lease must remain alive as well.
        super().close()
        release, self._release = self._release, None
        if release is not None:
            release()


class ArchiveSession:
    """Own one connection and its access lease for the complete application use case."""

    def __init__(
        self, archive: str | os.PathLike[str], *, mode: ArchiveAccessMode,
        contract: ArchiveContract = ArchiveContract.RUNTIME,
        artifact_roots: ArtifactRoots | None = None, busy_timeout_ms: int | None = None,
    ) -> None:
        if not isinstance(mode, ArchiveAccessMode) or not isinstance(contract, ArchiveContract):
            raise TypeError("archive session needs explicit access mode and contract")
        requested = Path(archive)
        _checked_path(requested if requested.is_absolute() else Path.cwd() / requested)
        path = Path(os.path.abspath(requested))
        explicit_database = path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} and not path.is_dir()
        self.archive_root = path.parent if explicit_database else path
        self.database_path = path if explicit_database else path / "archive.db"
        self.mode, self.contract = mode, contract
        self.artifact_roots = artifact_roots or ArtifactRoots.of(self.archive_root)
        if self.artifact_roots.archive_root != self.archive_root:
            raise ValueError("artifact scope belongs to a different archive")
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: _SessionConnection | None = None
        self._resources = ExitStack()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("archive session is not open")
        return self._connection

    @contextmanager
    def access(self, *, allow_missing: bool = False) -> Iterator[None]:
        """Coordinate an invocation before its handler opens a database.

        Missing roots can be allowed at a command boundary so the handler can
        report its normal missing-archive error; this never creates archive.db.
        """
        _checked_path(self.archive_root)
        with archive_access(self.archive_root, exclusive=self.mode is ArchiveAccessMode.MAINTENANCE,
                            create_root=allow_missing or self.mode is ArchiveAccessMode.BOOTSTRAP):
            yield

    def open(self) -> "ArchiveSession":
        if self._connection is not None:
            return self
        _checked_path(self.database_path)
        if self.mode is not ArchiveAccessMode.BOOTSTRAP and not self.database_path.is_file():
            raise FileNotFoundError(f"no archive database at {self.archive_root}; run archive init --target-root to create it")
        try:
            self._resources.enter_context(self.access())
            if self.mode is ArchiveAccessMode.BOOTSTRAP:
                self.archive_root.mkdir(parents=True, exist_ok=True)
            connection = connect_database(
                self.database_path, busy_timeout_ms=self.busy_timeout_ms,
                readonly=self.mode in {ArchiveAccessMode.READ, ArchiveAccessMode.MAINTENANCE},
                must_exist=self.mode is not ArchiveAccessMode.BOOTSTRAP, factory=_SessionConnection,
            )
            self._connection = connection
            connection._release = self._release
            if self.mode is ArchiveAccessMode.BOOTSTRAP:
                initialize_schema(connection)
            elif self.contract is not ArchiveContract.NONE:
                require_archive_schema(connection)
            if self.contract is ArchiveContract.MANUSCRIPT:
                require_manuscript_schema(connection)
        except BaseException:
            self.close()
            raise
        return self

    def _release(self) -> None:
        self._connection = None
        self._resources.close()

    def close(self) -> None:
        connection = self._connection
        if connection is not None:
            connection.close()
        else:
            self._resources.close()

    def __enter__(self) -> "ArchiveSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()


def open_archive_connection(
    archive: str | os.PathLike[str], *, mode: ArchiveAccessMode,
    contract: ArchiveContract = ArchiveContract.RUNTIME,
    artifact_roots: ArtifactRoots | None = None, busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Return a configured connection whose close releases its archive session."""
    return ArchiveSession(archive, mode=mode, contract=contract,
                          artifact_roots=artifact_roots, busy_timeout_ms=busy_timeout_ms).open().connection
