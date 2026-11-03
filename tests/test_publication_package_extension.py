"""Original releases stay readable; opt-in packages are validated and backed up."""
import sqlite3

import pytest

from bili_asr.cli import main
from bili_asr.publication import create_edition, publish_edition, verify_release
from bili_asr.storage.database import open_database
from bili_asr.storage.publication_packages import install_packages, package_record, require_packages
from bili_asr.storage.schema_contract import SchemaContractError
from bili_asr.storage.snapshots import create_database_snapshot, required_artifacts, validate_snapshot_database
from tests.test_publication import seeded_publication, approve

pytestmark = pytest.mark.usefixtures("enable_content_publication_for_tests")


def test_original_schema_is_readable_without_installing_packages(tmp_path):
    connection, revision, roots = seeded_publication(tmp_path)
    try:
        assert not require_packages(connection)
        edition = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="test")
        approve(connection, edition)
        release = publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=roots,
                                  write_root=tmp_path, actor="test")
        assert verify_release(connection, release["release_id"], roots)[0]["release_id"] == release["release_id"]
        old_contract = validate_snapshot_database(tmp_path / "archive.db")
        install_packages(connection)
        assert package_record(connection, release["release_id"]) is None
        assert verify_release(connection, release["release_id"], roots)[0]["release_id"] == release["release_id"]
        assert validate_snapshot_database(tmp_path / "archive.db") != old_contract
    finally:
        connection.close()


def test_new_package_is_immutable_and_travels_in_consistent_database_backup(tmp_path):
    connection, revision, roots = seeded_publication(tmp_path)
    try:
        install_packages(connection)
        edition = create_edition(connection, revision_id=revision, artifact_roots=roots, actor="test")
        approve(connection, edition)
        release = publish_edition(connection, edition_id=edition["edition_id"], artifact_roots=roots,
                                  write_root=tmp_path, actor="test")
        verify_release(connection, release["release_id"], roots)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM publication_packages")
        connection.rollback()
        expected = validate_snapshot_database(tmp_path / "archive.db")
        assert create_database_snapshot(tmp_path / "archive.db", tmp_path / "backup.db") == expected
        assert required_artifacts(tmp_path / "backup.db")[release["package_path"]] == release["package_sha256"]
        assert install_packages(connection)["reused"]
    finally:
        connection.close()


def test_missing_package_for_post_install_release_fails_closed(tmp_path):
    connection = open_database(tmp_path)
    try:
        install_packages(connection)
        with pytest.raises(ValueError, match="package is missing"):
            package_record(connection, "a" * 64)
        connection.execute("DROP TABLE publication_package_legacy")
        connection.commit()
        with pytest.raises(SchemaContractError, match="missing or altered"):
            require_packages(connection)
    finally:
        connection.close()


def test_explicit_cli_install_holds_maintenance_scope(tmp_path, capsys):
    open_database(tmp_path).close()
    assert main(["archive", "install-publication-packages", "--archive-root", str(tmp_path)]) == 0
    assert '"installed": true' in capsys.readouterr().out
    with sqlite3.connect(tmp_path / "archive.db") as connection:
        assert require_packages(connection)
