"""Runtime access never upgrades an archive and holds coordination until close."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from bili_asr.archive_maintenance import ArchiveBusyError, archive_access
from bili_asr.archive_session import ArchiveAccessMode, ArchiveContract, ArchiveSession, open_archive_connection
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.cli.main import main
from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS, ArtifactPolicy, CommandSpec
from bili_asr.storage.database import SchemaContractError, connect_database


def _bootstrap(root: Path) -> None:
    with ArchiveSession(root, mode=ArchiveAccessMode.BOOTSTRAP):
        pass


def _exclusive_available(root: Path) -> bool:
    def acquire():
        try:
            with archive_access(root, exclusive=True):
                return True
        except ArchiveBusyError:
            return False

    with ThreadPoolExecutor(max_workers=1) as threads:
        return threads.submit(acquire).result(timeout=5)


def _stale_view(root: Path) -> str:
    with sqlite3.connect(root / "archive.db") as connection:
        connection.executescript("DROP VIEW v_missing_audio; CREATE VIEW v_missing_audio AS SELECT 42 AS stale;")
        return connection.execute("SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'").fetchone()[0]


@pytest.mark.parametrize("mode", [ArchiveAccessMode.READ, ArchiveAccessMode.WRITE])
def test_existing_access_does_not_refresh_schema_or_change_database_bytes(tmp_path, mode):
    _bootstrap(tmp_path)
    stale_sql = _stale_view(tmp_path)
    before = (tmp_path / "archive.db").read_bytes()
    with ArchiveSession(tmp_path, mode=mode) as session:
        assert session.connection.row_factory is sqlite3.Row
        assert session.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert session.connection.execute("SELECT stale FROM v_missing_audio").fetchone()[0] == 42
        assert session.connection.execute("SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'").fetchone()[0] == stale_sql
    assert (tmp_path / "archive.db").read_bytes() == before


def test_only_bootstrap_refreshes_shipped_views(tmp_path):
    _bootstrap(tmp_path)
    stale_sql = _stale_view(tmp_path)
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.BOOTSTRAP) as session:
        assert session.connection.execute("SELECT sql FROM sqlite_master WHERE name = 'v_missing_audio'").fetchone()[0] != stale_sql
        assert "video_part_id" in {row[1] for row in session.connection.execute("PRAGMA table_info(v_missing_audio)")}


@pytest.mark.parametrize("command", ["status", "runs"])
def test_metadata_query_cli_preserves_stale_view_and_archive_bytes(tmp_path, command, capsys):
    _bootstrap(tmp_path)
    _stale_view(tmp_path)
    before = (tmp_path / "archive.db").read_bytes()
    assert main([command, "--archive-root", str(tmp_path)]) == 0
    assert (tmp_path / "archive.db").read_bytes() == before
    assert "incompatible" not in capsys.readouterr().err


@pytest.mark.parametrize("mode", [ArchiveAccessMode.READ, ArchiveAccessMode.MAINTENANCE])
def test_read_access_enforces_query_only_and_shared_connection_configuration(tmp_path, mode):
    _bootstrap(tmp_path)
    with ArchiveSession(tmp_path, mode=mode, busy_timeout_ms=173) as session:
        assert session.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 173
        assert session.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            session.connection.execute("INSERT INTO bilibili_users VALUES (1, 'blocked', 1, 1)")


@pytest.mark.parametrize("mode", [ArchiveAccessMode.READ, ArchiveAccessMode.WRITE])
def test_missing_existing_database_is_never_created(tmp_path, mode):
    with pytest.raises(FileNotFoundError, match="no archive database"):
        open_archive_connection(tmp_path, mode=mode)
    assert not (tmp_path / "archive.db").exists()
    assert _exclusive_available(tmp_path)


def test_write_access_commits_data_without_bootstrap(tmp_path):
    _bootstrap(tmp_path)
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.WRITE) as session:
        assert session.connection.execute("PRAGMA query_only").fetchone()[0] == 0
        with session.connection:
            session.connection.execute("INSERT INTO bilibili_users VALUES (1, 'written', 1, 1)")
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.READ) as session:
        assert session.connection.execute("SELECT display_name FROM bilibili_users WHERE mid = 1").fetchone()[0] == "written"


def test_connection_close_releases_programmatic_access_and_out_of_order_nested_connections(tmp_path):
    _bootstrap(tmp_path)
    first = open_archive_connection(tmp_path, mode=ArchiveAccessMode.WRITE)
    second = open_archive_connection(tmp_path, mode=ArchiveAccessMode.WRITE)
    try:
        assert not _exclusive_available(tmp_path)
        first.close()
        assert not _exclusive_available(tmp_path)
        second.close()
        assert _exclusive_available(tmp_path)
    finally:
        first.close()
        second.close()


def test_failed_close_from_another_thread_preserves_live_connection_access(tmp_path):
    _bootstrap(tmp_path)
    connection = open_archive_connection(tmp_path, mode=ArchiveAccessMode.WRITE)
    try:
        with ThreadPoolExecutor(max_workers=1) as threads:
            with pytest.raises(sqlite3.ProgrammingError):
                threads.submit(connection.close).result(timeout=5)
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert not _exclusive_available(tmp_path)
    finally:
        connection.close()
    assert _exclusive_available(tmp_path)


def test_session_preserves_explicit_database_and_artifact_scope(tmp_path):
    archive = tmp_path / "archive"
    products = tmp_path / "products"
    products.mkdir()
    roots = ArtifactRoots.of(archive, products)
    with ArchiveSession(archive / "custom.sqlite", mode=ArchiveAccessMode.BOOTSTRAP, artifact_roots=roots) as session:
        assert session.database_path == archive / "custom.sqlite"
        assert session.artifact_roots.read_bases() == (products, archive)
    assert not (archive / "archive.db").exists()
    with pytest.raises(ValueError, match="different archive"):
        ArchiveSession(tmp_path / "other", mode=ArchiveAccessMode.READ, artifact_roots=roots)


def test_explicit_no_contract_read_supports_frozen_legacy_maintenance_only(tmp_path):
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE historical(value TEXT)")
        connection.execute("INSERT INTO historical VALUES ('kept')")
    before = path.read_bytes()
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.MAINTENANCE, contract=ArchiveContract.NONE) as session:
        assert session.connection.execute("SELECT value FROM historical").fetchone()[0] == "kept"
    for mode in (ArchiveAccessMode.READ, ArchiveAccessMode.WRITE, ArchiveAccessMode.BOOTSTRAP):
        with pytest.raises(SchemaContractError, match="unsupported table"):
            open_archive_connection(tmp_path, mode=mode)
        assert path.read_bytes() == before
        assert _exclusive_available(tmp_path)


def test_symlink_database_refused_before_any_schema_or_data_access(tmp_path):
    real = tmp_path / "real"
    linked = tmp_path / "linked"
    _bootstrap(real)
    linked.mkdir()
    try:
        (linked / "archive.db").symlink_to(real / "archive.db")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    before = (real / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="cannot use links"):
        open_archive_connection(linked, mode=ArchiveAccessMode.READ)
    assert (real / "archive.db").read_bytes() == before


def test_linked_parent_is_refused_before_dotdot_normalization(tmp_path):
    real = tmp_path / "actual" / "archive"
    _bootstrap(real)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    # abspath alone would erase the link component; inspect the supplied path
    # before normalizing it into a root or database filename.
    misleading = linked / ".." / "actual" / "archive"
    before = (real / "archive.db").read_bytes()
    with pytest.raises(ValueError, match="link or reparse"):
        open_archive_connection(misleading, mode=ArchiveAccessMode.READ)
    assert (real / "archive.db").read_bytes() == before


def test_background_lease_connection_retains_maintenance_access_after_primary_connection_closes(tmp_path):
    from threading import Event, Thread
    from bili_asr.storage.workflow import WorkflowRepository

    _bootstrap(tmp_path)
    connection = open_archive_connection(tmp_path, mode=ArchiveAccessMode.WRITE, busy_timeout_ms=173)
    repository = WorkflowRepository(connection)
    opened, release = Event(), Event()
    errors = []

    def heartbeat():
        try:
            lease_repository = repository.open_lease_repository()
            try:
                assert lease_repository.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 173
                opened.set()
                assert release.wait(timeout=10)
            finally:
                lease_repository.close()
        except BaseException as exc:
            errors.append(exc)
            opened.set()

    worker = Thread(target=heartbeat)
    worker.start()
    try:
        assert opened.wait(timeout=10)
        assert not errors
        connection.close()
        assert not _exclusive_available(tmp_path)
    finally:
        release.set()
        worker.join(timeout=10)
        connection.close()
    assert not worker.is_alive() and not errors
    assert _exclusive_available(tmp_path)


def test_cli_invocation_lease_survives_after_an_independent_connection_escapes_it(tmp_path):
    _bootstrap(tmp_path)
    with ArchiveSession(tmp_path, mode=ArchiveAccessMode.WRITE).access():
        connection = open_archive_connection(tmp_path, mode=ArchiveAccessMode.WRITE)
    try:
        assert not _exclusive_available(tmp_path)
    finally:
        connection.close()
    assert _exclusive_available(tmp_path)


@pytest.mark.parametrize("value", [0, -1, 300001, True, 1.5, "123"])
def test_explicit_timeout_is_validated_before_connection_creation(tmp_path, value):
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        connect_database(tmp_path / "archive.db", busy_timeout_ms=value)
    assert not (tmp_path / "archive.db").exists()


def test_command_registration_requires_explicit_database_policy():
    with pytest.raises(TypeError):
        CommandSpec("_handler", "bili_asr.cli.ops", ArtifactPolicy.NONE)
    with pytest.raises(TypeError, match="database policy"):
        CommandSpec("_handler", "bili_asr.cli.ops", ArtifactPolicy.NONE, "read")


@pytest.mark.parametrize("argv,expected", [
    (["status"], ArchiveAccessMode.READ),
    (["runs"], ArchiveAccessMode.READ),
    (["workflow", "status"], ArchiveAccessMode.READ),
    (["workflow", "explain", "--job-id", "job"], ArchiveAccessMode.READ),
    (["workflow", "asr-evidence", "--run-id", "run", "--part-id", "1"], ArchiveAccessMode.READ),
    (["workflow", "plan", "--part-id", "1"], ArchiveAccessMode.WRITE),
    (["search", "word"], ArchiveAccessMode.READ),
    (["search", "word", "--rebuild"], ArchiveAccessMode.WRITE),
    (["publication", "show", "--edition-id", "edition"], ArchiveAccessMode.READ),
    (["publication", "edit", "--edition-id", "edition", "--markdown-file", "draft.md", "--actor", "operator", "--note", "edit"], ArchiveAccessMode.WRITE),
    (["snapshot", "check", "--file", "snapshot.zip"], None),
    (["snapshot", "save", "--out", "snapshot.zip"], ArchiveAccessMode.MAINTENANCE),
])
def test_parser_actions_select_declared_database_access(argv, expected):
    args = build_parser().parse_args(argv)
    assert COMMANDS[args.command].database_for(args) is expected
