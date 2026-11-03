"""Database implementation."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from importlib import resources
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterator, TypeAlias
from bili_asr.storage.schema_contract import (SchemaContractError, _validate_connection, _normalize_view_sql, _strip_sql_comments, normalize_schema_sql as _normalize_manuscript_sql)
from bili_asr.storage.models import ALLOWED_ATTEMPT_OUTCOMES, ALLOWED_RUN_OUTCOMES


DatabaseConnection: TypeAlias = sqlite3.Connection


_ARCHIVE_DATABASE_NAME = "archive.db"


_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


_TERMINAL_RUN_OUTCOMES = ALLOWED_RUN_OUTCOMES - frozenset({"running"})


_TERMINAL_ACQUISITION_OUTCOMES = frozenset({"complete", "partial", "failed"})


_NO_TRANSCRIPT_ATTEMPT_OUTCOMES = ALLOWED_ATTEMPT_OUTCOMES - {"stored", "unchanged"}


_SCHEMA_RESOURCE = resources.files(__package__).joinpath("schema.sql")


_TRANSCRIPT_SCHEMA_RESOURCE = resources.files(__package__).joinpath(
    "schema-transcripts.sql"
)

_WORKFLOW_SCHEMA_RESOURCE = resources.files(__package__).joinpath("schema-workflow.sql")
_EDITORIAL_SCHEMA_RESOURCE = resources.files(__package__).joinpath("schema-editorial.sql")

_LEGACY_MANUSCRIPT_OBJECTS = frozenset({
    "reading_document_editions", "reading_publications", "reading_publication_events",
})


_TRANSCRIPT_CONTRACT_COLUMNS = frozenset({"language", "content_sha256"})


_SUBTITLE_SCHEMA_OBJECTS = (
    "acquisition_attempts",
    "acquisition_runs",
    "v_pending_subtitles",
)


_PUBDATE_CHUNK = 900


SQLITE_BUSY_TIMEOUT_ENV = "BILI_SQLITE_BUSY_TIMEOUT_MS"
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def sqlite_busy_timeout_ms() -> int:
    """One bounded contention policy for application and heartbeat connections."""
    raw = os.environ.get(SQLITE_BUSY_TIMEOUT_ENV, str(DEFAULT_BUSY_TIMEOUT_MS))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{SQLITE_BUSY_TIMEOUT_ENV} must be an integer from 1 to 300000") from None
    if not 1 <= value <= 300_000:
        raise ValueError(f"{SQLITE_BUSY_TIMEOUT_ENV} must be an integer from 1 to 300000")
    return value


def connect_database(
    path: str | os.PathLike[str], *, busy_timeout_ms: int | None = None,
    readonly: bool = False, must_exist: bool = False,
    factory: type[sqlite3.Connection] = sqlite3.Connection,
) -> DatabaseConnection:
    """Open a thread-owned connection; callers decide whether to bootstrap."""
    timeout = sqlite_busy_timeout_ms() if busy_timeout_ms is None else busy_timeout_ms
    if type(timeout) is not int or not 1 <= timeout <= 300_000:
        raise ValueError("busy_timeout_ms must be an integer from 1 to 300000")
    if readonly or must_exist:
        target = Path(path).absolute().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    else:
        target = path
    connection = sqlite3.connect(target, uri=readonly or must_exist, factory=factory,
                                 isolation_level="DEFERRED", timeout=timeout / 1000)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(timeout)}")
        if readonly:
            connection.execute("PRAGMA query_only = ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _rebuild_error(detail: str) -> SchemaContractError:
    return SchemaContractError(
        f"incompatible archive database ({detail}); preserve archive.db and its artifacts. "
        "Use archive migration-preflight and explicit archive migrate into a separate empty target "
        "for a supported legacy contract; ordinary access never upgrades or deletes an archive."
    )


def duration_to_ms(seconds: int | float) -> int:
    """Convert a non-negative duration in seconds to floored milliseconds."""
    if isinstance(seconds, bool):
        raise TypeError("duration must be numeric")
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise TypeError("duration must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("duration must be finite and non-negative")
    return math.floor(value * 1000)


def normalize_page_index(page_number: int) -> int:
    """Convert a one-based Bilibili page number to a zero-based part index."""
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise TypeError("page number must be an integer")
    if page_number < 1:
        raise ValueError("page number must be at least 1")
    return page_number - 1


def _resolve_database_path(path: str | os.PathLike[str]) -> str | os.PathLike[str]:
    """Accept either an archive root or an explicit SQLite database path.

    ``:memory:`` opens an unnamed in-memory database. URI strings are not
    interpreted: a ``file:``-prefixed value is handled as an ordinary file
    name like any other explicit path.
    """
    value = os.fspath(path)
    if value == ":memory:":
        return value

    candidate = Path(value)
    if candidate.is_dir() or (
        not candidate.exists() and candidate.suffix.lower() not in _DATABASE_SUFFIXES
    ):
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate / _ARCHIVE_DATABASE_NAME
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _transcripts_columns(connection: sqlite3.Connection) -> frozenset[str]:
    """Return the column names of ``transcripts``; empty when it is absent."""
    return frozenset(
        row[1] for row in connection.execute("PRAGMA table_info(transcripts)")
    )


def _schema_object_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Return every table and view name the database declares."""
    return frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    )


def _has_subtitle_schema(connection: sqlite3.Connection) -> bool:
    """Report whether the transcript-schema contract is present."""
    if not _TRANSCRIPT_CONTRACT_COLUMNS <= _transcripts_columns(connection):
        return False
    return set(_SUBTITLE_SCHEMA_OBJECTS) <= _schema_object_names(connection)


_VIEW_NAME_RE = re.compile(
    r"CREATE\s+VIEW(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\b",
    re.IGNORECASE | re.DOTALL,
)


_BARE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


_SQL_KEYWORDS = frozenset(
    {"if", "not", "exists", "as", "select", "with", "values", "table", "view"}
)


def _statement_view_name(statement: str) -> str | None:
    """The view name a shipped statement defines, or ``None``.

    Comments are stripped first: a leading comment block that mentions "CREATE
    VIEW" would otherwise be matched instead of the statement's own clause.
    """

    body = "\n".join(
        line for line in statement.splitlines() if not line.lstrip().startswith("--")
    )
    match = _VIEW_NAME_RE.match(body.strip())
    if match is None:
        return None
    name = match.group(1)
    if name.casefold() in _SQL_KEYWORDS:
        return None
    return name


def _shipped_view_bodies() -> dict[str, str]:
    """The ``name -> CREATE VIEW`` bodies this build ships, keyed by view name.

    Read from the checked-in transcript script so there is one source of truth:
    the body compared against ``sqlite_master`` is the body this build would
    create.  A statement that fails to parse is skipped rather than guessed at —
    ``executescript`` below reports that failure with its own error.
    """

    script = _TRANSCRIPT_SCHEMA_RESOURCE.read_text(encoding="utf-8")
    bodies: dict[str, str] = {}
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if "CREATE VIEW" not in statement.upper():
            continue
        name = _statement_view_name(statement)
        if name is not None:
            bodies[name] = statement
    return bodies




def refresh_shipped_views(connection: sqlite3.Connection) -> int:
    """Recreate any shipped view whose stored body differs from this build's.

    ``CREATE VIEW IF NOT EXISTS`` means a re-executed schema script never updates
    a view that already exists, so a corrected predicate would silently never
    reach an existing archive.  This closes that gap without making every open a
    write:

    * the stored body is compared against the shipped one first, and nothing is
      touched when they agree — so a read-only archive, or one opened while
      another handle holds a read transaction, is not forced into a write it does
      not need;
    * a refresh runs inside a **savepoint**, so a failure cannot leave the view
      absent: ``_transaction`` commits but never issues BEGIN, which would let a
      DROP autocommit (measured: a failing CREATE then left the view missing from
      ``sqlite_master``), whereas ``ROLLBACK TO`` restores it.

    Views the shipped script defines are the only ones considered; anything else
    in the database is left alone.  Returns the number of views refreshed.
    """

    shipped = _shipped_view_bodies()
    if not shipped:
        return 0
    # Positional indexing, like ``_schema_object_names``: a caller may hand us a
    # wrapped connection without ``row_factory = sqlite3.Row``, and reading by
    # name here would raise ``TypeError`` on it.
    stored = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'view' AND sql IS NOT NULL"
        )
    }
    stale = [
        name
        for name, body in shipped.items()
        if name in stored
        and _normalize_view_sql(stored[name]) != _normalize_view_sql(body)
    ]
    if not stale:
        return 0
    # The names come from this build's own script and each is re-validated as a
    # bare identifier before it can reach any DDL string.
    for name in stale:
        if not _BARE_IDENTIFIER_RE.fullmatch(name):
            raise sqlite3.DatabaseError(f"refusing to refresh view {name!r}")
    # A SAVEPOINT, not ``_transaction``: that helper commits but never issues
    # BEGIN, so a DROP inside it would autocommit and a failing CREATE would
    # leave the view *absent* — measured, and the reason this is not a plain
    # transaction block.  A savepoint is also safe when a transaction is already
    # open around this call.
    connection.execute("SAVEPOINT refresh_shipped_views")
    try:
        for name in stale:
            connection.execute(f"DROP VIEW IF EXISTS {name}")
            connection.execute(shipped[name].replace("IF NOT EXISTS ", "", 1))
    except BaseException:
        # Suppress a failure in the rollback itself so the ORIGINAL error is what
        # the caller sees; a masked syntax error would be much harder to place.
        for undo in ("ROLLBACK TO refresh_shipped_views", "RELEASE refresh_shipped_views"):
            try:
                connection.execute(undo)
            except sqlite3.Error:
                continue
        raise
    connection.execute("RELEASE refresh_shipped_views")
    return len(stale)


@lru_cache(maxsize=1)
def _manuscript_schema_objects() -> dict[str, tuple[str, str]]:
    """Read the exact manuscript DDL contract without touching the archive."""
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(_EDITORIAL_SCHEMA_RESOURCE.read_text(encoding="utf-8"))
        return {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in reference.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }


def require_manuscript_schema(connection: sqlite3.Connection) -> None:
    """Reject missing, legacy or altered manuscript contracts using reads only."""
    from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2, require_universal_contract
    if runtime_contract(connection) == UNIVERSAL_V2:
        require_universal_contract(connection)
        return
    expected = _manuscript_schema_objects()
    actual = {
        str(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE sql IS NOT NULL"
        )
    }
    valid = not (_LEGACY_MANUSCRIPT_OBJECTS & actual.keys())
    valid = valid and all(
        name in actual and actual[name][0] == kind
        and _normalize_manuscript_sql(actual[name][1]) == _normalize_manuscript_sql(sql)
        for name, (kind, sql) in expected.items()
    )
    if valid:
        valid = [tuple(row) for row in connection.execute(
            "SELECT version FROM manuscript_contract"
        )] == [(1,)]
    if valid:
        return
    raise SchemaContractError(
        "manuscript-schema-contract: archive uses a missing, legacy or altered "
        "manuscript schema; create a separate new archive (no in-place migration)"
    )


def require_editorial_schema(connection: sqlite3.Connection) -> None:
    """Require the same fixed manuscript contract for AI editorial operations."""
    require_manuscript_schema(connection)


def _accepts_manuscript_script(connection: sqlite3.Connection) -> bool:
    names = _schema_object_names(connection)
    if not names:
        return True
    editorial_names = _manuscript_schema_objects().keys()
    if names & (set(editorial_names) | _LEGACY_MANUSCRIPT_OBJECTS):
        require_manuscript_schema(connection)
        return True
    # Metadata-only archives remain usable for metadata; manuscript commands
    # require an explicitly new contract rather than silently upgrading them.
    return False


def _schema_scripts() -> tuple[str, ...]:
    return tuple(resource.read_text(encoding="utf-8") for resource in (
        _SCHEMA_RESOURCE, _TRANSCRIPT_SCHEMA_RESOURCE, _WORKFLOW_SCHEMA_RESOURCE, _EDITORIAL_SCHEMA_RESOURCE
    ))


@lru_cache(maxsize=1)
def _shipped_table_contract() -> dict[str, str]:
    """Derive the current contract from SQL, without a schema version or migration."""
    expected = sqlite3.connect(":memory:")
    try:
        for script in _schema_scripts():
            expected.executescript(script)
        return {name: _normalize_view_sql(sql) for name, sql in expected.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
    finally:
        expected.close()


def initialize_schema(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Bootstrap fresh databases; refuse incompatible tables before any DDL."""
    from bili_asr.storage.archive_validation import require_optional_extensions
    require_optional_extensions(connection)
    from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2, require_universal_contract
    if runtime_contract(connection) == UNIVERSAL_V2:
        require_universal_contract(connection)
        return connection
    accepts_manuscripts = _accepts_manuscript_script(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("SQLite foreign-key enforcement could not be enabled")
    require_archive_schema(connection, allow_empty=True)
    scripts = _schema_scripts() if accepts_manuscripts else _schema_scripts()[:-1]
    for script in scripts:
        connection.executescript(script)
    refresh_shipped_views(connection)
    connection.commit()
    return connection


def require_archive_schema(connection: sqlite3.Connection, *, allow_empty: bool = False) -> None:
    """Check existing runtime tables using reads only; never upgrade an archive.

    Metadata-only archives and the optional tag-observation table retain the
    compatibility accepted by explicit bootstrap. Missing manuscript tables
    remain a separate command contract, not an implicit schema upgrade.
    """
    from bili_asr.storage.archive_validation import require_optional_extensions
    require_optional_extensions(connection)
    from bili_asr.storage.archive_contracts import runtime_contract, UNIVERSAL_V2, require_universal_contract
    if runtime_contract(connection) == UNIVERSAL_V2:
        require_universal_contract(connection)
        return
    accepts_manuscripts = _accepts_manuscript_script(connection)
    tables = {name: _normalize_view_sql(sql) for name, sql in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}
    if not tables:
        if allow_empty:
            return
        raise _rebuild_error("archive schema missing")
    for name, expected in _shipped_table_contract().items():
        if not accepts_manuscripts and name in _manuscript_schema_objects():
            continue
        if name == "video_tag_observations" and name not in tables:
            continue
        if tables.get(name) != expected:
            raise _rebuild_error(f"unsupported table {name}")


def require_subtitle_schema(connection: sqlite3.Connection) -> None:
    """Require the transcript-schema contract on ``connection``.

    The check is structural — the ``transcripts`` columns only this contract
    has, plus the process-record tables and the pending view — because a stale
    version stamp can lie and a missing column cannot.  A database that
    predates the contract raises :class:`SchemaContractError`; the caller
    reports the rebuild procedure and stops.
    """
    if _has_subtitle_schema(connection):
        return
    raise _rebuild_error("transcript schema contract missing")


def open_database(path: str | os.PathLike[str]) -> DatabaseConnection:
    """Open and initialize ``archive.db`` below an archive root.

    Existing directories are interpreted as archive roots. Paths ending in a
    normal SQLite suffix (``.db``, ``.sqlite``, or ``.sqlite3``) are treated as
    explicit database files, which is useful for tests and callers with a
    custom filename; ``:memory:`` opens an in-memory database. URI strings are
    not interpreted, so callers pass plain paths. Connections use an explicit
    deferred transaction mode; callers can use ``with connection:`` for
    atomic write groups.
    """
    database_path = _resolve_database_path(path)
    connection = connect_database(database_path)
    try:
        initialize_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection




@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serve this module's one transaction discipline for a write group.

    The enclosed writes are committed together on success; any exception rolls
    the whole group back and is re-raised, so a caller never observes half of a
    write group.
    """
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


__all__ = [
    "DatabaseConnection",
    "SchemaContractError",
    "duration_to_ms",
    "initialize_schema",
    "normalize_page_index",
    "open_database",
    "require_subtitle_schema",
    "require_archive_schema",
    "require_manuscript_schema",
    "require_editorial_schema",
]
